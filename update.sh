#!/usr/bin/env bash
# anthrouter update script.
#
# In-place update flow: stop → backup venv → fresh clone from manifest upstreamUrl → pip upgrade → restart → health check → rollback on failure.
# Never touches: settings.json, caveman.yaml, shell profile, config.env, anthrouter.db.
set -euo pipefail

HOME_DIR="${ANTHROUTER_HOME:-$HOME/.anthrouter}"
MANIFEST="$HOME_DIR/manifest.json"
CONFIG_ENV="$HOME_DIR/config.env"
LOCK_DIR="$HOME_DIR/update.lock"
VENV="$HOME_DIR/venv"
VENV_BAK="$HOME_DIR/venv.bak"
TMPDIR_CLONE=""

FAILED=0
LOCK_CREATED=0
# BACKUP_FAILED=1 means backup step failed — do NOT attempt restore (original venv is intact)
BACKUP_FAILED=0

say() { echo "[anthrouter] $*"; }
die() {
  echo "[anthrouter] error: $*" >&2
  exit 1
}

# Cleanup trap: restore on failure (if applicable), then remove lock and temp clone dir
cleanup() {
  local EXIT_CODE=$?
  
  # Step 9: restore sequence (ONLY if FAILED=1 and BACKUP_FAILED=0)
  # If BACKUP_FAILED=1, the backup is untrustworthy and the original venv is still intact — no restore needed
  if [ "$FAILED" = "1" ] && [ "$BACKUP_FAILED" = "0" ]; then
    # Kill the new process if it's still running before restoring old version
    if [ -n "${NEW_PID:-}" ]; then
      if kill -0 "$NEW_PID" 2>/dev/null; then
        say "Stopping failed new version (pid $NEW_PID) before restore..."
        kill -TERM "$NEW_PID" 2>/dev/null || true
        sleep 0.5
      fi
    fi

    say "Update failed — attempting restore..."
    
    # Probe for usable backup
    if ! test -x "$VENV_BAK/bin/python"; then
      say "Restore probe failed: $VENV_BAK/bin/python not executable"
      say "Recovery: the backup venv is not usable. Manual intervention required."
      say "  - Check $VENV_BAK for corruption"
      say "  - Or manually start the old version"
      # Skip to final cleanup, preserve exit code
    else
      # Destructive restore
      if rm -rf "$VENV" 2>/dev/null && mv "$VENV_BAK" "$VENV" 2>/dev/null; then
        say "Restored venv from backup"
        
        # Restart old version via shimPath (use HOME_DIR since manifest is at HOME_DIR/manifest.json)
        # Read shimPath from manifest using python with explicit env var passing
        SHIM_PATH=""
        export MANIFEST_PATH="$HOME_DIR/manifest.json"
        SHIM_PATH="$(python3 -c 'import json,os; print(json.load(open(os.environ["MANIFEST_PATH"]))["shimPath"])')" 2>/dev/null || SHIM_PATH=""
        unset MANIFEST_PATH
        if [ -n "$SHIM_PATH" ] && [ -x "$SHIM_PATH" ]; then
          say "Restarting old version via $SHIM_PATH..."
          nohup "$SHIM_PATH" >>"$HOME_DIR/anthrouter.out" 2>&1 &
          local RESTORE_PID=$!
          
          # Re-poll health (shorter timeout for restore: 2s instead of 10s)
          say "Waiting for health check on restored version..."
          # Source config.env if it exists (needed for tests that set custom ports)
          if [ -f "$CONFIG_ENV" ]; then
            # shellcheck source=/dev/null
            . "$CONFIG_ENV"
          fi
          local RESTORE_HOST="${ANTHROUTER_HOST:-127.0.0.1}"
          local RESTORE_PORT="${ANTHROUTER_PORT:-8083}"
          local HEALTHY=0
          for _ in $(seq 1 4); do
            if curl -fsS -o /dev/null -w '%{http_code}' "http://$RESTORE_HOST:$RESTORE_PORT/health" 2>/dev/null | grep -qE '^2[0-9][0-9]$'; then
              HEALTHY=1
              break
            fi
            kill -0 "$RESTORE_PID" 2>/dev/null || break
            sleep 0.5
          done
          
          if [ "$HEALTHY" = "1" ]; then
            say "Restored version is healthy"
          else
            say "Health check failed on restored version"
            say "Recovery: manually investigate $HOME_DIR/anthrouter.log"
          fi
        else
          say "Could not read shimPath from manifest or shim not executable"
          say "Recovery: manually start the old version"
        fi
      else
        say "Restore failed: could not restore venv"
        say "Recovery: manually run: rm -rf $VENV && mv $VENV_BAK $VENV"
      fi
    fi
  fi

  # Always clean up lock and temp dir (success or any failure path)
  if [ "$LOCK_CREATED" = "1" ]; then
    rmdir "$LOCK_DIR" 2>/dev/null || true
  fi
  if [ -n "$TMPDIR_CLONE" ] && [ -d "$TMPDIR_CLONE" ]; then
    rm -rf "$TMPDIR_CLONE"
  fi
}
trap cleanup EXIT

