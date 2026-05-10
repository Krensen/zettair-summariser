# zettair-summariser

Mac Mini side of [PRD-018](https://github.com/Krensen/zettair-search/blob/main/prd/PRD-018-knowledge-panel.md) — the knowledge-panel summary worker for [zettair-search](https://github.com/Krensen/zettair-search).

Pulls pending summary jobs from the prod box, runs them through a local model, pushes the rendered summaries back. File-based queue, no orchestration daemon, idempotent at every step.

## What it does

```
   ┌─────────────────────────┐                  ┌──────────────────────────┐
   │  PROD (zettair@hetzner) │                  │  THIS MAC MINI            │
   │                         │                  │                           │
   │  /mnt/.../summaries/    │                  │  ~/zettair-summariser-    │
   │    pending/*.json       │ ─── rsync ────▶  │    data/inbox/            │
   │    done/*.md            │ ◀── rsync ───    │    outbox/                │
   │    errors/*.json        │ ◀── rsync ───    │    errors-outbox/         │
   └─────────────────────────┘                  │                           │
                                                 │  poll.py one sweep:       │
                                                 │   1. rsync down           │
                                                 │   2. generate.py per job  │
                                                 │   3. rsync up             │
                                                 │   4. move inbox→processed │
                                                 │                           │
                                                 │  launchd fires --once     │
                                                 │  every poll.interval_sec  │
                                                 └──────────────────────────┘
```

Multiple Mac Minis can poll the same queue — `rsync --remove-source-files` is per-file atomic, so each worker claims a disjoint subset.

## Setup on a fresh Mac Mini

```bash
git clone https://github.com/Krensen/zettair-summariser.git
cd zettair-summariser
bash setup.sh
# → creates config.toml from config.example.toml on first run; edit it.
bash setup.sh
# → second invocation finds config.toml, verifies SSH, installs launchd job,
#   fires one sweep as a smoke test.
```

Prerequisites:
- macOS with `python3 >= 3.11` (for `tomllib`), `rsync`, `ssh` on PATH.
- An SSH key set up against the prod host. `ssh sparky@prod-host echo ok` should succeed without prompts.
- (For non-stub backends) a running [ollama](https://ollama.com/) instance with the configured model pulled.

## Running

The launchd job invokes `poll.py --once` every `poll.interval_seconds`. To run manually:

```bash
python3 poll.py --once         # one sweep, exit
python3 poll.py --daemon       # loop forever
```

Logs land at `~/zettair-summariser-data/logs/poll.log` plus the launchd `*.out` / `*.err` next to it.

## Backends

`config.toml` `[model] backend = "..."`:

- `stub` — deterministic placeholder summary, no model required. Used for round-trip testing.
- `ollama` — POSTs to a local ollama server, retries on parse / timeout errors.

Add more backends by editing `generate.py`. `prompt.py` carries the prompt template and the output parser; both are independent of backend.

## Layout

```
poll.py                    — top-level worker
generate.py                — backend dispatch + ollama implementation
prompt.py                  — prompt template + output parser + utilities
config.example.toml        — template config (copy to config.toml)
setup.sh                   — provisions a fresh Mac Mini
launchd/                   — (reserved for future plist templates)
tests/
  fixtures/morrissey.json  — example pending-job file
  test_round_trip.py       — stub-backend tests
```

## Testing

```bash
python3 tests/test_round_trip.py
```

Six tests covering prompt assembly, output parsing, stub generation, and UTF-8 boundary handling. No network, no model required.

## See also

- [PRD-018 — Knowledge panel](https://github.com/Krensen/zettair-search/blob/main/prd/PRD-018-knowledge-panel.md) — full architecture doc.
- [zettair-search](https://github.com/Krensen/zettair-search) — the search service this feeds.
