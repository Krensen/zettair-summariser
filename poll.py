#!/usr/bin/env python3
"""
poll.py — the Mac-Mini-side worker for PRD-018 knowledge-panel summaries.

One sweep:
  1. rsync pending/ from prod → local inbox (claims jobs via
     --remove-source-files; per-file atomic, multi-worker safe).
  2. For each job: call generate(); write outbox/<query_norm>.md OR
     errors-outbox/<query_norm>.error.json.
  3. rsync outbox + errors-outbox → prod (done/, errors/).
  4. Move inbox/<query_norm>.json to inbox-processed/ as audit trail.

Invocation:
  python3 poll.py [--once]       run one sweep and exit (default)
  python3 poll.py --daemon       loop forever, sleeping [poll].interval_seconds

The launchd plist invokes us in --once mode every interval — that's
the canonical setup. --daemon is for interactive debugging.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
import tomllib
from pathlib import Path

import generate as gen


# -- config -----------------------------------------------------------------

def load_config(path: Path) -> dict:
    if not path.exists():
        sys.exit(
            f"ERROR: {path} not found. Copy config.example.toml to "
            f"{path.name} and edit for this machine."
        )
    with open(path, "rb") as f:
        cfg = tomllib.load(f)
    # Expand ~ in local paths
    for k, v in cfg.get("local", {}).items():
        cfg["local"][k] = Path(os.path.expanduser(v))
    return cfg


# -- logging ----------------------------------------------------------------

def log_path(cfg: dict) -> Path:
    return cfg["local"]["logs"] / "poll.log"


def log(cfg: dict, msg: str) -> None:
    ts = dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    line = f"[{ts}] {msg}"
    print(line, flush=True)
    p = log_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    with open(p, "a", encoding="utf-8") as f:
        f.write(line + "\n")


# -- rsync helpers ----------------------------------------------------------

# `-a` is `-rlptgoD`; the `-p` / `-t` / `-g` / `-o` bits all need
# write permission on the directory itself (not just its contents),
# which sparky doesn't have on the prod queue dirs (owned by zettair,
# mode 2775 — group can write entries, not modify dir metadata).
# We want destination defaults for everything anyway. `-rv` for
# recursion + verbose plus the explicit `--no-*` flags is enough.
RSYNC_FLAGS = [
    "-rv",
    "--no-perms", "--no-owner", "--no-group",
    "--no-times", "--omit-dir-times",
    "--remove-source-files",
]


def rsync_pull(cfg: dict) -> int:
    """Pull pending/*.json from prod to local inbox. Returns count claimed."""
    inbox = cfg["local"]["inbox"]
    inbox.mkdir(parents=True, exist_ok=True)
    remote = f'{cfg["prod"]["ssh_host"]}:{cfg["prod"]["remote_pending"]}/'
    before = len(list(inbox.glob("*.json")))
    cmd = [
        "rsync", *RSYNC_FLAGS,
        "--include=*.json", "--exclude=*",
        remote, str(inbox) + "/",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log(cfg, f"rsync pull failed rc={r.returncode}: {r.stderr.strip()[:200]}")
    after = len(list(inbox.glob("*.json")))
    return max(0, after - before)


def rsync_push(cfg: dict) -> tuple[int, int]:
    """Push outbox/*.md and errors-outbox/*.json to prod. Returns
    (n_done_uploaded, n_errors_uploaded)."""
    out = cfg["local"]["outbox"]
    err = cfg["local"]["errors_outbox"]
    out.mkdir(parents=True, exist_ok=True)
    err.mkdir(parents=True, exist_ok=True)

    n_done = len(list(out.glob("*.md")))
    n_err  = len(list(err.glob("*.json")))

    if n_done:
        cmd = [
            "rsync", *RSYNC_FLAGS,
            "--include=*.md", "--exclude=*",
            str(out) + "/",
            f'{cfg["prod"]["ssh_host"]}:{cfg["prod"]["remote_done"]}/',
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            log(cfg, f"rsync push done failed rc={r.returncode}: {r.stderr.strip()[:200]}")
            n_done = 0  # treat as not-uploaded; will retry next sweep

    if n_err:
        cmd = [
            "rsync", *RSYNC_FLAGS,
            "--include=*.json", "--exclude=*",
            str(err) + "/",
            f'{cfg["prod"]["ssh_host"]}:{cfg["prod"]["remote_errors"]}/',
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            log(cfg, f"rsync push errors failed rc={r.returncode}: {r.stderr.strip()[:200]}")
            n_err = 0

    return n_done, n_err


# -- generation -------------------------------------------------------------

def process_one(cfg: dict, job_file: Path) -> tuple[str, str]:
    """Run one job. Returns (outcome, query_norm) where outcome is
    'ok' / 'error' / 'skip'."""
    # Read as bytes and decode with errors=replace. Producer should
    # write valid UTF-8, but if a job slipped through with bad bytes
    # we'd rather replace them than crash the whole sweep.
    try:
        raw = json.loads(job_file.read_bytes().decode("utf-8", errors="replace"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        query_norm = job_file.stem
        err_path = cfg["local"]["errors_outbox"] / f"{query_norm}.error.json"
        err_path.parent.mkdir(parents=True, exist_ok=True)
        with open(err_path, "w", encoding="utf-8") as f:
            json.dump({
                "query_norm": query_norm,
                "error": f"unparseable job file: {type(e).__name__}: {e}",
                "failed_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "backend": cfg["model"].get("backend"),
            }, f, indent=2)
        return ("error", query_norm)
    query_norm = raw.get("query_norm", job_file.stem)
    try:
        job = gen.job_from_pending_json(raw)
        md = gen.generate(job, cfg["model"])
    except gen.GenerationError as e:
        # Write an error record; worker will rsync it to prod's errors/.
        err_path = cfg["local"]["errors_outbox"] / f"{query_norm}.error.json"
        err_path.parent.mkdir(parents=True, exist_ok=True)
        with open(err_path, "w", encoding="utf-8") as f:
            json.dump({
                "query_norm": query_norm,
                "query": raw.get("query"),
                "error": str(e),
                "failed_at": dt.datetime.now(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "backend": cfg["model"].get("backend"),
            }, f, indent=2)
        return ("error", query_norm)

    out_path = cfg["local"]["outbox"] / f"{query_norm}.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write — model output can be long, we never want a partial file
    # to be rsync'd up.
    tmp = out_path.with_suffix(".md.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(md.rstrip() + "\n")
    tmp.replace(out_path)
    return ("ok", query_norm)


# -- sweep ------------------------------------------------------------------

def sweep(cfg: dict) -> None:
    pulled = rsync_pull(cfg)
    inbox = cfg["local"]["inbox"]
    processed_dir = cfg["local"]["inbox_processed"]
    processed_dir.mkdir(parents=True, exist_ok=True)

    jobs = sorted(inbox.glob("*.json"))[: cfg["poll"]["max_jobs_per_sweep"]]
    if not jobs:
        log(cfg, f"sweep: pulled={pulled}, no jobs in inbox, nothing to do")
        # Still push in case there are stale outbox/errors from last sweep
        rsync_push(cfg)
        return

    n_ok = n_err = 0
    t0 = time.time()
    for j in jobs:
        outcome, qnorm = process_one(cfg, j)
        if outcome == "ok":
            n_ok += 1
        elif outcome == "error":
            n_err += 1
        # Audit-trail move regardless of outcome
        j.replace(processed_dir / j.name)

    pushed_ok, pushed_err = rsync_push(cfg)
    elapsed = time.time() - t0
    log(
        cfg,
        f"sweep: pulled={pulled} processed={len(jobs)} "
        f"ok={n_ok} err={n_err} pushed_done={pushed_ok} pushed_err={pushed_err} "
        f"elapsed={elapsed:.1f}s",
    )


# -- main -------------------------------------------------------------------

def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--config", default="config.toml", help="path to config TOML")
    p.add_argument("--once", action="store_true", help="run one sweep and exit (default)")
    p.add_argument("--daemon", action="store_true", help="loop forever")
    args = p.parse_args()

    cfg = load_config(Path(args.config))

    if args.daemon:
        interval = cfg["poll"]["interval_seconds"]
        log(cfg, f"daemon mode, interval={interval}s")
        while True:
            try:
                sweep(cfg)
            except Exception as e:
                log(cfg, f"sweep crashed: {type(e).__name__}: {e}")
            time.sleep(interval)
    else:
        sweep(cfg)


if __name__ == "__main__":
    main()
