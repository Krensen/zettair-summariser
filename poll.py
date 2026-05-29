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


def _run_rsync(cmd: list[str]) -> tuple[int, str, str]:
    """Run an rsync command, returning (rc, stdout, stderr) with
    UTF-8-replace decoding.

    We can't use subprocess.run(..., text=True) here because rsync's
    stdout includes filenames in whatever encoding the filesystem
    uses, and a single non-UTF-8 byte (e.g. from a producer that
    emitted a Latin-1 filename) crashes the whole sweep with a
    UnicodeDecodeError. Capturing as bytes and decoding with
    errors='replace' lets the worker survive bad filenames and
    log a safe approximation of them."""
    r = subprocess.run(cmd, capture_output=True)
    out = (r.stdout or b"").decode("utf-8", errors="replace")
    err = (r.stderr or b"").decode("utf-8", errors="replace")
    return r.returncode, out, err


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
    rc, _out, err = _run_rsync(cmd)
    if rc != 0:
        log(cfg, f"rsync pull {remote_path} failed rc={rc}: {err.strip()[:200]}")
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
        rc, out_buf, err_buf = _run_rsync(cmd)
        after_done = len(list(out.glob("*.md")))
        n_done = before_done - after_done
        if rc != 0:
            log(cfg, f"rsync push done failed rc={rc}: {err_buf.strip()[:300]}")
        elif n_done != before_done:
            # rsync exited 0 but didn't actually move everything we expected.
            log(cfg, f"rsync push done suspicious: before={before_done} after={after_done} stdout={out_buf.strip()[:300]} stderr={err_buf.strip()[:300]}")

    if before_err:
        cmd = [
            "rsync", *RSYNC_FLAGS,
            "--include=*.json", "--exclude=*",
            str(err) + "/",
            f'{cfg["prod"]["ssh_host"]}:{cfg["prod"]["remote_errors"]}/',
        ]
        rc, out_buf, err_buf = _run_rsync(cmd)
        after_err = len(list(err.glob("*.json")))
        n_err = before_err - after_err
        if rc != 0:
            log(cfg, f"rsync push errors failed rc={rc}: {err_buf.strip()[:300]}")
        elif n_err != before_err:
            log(cfg, f"rsync push errors suspicious: before={before_err} after={after_err} stdout={out_buf.strip()[:300]} stderr={err_buf.strip()[:300]}")

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

def _next_job(cfg: dict):
    """Pick the next job to process. Priority queue first, bulk after.
    Returns a Path or None. Re-globs each call so newly-arrived
    priority files preempt bulk work."""
    pri = sorted(cfg["local"]["priority_inbox"].glob("*.json"))
    if pri:
        return pri[0], "priority"
    bulk = sorted(cfg["local"]["inbox"].glob("*.json"))
    if bulk:
        return bulk[0], "bulk"
    return None, None


def sweep(cfg: dict) -> None:
    # Initial pull: bring both priority and bulk down before the loop
    # starts. The intra-loop pulls below catch anything new that lands
    # while we're processing.
    pri_pulled_total, bulk_pulled_total = rsync_pull(cfg)
    processed_dir = cfg["local"]["inbox_processed"]
    processed_dir.mkdir(parents=True, exist_ok=True)

    max_jobs = cfg["poll"]["max_jobs_per_sweep"]
    push_every = cfg["poll"].get("push_every", 5)

    # Each iteration:
    #   1. Re-pull from priority/ on prod (cheap; rsync no-ops when empty).
    #      This ensures newly-arrived priority jobs preempt bulk work
    #      even mid-sweep instead of waiting hours for the next sweep.
    #   2. Pick the next job — priority first, bulk fallback.
    #   3. Process it.
    # The intra-loop pull only does priority (not bulk) because bulk
    # is huge and we don't want to pay rsync cost on every iteration.
    n_ok = n_err = n_skipped = 0
    n_pushed_done = n_pushed_err = 0
    n_processed_pri = n_processed_bulk = 0
    since_last_push = 0
    t0 = time.time()
    pri_inbox = cfg["local"]["priority_inbox"]
    for i in range(1, max_jobs + 1):
        # Intra-loop priority pull. Use the dedicated pull helper for
        # priority only — bulk is too expensive to re-pull each tick.
        pri_pulled_now = _rsync_pull_one(
            cfg,
            cfg["prod"].get("remote_priority")
                or cfg["prod"]["remote_pending"].rsplit("/", 1)[0] + "/priority",
            pri_inbox,
        )
        if pri_pulled_now:
            pri_pulled_total += pri_pulled_now
            log(cfg, f"  intra-sweep: pulled {pri_pulled_now} priority job(s)")

        j, source = _next_job(cfg)
        if j is None:
            break

        # Claim the job atomically. If the file's gone (race with a
        # parallel worker) skip without LLM cost.
        claimed = processed_dir / j.name
        try:
            j.replace(claimed)
        except FileNotFoundError:
            n_skipped += 1
            continue

        outcome, qnorm = process_one(cfg, claimed)
        if outcome == "ok":
            n_ok += 1
        elif outcome == "error":
            n_err += 1
        if source == "priority":
            n_processed_pri += 1
        else:
            n_processed_bulk += 1

        since_last_push += 1
        if since_last_push >= push_every:
            d, e = rsync_push(cfg)
            n_pushed_done += d
            n_pushed_err  += e
            since_last_push = 0

    # Final push to flush anything left in outbox.
    if since_last_push > 0 or n_ok > 0 or n_err > 0:
        d, e = rsync_push(cfg)
        n_pushed_done += d
        n_pushed_err  += e

    processed = n_processed_pri + n_processed_bulk
    elapsed = time.time() - t0
    if processed == 0 and n_skipped == 0:
        log(cfg, f"sweep: pri_pulled={pri_pulled_total} bulk_pulled={bulk_pulled_total}, no jobs anywhere")
    else:
        log(
            cfg,
            f"sweep: pri_pulled={pri_pulled_total} bulk_pulled={bulk_pulled_total} "
            f"processed={processed} (pri={n_processed_pri} bulk={n_processed_bulk}) "
            f"ok={n_ok} err={n_err} skipped={n_skipped} "
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
