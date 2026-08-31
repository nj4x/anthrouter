#!/usr/bin/env bash
# anthrouter uninstaller.
#
# Stops anthrouter, reverses every config mutation recorded in
# ~/.anthrouter/manifest.json, and removes the venv and launcher shim.
# Config and the request DB survive unless --purge is passed.
#
# Idempotent by construction: each restore reads the live file or the manifest,
# never state consumed by an earlier run, so a failed uninstall is corrected by
# re-running it.
set -euo pipefail

PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

ANTHROUTER_URL="http://127.0.0.1:8083"
ANTHROUTER_HOSTPORT="127.0.0.1:8083"
BACKUP_KEY="ANTHROPIC_BASE_URL_ANTHROUTER_BACKUP"
BLOCK_BEGIN="# >>> anthrouter >>>"
BLOCK_END="# <<< anthrouter <<<"

HOME_DIR="${ANTHROUTER_HOME:-$HOME/.anthrouter}"
MANIFEST="$HOME_DIR/manifest.json"

say() { echo "[anthrouter] $*"; }

# The script may live inside the directory --purge deletes; run from a copy so
# bash is not reading a file that vanishes mid-run.
if [ "${ANTHROUTER_UNINSTALL_REEXEC:-}" != "1" ] \
   && [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SELF="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/$(basename "${BASH_SOURCE[0]}")"
  case "$SELF" in
    "$HOME_DIR"/*)
      TMP_SELF="$(mktemp -t anthrouter-uninstall)"
      cp "$SELF" "$TMP_SELF"
      ANTHROUTER_UNINSTALL_REEXEC=1 bash "$TMP_SELF" "$@"
      rc=$?
      rm -f "$TMP_SELF"
      exit $rc
      ;;
  esac
fi

if [ ! -d "$HOME_DIR" ]; then
  say "nothing to do — $HOME_DIR does not exist"
  exit 0
fi

# bash 3.2 cannot parse a heredoc inside $(...); python steps write here.
STEP_OUT="$(mktemp -t anthrouter-step)"
trap 'rm -f "$STEP_OUT"' EXIT

mf_get() {  # mf_get key — prints the value, empty for null/absent
  [ -f "$MANIFEST" ] || return 0
  MF_PATH="$MANIFEST" python3 - "$1" <<'PY'
import json, os, sys
try:
    with open(os.environ['MF_PATH']) as fh:
        data = json.load(fh)
except (OSError, ValueError):
    raise SystemExit(0)
value = data.get(sys.argv[1])
if value is not None:
    print(value)
PY
}

if [ ! -f "$MANIFEST" ]; then
  say "warning: $MANIFEST missing — cannot reverse config changes; cleaning up files only" >&2
fi

# ---------------------------------------------------------------- stop proxy ---

PID="$(mf_get pid)"
if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
  if ps -p "$PID" -o command= 2>/dev/null | grep -q anthrouter; then
    say "stopping anthrouter (pid $PID)..."
    kill "$PID" 2>/dev/null || true
  else
    say "warning: pid $PID is no longer anthrouter — not killing it" >&2
  fi
fi

# -------------------------------------------------------------- settings.json ---

SETTINGS="$(mf_get claudeSettings)"
SETTINGS_SLOT="$(mf_get settingsSlot)"
if [ -n "$SETTINGS" ] && [ -f "$SETTINGS" ]; then
  SETTINGS_PATH="$SETTINGS" python3 - "$BACKUP_KEY" "$ANTHROUTER_URL" "$SETTINGS_SLOT" > "$STEP_OUT" <<'PY'
import json, os, sys

path = os.environ['SETTINGS_PATH']
backup_key, our_url, slot = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as fh:
    data = json.load(fh)
env = data.get('env', {})

if backup_key in env:
    env['ANTHROPIC_BASE_URL'] = env.pop(backup_key)
    result = 'restored ' + env['ANTHROPIC_BASE_URL']
elif slot == 'created' and env.get('ANTHROPIC_BASE_URL') == our_url:
    env.pop('ANTHROPIC_BASE_URL')
    result = 'removed'
else:
    raise SystemExit(0)

tmp = path + '.tmp'
with open(tmp, 'w') as fh:
    json.dump(data, fh, indent=2)
    fh.write('\n')
os.replace(tmp, path)
print(result)
PY
  RESULT="$(cat "$STEP_OUT")"
  [ -n "$RESULT" ] && say "$SETTINGS: ANTHROPIC_BASE_URL $RESULT"
fi

# -------------------------------------------------------------- caveman.yaml ---

CAVEMAN_FILE="$(mf_get cavemanConfig)"
CAVEMAN_SLOT="$(mf_get cavemanSlot)"
CAVEMAN_PREV="$(mf_get cavemanPreviousUrl)"
if [ -n "$CAVEMAN_FILE" ] && [ -f "$CAVEMAN_FILE" ] && [ -n "$CAVEMAN_SLOT" ]; then
  CAVEMAN_PATH="$CAVEMAN_FILE" python3 - "$CAVEMAN_SLOT" "$CAVEMAN_PREV" > "$STEP_OUT" <<'PY'
import os, re, sys

path = os.environ['CAVEMAN_PATH']
slot, previous = sys.argv[1], sys.argv[2]
with open(path) as fh:
    lines = fh.read().splitlines()


def indent_of(line):
    return len(line) - len(line.lstrip(' '))


def block_end(start, parent_indent):
    for i in range(start, len(lines)):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if indent_of(line) <= parent_indent:
            return i
    return len(lines)


def find_key(start, end, key, parent_indent):
    for i in range(start, end):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if indent_of(line) == parent_indent + 2 and re.match(rf'\s*{key}\s*:', line):
            return i
    return None


providers = find_key(0, len(lines), 'providers', -2)
if providers is None:
    raise SystemExit(0)
p_end = block_end(providers + 1, indent_of(lines[providers]))
anthropic = find_key(providers + 1, p_end, 'anthropic', indent_of(lines[providers]))
if anthropic is None:
    raise SystemExit(0)
a_end = block_end(anthropic + 1, indent_of(lines[anthropic]))
base_url = find_key(anthropic + 1, a_end, 'base_url', indent_of(lines[anthropic]))
if base_url is None:
    raise SystemExit(0)

if slot == 'backed_up':
    lines[base_url] = ' ' * indent_of(lines[base_url]) + f'base_url: {previous}'
    result = 'restored ' + previous
else:
    del lines[base_url]
    # Drop the containers too if we left them empty.
    a_end = block_end(anthropic + 1, indent_of(lines[anthropic]))
    if a_end == anthropic + 1:
        del lines[anthropic]
        p_end = block_end(providers + 1, indent_of(lines[providers]))
        if p_end == providers + 1:
            del lines[providers]
    result = 'removed'

tmp = path + '.tmp'
with open(tmp, 'w') as fh:
    fh.write('\n'.join(lines) + '\n' if lines else '')
os.replace(tmp, path)
print(result)
PY
  RESULT="$(cat "$STEP_OUT")"
  [ -n "$RESULT" ] && say "$CAVEMAN_FILE: providers.anthropic.base_url $RESULT"
fi

# ------------------------------------------------------- shell-profile allowlist ---

PROFILE_FILE="$(mf_get profileFile)"
ALLOWLIST_PATH="$(mf_get allowlistInstallPath)"
if [ -n "$PROFILE_FILE" ] && [ -f "$PROFILE_FILE" ] \
   && { [ "$ALLOWLIST_PATH" = "append" ] || [ "$ALLOWLIST_PATH" = "new_block" ]; }; then
  PROFILE_PATH="$PROFILE_FILE" python3 - "$ALLOWLIST_PATH" "$ANTHROUTER_HOSTPORT" "$BLOCK_BEGIN" "$BLOCK_END" > "$STEP_OUT" <<'PY'
import os, re, sys

path = os.environ['PROFILE_PATH']
mode, hostport, begin, end = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]
with open(path) as fh:
    lines = fh.read().splitlines()

if mode == 'new_block':
    try:
        i, j = lines.index(begin), lines.index(end)
    except ValueError:
        raise SystemExit(0)
    del lines[i:j + 1]
    result = 'block removed'
else:
    pattern = re.compile(r'^\s*export\s+CAVE_SSRF_ALLOWLIST\s*=\s*(.*)$')
    idx = None
    for i, line in enumerate(lines):
        if pattern.match(line):
            idx = i
    if idx is None:
        raise SystemExit(0)
    value = pattern.match(lines[idx]).group(1).strip().strip('"\'')
    entries = [e for e in (p.strip() for p in value.split(',')) if e]
    if hostport not in entries:
        raise SystemExit(0)
    entries.remove(hostport)
    if entries:
        lines[idx] = f'export CAVE_SSRF_ALLOWLIST="{",".join(entries)}"'
    else:
        del lines[idx]
    result = 'entry removed'

tmp = path + '.tmp'
with open(tmp, 'w') as fh:
    fh.write('\n'.join(lines) + '\n' if lines else '')
os.replace(tmp, path)
print(result)
PY
  RESULT="$(cat "$STEP_OUT")"
  [ -n "$RESULT" ] && say "$PROFILE_FILE: CAVE_SSRF_ALLOWLIST $RESULT"
fi

# ------------------------------------------------------------------- files ---

SHIM="$(mf_get shimPath)"
if [ -n "$SHIM" ] && [ -f "$SHIM" ] && grep -q "$HOME_DIR" "$SHIM"; then
  rm -f "$SHIM"
  say "removed $SHIM"
fi

rm -rf "$HOME_DIR/venv" "$HOME_DIR/src" "$HOME_DIR/backup"
rm -f "$MANIFEST"

if [ "$PURGE" = "1" ]; then
  cd /
  rm -rf "$HOME_DIR"
  say "purged $HOME_DIR"
else
  say "removed venv and shim; config and request DB kept in $HOME_DIR (--purge removes them)"
fi

echo
say "uninstalled. Restart Claude Code (and caveman, if it was in the chain)."
