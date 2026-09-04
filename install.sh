#!/usr/bin/env bash
# anthrouter installer.
#
# Creates a venv under ~/.anthrouter, installs anthrouter into it, drops an
# `anthrouter` launcher shim on PATH, starts the proxy, and only then wires it
# into the live Claude Code setup. Two topologies are recognized (ADR-0001):
#
#   direct  — ~/.claude/settings.json has no ANTHROPIC_BASE_URL:
#             the installer points it at anthrouter.
#   chained — settings.json already points at caveman:
#             settings.json is left alone, caveman.yaml's anthropic slot is
#             pointed at anthrouter and the shell profile's CAVE_SSRF_ALLOWLIST
#             gains anthrouter's host:port.
#
# Any other ANTHROPIC_BASE_URL value aborts without touching anything.
# Every mutation is recorded in ~/.anthrouter/manifest.json before it is made,
# so uninstall.sh can reverse it. A failure mid-install rolls back via
# uninstall.sh.
set -euo pipefail

ANTHROUTER_URL="http://127.0.0.1:8083"
ANTHROUTER_HOSTPORT="127.0.0.1:8083"
CAVEMAN_URL="http://127.0.0.1:8787/w/claude"
BACKUP_KEY="ANTHROPIC_BASE_URL_ANTHROUTER_BACKUP"
REPO_URL="https://github.com/nj4x/anthrouter.git"
BLOCK_BEGIN="# >>> anthrouter >>>"
BLOCK_END="# <<< anthrouter <<<"

HOME_DIR="${ANTHROUTER_HOME:-$HOME/.anthrouter}"
BACKUP_DIR="$HOME_DIR/backup"
MANIFEST="$HOME_DIR/manifest.json"
VENV="$HOME_DIR/venv"
CONFIG_ENV="$HOME_DIR/config.env"
SRC_DIR="$HOME_DIR/src"
SHIM_DIR="${ANTHROUTER_SHIM_DIR:-$HOME/.local/bin}"
SHIM="$SHIM_DIR/anthrouter"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
SETTINGS="$CLAUDE_DIR/settings.json"
CAVEMAN_CONFIG_FILE="${CAVEMAN_CONFIG:-$HOME/.caveman/caveman.yaml}"

PID=""
MUTATING=0
HOME_PREEXISTED=0
[ -d "$HOME_DIR" ] && HOME_PREEXISTED=1
# bash 3.2 (the macOS system bash, and what `curl | bash` runs) cannot parse a
# heredoc inside $(...), so python steps write here and bash reads it back.
STEP_OUT="$(mktemp -t anthrouter-step)"
trap 'rm -f "$STEP_OUT"' EXIT

say() { echo "[anthrouter] $*"; }
die() {
  echo "[anthrouter] error: $*" >&2
  [ "$MUTATING" = "1" ] && rollback
  exit 1
}

# Paths flow into python argv and generated shell files; a quote or backslash
# would corrupt the shim and the manifest.
for _pv in "HOME=$HOME" "ANTHROUTER_HOME=$HOME_DIR" "CLAUDE_DIR=$CLAUDE_DIR"; do
  case "${_pv#*=}" in
    *\"*|*\\*|*\'*) die "${_pv%%=*} contains a quote or backslash — unsupported path: ${_pv#*=}" ;;
  esac
done
unset _pv

# ---------------------------------------------------------------- manifest ---

mf_write() {  # mf_write key json-encoded-value
  MF_PATH="$MANIFEST" python3 - "$1" "$2" <<'PY'
import json, os, sys
path = os.environ['MF_PATH']
key, raw = sys.argv[1], sys.argv[2]
try:
    with open(path) as fh:
        data = json.load(fh)
except (OSError, ValueError):
    data = {}
data[key] = json.loads(raw)
tmp = path + '.tmp'
with open(tmp, 'w') as fh:
    json.dump(data, fh, indent=2)
    fh.write('\n')
os.replace(tmp, path)
PY
}

mf_set() {  # mf_set key string-value
  mf_write "$1" "$(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$2")"
}

# ---------------------------------------------------------------- rollback ---