# ============================================================ Step 1: Pre-flight ===

say "Running pre-flight checks..."

# Check manifest exists and completed
if [ ! -f "$MANIFEST" ]; then
  die "anthrouter is not installed — run install.sh first ($MANIFEST not found)"
fi

# Check completed: true
COMPLETED="$(MANIFEST_PATH="$MANIFEST" python3 -c 'import json, os; print(json.load(open(os.environ["MANIFEST_PATH"])).get("completed", False))')" || {
  die "Could not read completed field from manifest"
}
if [ "$COMPLETED" != "True" ]; then
  die "Install not completed (completed=$COMPLETED) — run install.sh first"
fi

# Check upstreamUrl present
UPSTREAM_URL=""
UPSTREAM_URL="$(MANIFEST_PATH="$MANIFEST" python3 -c 'import json, os; d = json.load(open(os.environ["MANIFEST_PATH"])); print(d.get("upstreamUrl", ""))')" || {
  die "Could not read upstreamUrl from manifest"
}
if [ -z "$UPSTREAM_URL" ]; then
  die "upstreamUrl not found in manifest — add it manually (e.g. MANIFEST_PATH=\"$MANIFEST\" python3 -c 'import json, os; p = os.environ[\"MANIFEST_PATH\"]; d = json.load(open(p)); d[\"upstreamUrl\"] = \"<your-repo-url>\"; json.dump(d, open(p, \"w\"), indent=2)') — install.sh cannot be re-run over an existing install per ADR 0001"
fi

# Check shimPath present and executable
SHIM_PATH=""
SHIM_PATH="$(MANIFEST_PATH="$MANIFEST" python3 -c 'import json, os; print(json.load(open(os.environ["MANIFEST_PATH"])).get("shimPath", ""))')" || {
  die "Could not read shimPath from manifest"
}
if [ -z "$SHIM_PATH" ]; then
  die "shimPath not found in manifest"
fi
if [ ! -f "$SHIM_PATH" ]; then
  die "shimPath not found: $SHIM_PATH"
fi
if [ ! -x "$SHIM_PATH" ]; then
  die "shimPath not executable: $SHIM_PATH"
fi

# Check for prior venv.bak (reuse logic)
if [ -d "$VENV_BAK" ]; then
  say "Prior venv.bak found — reusing as restore point"
  BAK_SIZE="$(du -sh "$VENV_BAK" 2>/dev/null | cut -f1 || echo "unknown")"
  say "venv.bak size: ~$BAK_SIZE"
else
  say "No prior venv.bak — will create backup"
fi

# Concurrency lock (atomic mkdir)
if [ -d "$LOCK_DIR" ]; then
  die "Another update is in progress (lock exists at $LOCK_DIR)"
fi
mkdir "$LOCK_DIR" || die "Could not create lock at $LOCK_DIR"
LOCK_CREATED=1

# ============================================================ Step 2: Stop ===

say "Stopping anthrouter..."
PID=""
PID="$(MANIFEST_PATH="$MANIFEST" python3 -c 'import json, os; print(json.load(open(os.environ["MANIFEST_PATH"])).get("pid", ""))')" || true

if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
  # Verify process name matches anthrouter
  PROC_NAME="$(ps -p "$PID" -o comm= 2>/dev/null || true)"
  if [[ "$PROC_NAME" == *"anthrouter"* ]]; then
    say "Sending SIGTERM to pid $PID..."
    kill "$PID" 2>/dev/null || true
    
    # Wait up to 10s (0.5s polls)
    for _ in $(seq 1 20); do
      if ! kill -0 "$PID" 2>/dev/null; then
        break
      fi
      sleep 0.5
    done
    
    # SIGKILL if still alive
    if kill -0 "$PID" 2>/dev/null; then
      say "Process still alive — sending SIGKILL..."
      kill -9 "$PID" 2>/dev/null || true
      sleep 0.5
    fi
  else
    say "Process $PID not named anthrouter (name: $PROC_NAME) — skipping stop"
  fi
