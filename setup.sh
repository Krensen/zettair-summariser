#!/usr/bin/env bash
# setup.sh — provision a fresh Mac Mini as a zettair-summariser worker.
#
# Idempotent: safe to re-run after pulling new commits, swapping models,
# or changing the SSH host.
#
# Usage:
#   bash setup.sh
#
# After this runs, the worker is registered with launchd and starts
# sweeping the prod queue on its schedule. Watch the log:
#   tail -f ~/zettair-summariser-data/logs/poll.log

set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
DATA_DIR="$HOME/zettair-summariser-data"
CONFIG_FILE="$REPO_DIR/config.toml"
PLIST_NAME="com.zettair.summariser"
PLIST_PATH="$HOME/Library/LaunchAgents/$PLIST_NAME.plist"

log() { echo "$(date '+%H:%M:%S') ── $*"; }

### ── 1. Tooling check ────────────────────────────────────────────────────
log "Checking tooling..."
command -v python3 >/dev/null || { echo "ERROR: python3 not on PATH"; exit 1; }
command -v rsync   >/dev/null || { echo "ERROR: rsync not on PATH"; exit 1; }
command -v ssh     >/dev/null || { echo "ERROR: ssh not on PATH"; exit 1; }

PY_VER=$(python3 -c 'import sys; print("{}.{}".format(*sys.version_info[:2]))')
PY_MAJOR=${PY_VER%.*}
PY_MINOR=${PY_VER#*.}
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" = "3" ] && [ "$PY_MINOR" -lt 11 ]; }; then
    echo "ERROR: python3 >= 3.11 required for tomllib (have $PY_VER)" >&2
    exit 1
fi

### ── 2. Config ────────────────────────────────────────────────────────────
if [ ! -f "$CONFIG_FILE" ]; then
    cp "$REPO_DIR/config.example.toml" "$CONFIG_FILE"
    log "Created $CONFIG_FILE — edit it before continuing:"
    log "  - prod.ssh_host         (e.g. sparky@zettair-prod)"
    log "  - model.backend         (stub | ollama)"
    log "  - model.ollama_model    (if backend=ollama)"
    log "Re-run setup.sh after editing."
    exit 0
fi
log "Using $CONFIG_FILE"

### ── 3. Data directories ─────────────────────────────────────────────────
log "Ensuring data directories exist at $DATA_DIR ..."
mkdir -p "$DATA_DIR"/{inbox,inbox-processed,outbox,errors-outbox,logs}

### ── 4. SSH connectivity smoke test ──────────────────────────────────────
SSH_HOST=$(python3 -c "import tomllib; print(tomllib.load(open('$CONFIG_FILE','rb'))['prod']['ssh_host'])")
log "Testing SSH to $SSH_HOST ..."
if ssh -o BatchMode=yes -o ConnectTimeout=5 "$SSH_HOST" "echo ok" >/dev/null 2>&1; then
    log "  SSH OK"
else
    log "  SSH FAILED — fix key auth before continuing"
    log "  Test manually: ssh $SSH_HOST"
    exit 1
fi

### ── 5. launchd plist ────────────────────────────────────────────────────
log "Installing launchd job $PLIST_NAME ..."
mkdir -p "$(dirname "$PLIST_PATH")"
INTERVAL=$(python3 -c "import tomllib; print(tomllib.load(open('$CONFIG_FILE','rb'))['poll']['interval_seconds'])")
cat > "$PLIST_PATH" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
  <dict>
    <key>Label</key><string>$PLIST_NAME</string>
    <key>WorkingDirectory</key><string>$REPO_DIR</string>
    <key>ProgramArguments</key>
    <array>
      <string>/usr/bin/env</string>
      <string>python3</string>
      <string>$REPO_DIR/poll.py</string>
      <string>--once</string>
    </array>
    <key>StartInterval</key><integer>$INTERVAL</integer>
    <key>RunAtLoad</key><true/>
    <key>StandardOutPath</key><string>$DATA_DIR/logs/launchd.out</string>
    <key>StandardErrorPath</key><string>$DATA_DIR/logs/launchd.err</string>
    <key>EnvironmentVariables</key>
    <dict>
      <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>
  </dict>
</plist>
PLIST

# Reload — bootout is idempotent (no error if not loaded).
launchctl bootout "gui/$(id -u)/$PLIST_NAME" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
log "  launchd job loaded; first run will fire now."

### ── 6. Quick smoke test ─────────────────────────────────────────────────
log "Running one --once sweep to verify..."
( cd "$REPO_DIR" && python3 poll.py --once ) || {
    log "Smoke test failed. Check $DATA_DIR/logs/poll.log"
    exit 1
}

log "Setup complete. Worker will sweep every $INTERVAL seconds."
log "  log:   tail -f $DATA_DIR/logs/poll.log"
log "  stop:  launchctl bootout gui/\$(id -u)/$PLIST_NAME"
log "  start: launchctl bootstrap gui/\$(id -u) $PLIST_PATH"