rollback() {
  echo "[anthrouter] install failed — rolling back..." >&2
  if [ -n "$PID" ] && kill -0 "$PID" 2>/dev/null; then
    kill "$PID" 2>/dev/null || true
  fi
  if [ -f "$HOME_DIR/uninstall.sh" ]; then
    bash "$HOME_DIR/uninstall.sh" >&2 \
      || echo "[anthrouter] warning: rollback failed — inspect $HOME_DIR manually" >&2
  fi
  # A home directory that predates this run may hold a config or DB the user
  # wants back on the next attempt; only a directory this run created is removed.
  [ "$HOME_PREEXISTED" = "0" ] && rm -rf "$HOME_DIR"
}
trap rollback ERR

# --------------------------------------------------------------- re-run guard ---

if [ -f "$MANIFEST" ]; then
  die "anthrouter is already installed at $HOME_DIR. Run $HOME_DIR/uninstall.sh first (it is idempotent, and also clears a partial install)."
fi

# ------------------------------------------------------------- prerequisites ---

say "Checking prerequisites..."

command -v python3 >/dev/null 2>&1 || die "python3 not found on PATH"
command -v curl >/dev/null 2>&1 || die "curl not found on PATH"
python3 - <<'PY' || die "python3 is older than 3.10"
import sys
sys.exit(0 if sys.version_info >= (3, 10) else 1)
PY

# --------------------------------------------------------- source resolution ---

SCRIPT_DIR=""
if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
  SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
fi

SOURCE_MODE="clone"
if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/pyproject.toml" ] && [ -d "$SCRIPT_DIR/anthrouter" ]; then
  SOURCE_MODE="local"
fi

# --------------------------------------------------------- argument parsing ---

BROWSER_OPEN=1
while [ $# -gt 0 ]; do
  case "$1" in
    --no-browser) BROWSER_OPEN=0 ;;
    *) die "unknown argument: $1" ;;
  esac
  shift
done

# Resolve the upstream URL now so a bad checkout aborts before any mutation.
if [ "$SOURCE_MODE" = "clone" ]; then
  UPSTREAM_URL="$REPO_URL"
else
  UPSTREAM_URL="$(git -C "$SCRIPT_DIR" remote get-url origin 2>/dev/null)" \
    || die "Cannot determine upstream URL: '$SCRIPT_DIR' has no 'origin' remote. Add an origin remote and re-run install.sh."
fi

# ------------------------------------------------------------- pre-flight ---

say "Running pre-flight checks..."

mkdir -p "$CLAUDE_DIR"
[ -f "$SETTINGS" ] || echo '{}' > "$SETTINGS"

SETTINGS_PATH="$SETTINGS" python3 - "$CAVEMAN_URL" "$BACKUP_KEY" > "$STEP_OUT" <<'PY' || die "could not read $SETTINGS — is it valid JSON?"
import json, os, sys
caveman_url, backup_key = sys.argv[1], sys.argv[2]
with open(os.environ['SETTINGS_PATH']) as fh:
    data = json.load(fh)
env = data.get('env', {})
if backup_key in env:
    print('orphan')
    raise SystemExit(0)
current = env.get('ANTHROPIC_BASE_URL')
if current is None or current == '':
    print('direct')
elif current == caveman_url:
    print('chained')
else:
    print('third:' + current)
PY
TOPOLOGY="$(cat "$STEP_OUT")"

case "$TOPOLOGY" in
  orphan)
    die "$SETTINGS holds $BACKUP_KEY but no install manifest exists at $MANIFEST. A prior install was interrupted — remove that key by hand (restoring its value to ANTHROPIC_BASE_URL if it is the one you want) and re-run."
    ;;
  third:*)
    echo "[anthrouter] error: $SETTINGS already sets ANTHROPIC_BASE_URL to a value anthrouter does not recognize:" >&2
    echo "    ${TOPOLOGY#third:}" >&2
    echo "Recognized: absent (direct install) or $CAVEMAN_URL (install behind caveman)." >&2
    echo "Nothing was changed. Point that key at one of those, or remove it, then re-run." >&2
    exit 1
    ;;
esac

say "Topology: $TOPOLOGY"

PROFILE_FILE=""
case "${SHELL:-}" in
  */zsh)  PROFILE_FILE="$HOME/.zshrc" ;;
  */bash) PROFILE_FILE="$HOME/.bashrc" ;;
esac

if [ "$TOPOLOGY" = "chained" ]; then
  [ -f "$CAVEMAN_CONFIG_FILE" ] \
    || die "topology is chained (settings.json points at caveman) but $CAVEMAN_CONFIG_FILE does not exist. Set CAVEMAN_CONFIG if caveman's config lives elsewhere."
  [ -n "$PROFILE_FILE" ] \
    || die "unsupported shell: ${SHELL:-unset}. The chained install edits CAVE_SSRF_ALLOWLIST in a bash or zsh profile; add $ANTHROUTER_HOSTPORT to that variable by hand for other shells."
