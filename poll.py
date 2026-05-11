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
    # Default priority_inbox next to inbox if the user hasn't set one in
    # their config.toml. Existing configs keep working without edits.
    if "priority_inbox" not in cfg["local"] and "inbox" in cfg["local"]:
        cfg["local"]["priority_inbox"] = cfg["local"]["inbox"].parent / "priority-inbox"
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


def _rsync_pull_one(cfg: dict, remote_path: str, local_inbox) -> int:
    """Pull *.json from one remote dir into one local inbox.
    Returns number of new files claimed (post - pre)."""
    local_inbox.mkdir(parents=True, exist_ok=True)
    remote = f'{cfg["prod"]["ssh_host"]}:{remote_path}/'
    before = len(list(local_inbox.glob("*.json")))
    cmd = [
        "rsync", *RSYNC_FLAGS,
        "--include=*.json", "--exclude=*",
        remote, str(local_inbox) + "/",
    ]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        log(cfg, f"rsync pull {remote_path} failed rc={r.returncode}: {r.stderr.strip()[:200]}")
    after = len(list(local_inbox.glob("*.json")))
    return max(0, after - before)


def rsync_pull(cfg: dict) -> tuple[int, int]:
    """Pull from BOTH priority/ and pending/ on prod into the local
    priority-inbox and inbox respectively. Returns (priority_pulled,
    pending_pulled). The priority dir's remote path falls back to
    inferring it from remote_pending if not configured, so existing
    config.toml files don't need editing immediately."""
    pri = _rsync_pull_one(
        cfg,
        cfg["prod"].get("remote_priority")
            or cfg["prod"]["remote_pending"].rsplit("/", 1)[0] + "/priority",
        cfg["local"]["priority_inbox"],
    )
    bulk = _rsync_pull_one(cfg, cfg["prod"]["remote_pending"], cfg["local"]["inbox"])
    return pri, bulk


def rsync_push(cfg: dict) -> tuple[int, int]:
    """Push outbox/*.md and errors-outbox/*.json to prod. Returns
    (n_done_uploaded, n_errors_uploaded)."""
    out = cfg["local"]["outbox"]
    err = cfg["local"]["errors_outbox"]
    out.mkdir(parents=True, exist_ok=True)
    err.mkdir(parents=True, exist_ok=True)

    before_done = len(list(out.glob("*.md")))
    before_err  = len(list(err.glob("*.json")))
    n_done = n_err = 0

    if before_done:
        cmd = [
            "rsync", *RSYNC_FLAGS,
            "--include=*.md", "--exclude=*",
            str(out) + "/",
            f'{cfg["prod"]["ssh_host"]}:{cfg["prod"]["remote_done"]}/',
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        after_done = len(list(out.glob("*.md")))
        n_done = before_done - after_done
        if r.returncode != 0:
            log(cfg, f"rsync push done failed rc={r.returncode}: {r.stderr.strip()[:300]}")
        elif n_done != before_done:
            # rsync exited 0 but didn't actually move everything we expected.
            log(cfg, f"rsync push done suspicious: before={before_done} after={after_done} stdout={r.stdout.strip()[:300]} stderr={r.stderr.strip()[:300]}")

    if before_err:
        cmd = [
            "rsync", *RSYNC_FLAGS,
            "--include=*.json", "--exclude=*",
            str(err) + "/",
            f'{cfg["prod"]["ssh_host"]}:{cfg["prod"]["remote_errors"]}/',
        ]
        r = subprocess.run(cmd, capture_output=True, text=True)
        after_err = len(list(err.glob("*.json")))
        n_err = before_err - after_err
        if r.returncode != 0:
            log(cfg, f"rsync push errors failed rc={r.returncode}: {r.stderr.strip()[:300]}")
        elif n_err != before_err:
            log(cfg, f"rsync push errors suspicious: before={before_err} after={after_err} stdout={r.stdout.strip()[:300]} stderr={r.stderr.strip()[:300]}")

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
        job = gen.job_from_pending_json(raw, top_m=cfg["model"].get("top_m", 5))
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
    pri_pulled, bulk_pulled = rsync_pull(cfg)
    priority_inbox = cfg["local"]["priority_inbox"]
    inbox = cfg["local"]["inbox"]
    processed_dir = cfg["local"]["inbox_processed"]
    processed_dir.mkdir(parents=True, exist_ok=True)

    max_jobs = cfg["poll"]["max_jobs_per_sweep"]
    # Drain priority-inbox FIRST. If priority work fills the sweep
    # budget, bulk pending waits another cycle. That's the whole
    # point of having a priority lane.
    pri_jobs = sorted(priority_inbox.glob("*.json"))[: max_jobs]
    remaining = max_jobs - len(pri_jobs)
    bulk_jobs = sorted(inbox.glob("*.json"))[: max(0, remaining)]
    jobs = pri_jobs + bulk_jobs
    if not jobs:
        log(cfg, f"sweep: pri_pulled={pri_pulled} bulk_pulled={bulk_pulled}, no jobs anywhere")
        # Still push in case there are stale outbox/errors from last sweep
        rsync_push(cfg)
        return
    if pri_jobs:
        log(cfg, f"sweep: draining {len(pri_jobs)} priority + {len(bulk_jobs)} bulk")

    # Push every push_every jobs (default 5) so prod sees summaries
    # land continuously instead of waiting for the whole sweep to
    # finish. At 2-4 min per generation, a full sweep can take an hour;
    # we want the live site to start showing summaries within the first
    # ~10-15 min.
    push_every = cfg["poll"].get("push_every", 5)

    n_ok = n_err = 0
    n_pushed_done = n_pushed_err = 0
    since_last_push = 0
    t0 = time.time()
    for i, j in enumerate(jobs, 1):
        outcome, qnorm = process_one(cfg, j)
        if outcome == "ok":
            n_ok += 1
        elif outcome == "error":
            n_err += 1
        # Audit-trail move regardless of outcome
        j.replace(processed_dir / j.name)

        since_last_push += 1
        # Push intermediately when we've accumulated push_every results,
        # OR when we've reached the end of the batch.
        if since_last_push >= push_every or i == len(jobs):
            d, e = rsync_push(cfg)
            n_pushed_done += d
            n_pushed_err  += e
            since_last_push = 0

    elapsed = time.time() - t0
    log(
        cfg,
        f"sweep: pri_pulled={pri_pulled} bulk_pulled={bulk_pulled} "
        f"processed={len(jobs)} ok={n_ok} err={n_err} "
        f"pushed_done={n_pushed_done} pushed_err={n_pushed_err} "
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