else
  say "No running process found at pid $PID — skipping stop"
fi

# ============================================================ Step 3: Backup ===

if [ ! -d "$VENV_BAK" ]; then
  say "Backing up venv to venv.bak..."
  if ! cp -R "$VENV" "$VENV_BAK"; then
    say "Backup failed"
    BACKUP_FAILED=1
    # Exit without setting FAILED=1 — cleanup will just remove lock and temp dir
    # The original venv is still intact, no restore needed
    exit 1
  fi
else
  say "Skipping backup — venv.bak already exists"
fi

# ============================================================ Step 4: Fetch ===

say "Fetching update from $UPSTREAM_URL..."
TMPDIR_CLONE="$(mktemp -d)" || { FAILED=1; exit 1; }

git clone --depth 1 "$UPSTREAM_URL" "$TMPDIR_CLONE" 2>/dev/null || { FAILED=1; exit 1; }

# ============================================================ Step 5: Upgrade ===

say "Upgrading venv from clone..."
"$VENV/bin/pip" install --upgrade "$TMPDIR_CLONE" >/dev/null 2>&1 || { FAILED=1; exit 1; }

# Self-update: copy update.sh from clone
if [ -f "$TMPDIR_CLONE/update.sh" ]; then
  cp "$TMPDIR_CLONE/update.sh" "$HOME_DIR/update.sh" || { FAILED=1; exit 1; }
  chmod +x "$HOME_DIR/update.sh" || { FAILED=1; exit 1; }
  say "Self-updated update.sh"
fi

# ============================================================ Step 6: Restart ===

say "Restarting anthrouter via $SHIM_PATH..."
nohup "$SHIM_PATH" >>"$HOME_DIR/anthrouter.out" 2>&1 &
NEW_PID=$!

# Record new PID in manifest (atomic write)
export MANIFEST_PATH="$MANIFEST"
export NEW_PID="$NEW_PID"
python3 -c '
import json, os, tempfile

p = os.environ["MANIFEST_PATH"]
d = json.load(open(p))
d["pid"] = int(os.environ["NEW_PID"])

# Atomic write: write to .tmp then os.replace()
tmp_path = p + ".tmp"
with open(tmp_path, "w") as f:
    json.dump(d, f, indent=2)
os.replace(tmp_path, p)
' || { FAILED=1; exit 1; }

# ============================================================ Step 7: Health ===

say "Waiting for health check..."
source "$CONFIG_ENV" 2>/dev/null || true
HOST="${ANTHROUTER_HOST:-127.0.0.1}"
PORT="${ANTHROUTER_PORT:-8083}"

HEALTHY=0
for _ in $(seq 1 20); do
  HTTP_CODE="$(curl -fsS -o /dev/null -w '%{http_code}' "http://$HOST:$PORT/health" 2>/dev/null || true)"
  if [[ "$HTTP_CODE" =~ ^2[0-9][0-9]$ ]]; then
    HEALTHY=1
    break
  fi
  kill -0 "$NEW_PID" 2>/dev/null || {
    say "Process $NEW_PID died during health check"
    FAILED=1
    exit 1
  }
  sleep 0.5
done

if [ "$HEALTHY" = "0" ]; then
  say "Health check failed after 10s"
  FAILED=1
  exit 1
fi

# ============================================================ Step 8: Success ===

say "Health check passed — update successful"

# Remove venv.bak
rm -rf "$VENV_BAK" || {
  say "Warning: could not remove venv.bak"
}

# Stamp manifest updatedAt (atomic write)
export UPDATED_AT="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
python3 -c '
import json, os, tempfile

p = os.environ["MANIFEST_PATH"]
d = json.load(open(p))
d["updatedAt"] = os.environ["UPDATED_AT"]

# Atomic write: write to .tmp then os.replace()
tmp_path = p + ".tmp"
with open(tmp_path, "w") as f:
    json.dump(d, f, indent=2)
os.replace(tmp_path, p)
' || {
  say "Warning: could not stamp updatedAt in manifest"
}

say "Summary:"
say "  - Updated from: $UPSTREAM_URL"
say "  - New pid: $NEW_PID"
say "  - Health: http://$HOST:$PORT/health"
say "  - Config: $CONFIG_ENV"
say "  - Logs: $HOME_DIR/anthrouter.log"

# ============================================================ Done ===

say "Update complete"
say "Update:    $HOME_DIR/update.sh"