fi

python3 - "$ANTHROUTER_HOSTPORT" <<'PY' || die "port 8083 is already in use — stop whatever is listening on $ANTHROUTER_HOSTPORT and re-run"
import socket, sys
host, port = sys.argv[1].split(':')
s = socket.socket()
# Match ThreadingHTTPServer's allow_reuse_address, or a socket still in
# TIME_WAIT from a just-stopped anthrouter reads as an occupied port.
s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
try:
    s.bind((host, int(port)))
except OSError:
    raise SystemExit(1)
finally:
    s.close()
PY

# ------------------------------------------------------------------ install ---

say "Creating $HOME_DIR..."
mkdir -p "$HOME_DIR" "$BACKUP_DIR"
MUTATING=1

  if [ "$SOURCE_MODE" = "local" ]; then
    INSTALL_SRC="$SCRIPT_DIR"
    cp "$SCRIPT_DIR/uninstall.sh" "$HOME_DIR/uninstall.sh"
    cp "$SCRIPT_DIR/update.sh" "$HOME_DIR/update.sh"
  else
    command -v git >/dev/null 2>&1 || die "git not found on PATH (needed to fetch anthrouter)"
    say "Cloning $REPO_URL..."
    git clone --depth 1 "$REPO_URL" "$SRC_DIR" >/dev/null 2>&1 \
      || die "could not clone $REPO_URL"
    INSTALL_SRC="$SRC_DIR"
    cp "$SRC_DIR/uninstall.sh" "$HOME_DIR/uninstall.sh"
    cp "$SRC_DIR/update.sh" "$HOME_DIR/update.sh"
  fi
  chmod +x "$HOME_DIR/uninstall.sh"
  chmod +x "$HOME_DIR/update.sh"

mf_write installedAt "\"$(date -u +%Y-%m-%dT%H:%M:%SZ)\""
mf_set home "$HOME_DIR"
mf_set topology "$TOPOLOGY"
mf_set sourceMode "$SOURCE_MODE"
mf_set upstreamUrl "$UPSTREAM_URL"
mf_set claudeSettings "$SETTINGS"
mf_write settingsSlot null
mf_write cavemanSlot null
mf_write cavemanPreviousUrl null
mf_write profileFile null
mf_write allowlistInstallPath null
mf_write shimPath null
mf_write pid null
mf_write completed false

say "Creating virtualenv..."
python3 -m venv "$VENV"
"$VENV/bin/python" -m pip install --quiet --upgrade pip
say "Installing anthrouter from $INSTALL_SRC..."
"$VENV/bin/python" -m pip install --quiet "$INSTALL_SRC"

if [ ! -f "$CONFIG_ENV" ]; then
  say "Writing default config to $CONFIG_ENV..."
  cat > "$CONFIG_ENV" <<CONF
# anthrouter configuration. Sourced by the launcher shim; every setting here
# has a matching --flag (see \`anthrouter --help\`).
ANTHROUTER_HOST=127.0.0.1
ANTHROUTER_PORT=8083
ANTHROUTER_UPSTREAM_BASE_URL=https://api.anthropic.com
ANTHROUTER_SANITIZE_SYSTEM_PROMPT=strip
ANTHROUTER_AUTO_MODEL_ROUTING=1
ANTHROUTER_DB_PATH=$HOME_DIR/anthrouter.db
ANTHROUTER_DB_RETENTION_DAYS=30
ANTHROUTER_LOG_FILE=$HOME_DIR/anthrouter.log
# ANTHROUTER_MODEL_ALIASES=opus:claude-opus-5,sonnet:claude-sonnet-4-6
# Flags with no environment equivalent, word-split into the command line.
ANTHROUTER_ARGS="--enable-ui"
CONF
else
  say "Keeping existing config at $CONFIG_ENV"
fi

say "Installing launcher shim at $SHIM..."
mkdir -p "$SHIM_DIR"
cat > "$SHIM" <<SHIMEOF
#!/usr/bin/env bash
set -euo pipefail
if [ -f "$CONFIG_ENV" ]; then
  set -a
  . "$CONFIG_ENV"
  set +a
fi
eval "set -- \${ANTHROUTER_ARGS:-} \"\\\$@\""
exec "$VENV/bin/anthrouter" "\$@"
SHIMEOF
chmod +x "$SHIM"
mf_set shimPath "$SHIM"

# -------------------------------------------------------------------- start ---

say "Starting anthrouter..."
nohup "$SHIM" >>"$HOME_DIR/anthrouter.out" 2>&1 &
PID=$!
mf_write pid "$PID"

say "Waiting for $ANTHROUTER_URL/health..."
HEALTHY=0
for _ in $(seq 1 20); do
  if [ -n "$(curl -fsS -o /dev/null -w '%{http_code}' "$ANTHROUTER_URL/health" 2>/dev/null | grep -E '^2[0-9][0-9]$' || true)" ]; then
    HEALTHY=1
    break
  fi
  kill -0 "$PID" 2>/dev/null || break
  sleep 0.5
done
[ "$HEALTHY" = "1" ] || die "anthrouter did not answer $ANTHROUTER_URL/health within 10s — see $HOME_DIR/anthrouter.out"
say "anthrouter is healthy (pid $PID)"

# ------------------------------------------------------------- config wiring ---

if [ "$TOPOLOGY" = "direct" ]; then
  say "Pointing $SETTINGS at anthrouter..."
  mf_set settingsSlot created
  SETTINGS_PATH="$SETTINGS" python3 - "$ANTHROUTER_URL" <<'PY'
import json, os, sys
path = os.environ['SETTINGS_PATH']
with open(path) as fh:
    data = json.load(fh)
data.setdefault('env', {})['ANTHROPIC_BASE_URL'] = sys.argv[1]
tmp = path + '.tmp'
with open(tmp, 'w') as fh:
    json.dump(data, fh, indent=2)
    fh.write('\n')
os.replace(tmp, path)
PY
else
  say "Pointing caveman's anthropic slot at anthrouter..."
  CAVEMAN_PATH="$CAVEMAN_CONFIG_FILE" python3 - "$ANTHROUTER_URL" > "$STEP_OUT" <<'PY'
import os, re, sys

path = os.environ['CAVEMAN_PATH']
new_url = sys.argv[1]
with open(path) as fh:
    lines = fh.read().splitlines()


def indent_of(line):
    return len(line) - len(line.lstrip(' '))


def find_key(start, end, key, parent_indent):
    for i in range(start, end):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        ind = indent_of(line)
        if ind <= parent_indent:
            return None, i
        if ind == parent_indent + 2 and re.match(rf'\s*{key}\s*:', line):
            return i, end
    return None, end


def block_end(start, parent_indent):
    for i in range(start, len(lines)):
        line = lines[i]
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if indent_of(line) <= parent_indent:
            return i
    return len(lines)


providers, _ = find_key(0, len(lines), 'providers', -2)
if providers is None:
    lines += ['providers:', '  anthropic:', f'    base_url: {new_url}']
    result = 'created'
else:
    p_end = block_end(providers + 1, indent_of(lines[providers]))
    anthropic, _ = find_key(providers + 1, p_end, 'anthropic',
                            indent_of(lines[providers]))
    if anthropic is None:
        lines.insert(p_end, '  anthropic:')
        lines.insert(p_end + 1, f'    base_url: {new_url}')
        result = 'created'
    else:
        a_end = block_end(anthropic + 1, indent_of(lines[anthropic]))
        base_url, _ = find_key(anthropic + 1, a_end, 'base_url',
                               indent_of(lines[anthropic]))
        if base_url is None:
            lines.insert(a_end, ' ' * (indent_of(lines[anthropic]) + 2)
                         + f'base_url: {new_url}')
            result = 'created'
        else:
            old = lines[base_url].split(':', 1)[1].strip().strip('"\'')
            lines[base_url] = (' ' * indent_of(lines[base_url])
                               + f'base_url: {new_url}')
            result = 'backed_up\n' + old

tmp = path + '.tmp'
with open(tmp, 'w') as fh:
    fh.write('\n'.join(lines) + '\n')
os.replace(tmp, path)
print(result)
PY
  SLOT="$(cat "$STEP_OUT")"
  # The mutation is written by the same helper that reports it, so the manifest
  # is updated immediately after rather than before: an uninstall run against a
  # crash between the two would leave the slot pointed at anthrouter.
  SLOT_KIND="$(printf '%s\n' "$SLOT" | head -1)"
  if [ "$SLOT_KIND" = "backed_up" ]; then
    mf_set cavemanSlot backed_up
    mf_set cavemanPreviousUrl "$(printf '%s\n' "$SLOT" | tail -n +2)"
  else
    mf_set cavemanSlot created
  fi
  mf_set cavemanConfig "$CAVEMAN_CONFIG_FILE"

  say "Adding $ANTHROUTER_HOSTPORT to CAVE_SSRF_ALLOWLIST in $PROFILE_FILE..."
  mf_set profileFile "$PROFILE_FILE"
  touch "$PROFILE_FILE"
  PROFILE_PATH="$PROFILE_FILE" python3 - "$ANTHROUTER_HOSTPORT" "$BLOCK_BEGIN" "$BLOCK_END" > "$STEP_OUT" <<'PY'
import os, re, sys

path = os.environ['PROFILE_PATH']
hostport, begin, end = sys.argv[1], sys.argv[2], sys.argv[3]
with open(path) as fh:
    lines = fh.read().splitlines()

pattern = re.compile(r'^\s*export\s+CAVE_SSRF_ALLOWLIST\s*=\s*(.*)$')
target = None
for i, line in enumerate(lines):
    m = pattern.match(line)
    if m:
        target = (i, m.group(1).strip())

if target is not None:
    idx, raw = target
    value = raw.strip('"\'')
    entries = [e for e in (p.strip() for p in value.split(',')) if e]
    if hostport in entries:
        print('skipped')
        raise SystemExit(0)
    entries.append(hostport)
    lines[idx] = f'export CAVE_SSRF_ALLOWLIST="{",".join(entries)}"'
    result = 'append'
else:
    lines += [begin, f'export CAVE_SSRF_ALLOWLIST="{hostport}"', end]
    result = 'new_block'

tmp = path + '.tmp'
with open(tmp, 'w') as fh:
    fh.write('\n'.join(lines) + '\n')
os.replace(tmp, path)
print(result)
PY
  ALLOWLIST_PATH="$(cat "$STEP_OUT")"
  mf_set allowlistInstallPath "$ALLOWLIST_PATH"
  if [ "$ALLOWLIST_PATH" = "skipped" ]; then
    say "$ANTHROUTER_HOSTPORT was already allowlisted — left as-is"
  fi
fi

# The direct topology does not put caveman in the routing path, so its SSRF
# allowlist is irrelevant.
if [ "$TOPOLOGY" = "direct" ]; then
  say "caveman is not in the routing path — CAVE_SSRF_ALLOWLIST not touched"
fi

# A shell-level ANTHROPIC_BASE_URL governs clients outside both topologies
# (curl, SDK scripts). Reported, never rewritten.
if [ -n "$PROFILE_FILE" ] && [ -f "$PROFILE_FILE" ]; then
  STALE="$(grep -nE '^[^#]*export[[:space:]]+ANTHROPIC_BASE_URL' "$PROFILE_FILE" | tail -1 || true)"
  if [ -n "$STALE" ]; then
    echo "[anthrouter] note: $PROFILE_FILE:${STALE%%:*} exports ANTHROPIC_BASE_URL — left untouched; it governs non-Claude-Code clients." >&2
  fi
fi

trap - ERR
mf_write completed true

echo
say "Installed to $HOME_DIR (topology: $TOPOLOGY)"
say "Running on $ANTHROUTER_URL, pid $PID, UI at $ANTHROUTER_URL/ui/"
say "Config: $CONFIG_ENV   Logs: $HOME_DIR/anthrouter.log"
say "Start/stop by hand: $SHIM   |   kill $PID"
say "Uninstall: $HOME_DIR/uninstall.sh"
say "Update:    $HOME_DIR/update.sh"
case ":$PATH:" in
  *":$SHIM_DIR:"*) ;;
  *) say "note: $SHIM_DIR is not on PATH — add it to run \`anthrouter\` by name" ;;
esac
if [ "$TOPOLOGY" = "chained" ]; then
  say "Restart your shell (or re-source $PROFILE_FILE) so caveman picks up the allowlist, then restart caveman."
else
  say "Restart Claude Code so it picks up the new ANTHROPIC_BASE_URL."
fi

if [ "$BROWSER_OPEN" = "1" ]; then
  if command -v open &>/dev/null; then
    open "$ANTHROUTER_URL/ui/" &
  elif command -v xdg-open &>/dev/null; then
    xdg-open "$ANTHROUTER_URL/ui/" &
  fi
fi
