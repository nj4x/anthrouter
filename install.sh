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
NODE_CA_BACKUP_KEY="NODE_EXTRA_CA_CERTS_ANTHROUTER_BACKUP"
REPO_URL="https://github.com/nj4x/anthrouter.git"
BLOCK_BEGIN="# >>> anthrouter >>>"
BLOCK_END="# <<< anthrouter <<<"

HOME_DIR="${ANTHROUTER_HOME:-$HOME/.anthrouter}"
BACKUP_DIR="$HOME_DIR/backup"
MANIFEST="$HOME_DIR/manifest.json"
VENV="$HOME_DIR/venv"
CONFIG_ENV="$HOME_DIR/config.env"
TLS_DIR="$HOME_DIR/tls"
SRC_DIR="$HOME_DIR/src"
SHIM_DIR="${ANTHROUTER_SHIM_DIR:-$HOME/.local/bin}"
SHIM="$SHIM_DIR/anthrouter"
CLAUDE_DIR="${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
SETTINGS="$CLAUDE_DIR/settings.json"
CAVEMAN_CONFIG_FILE="${CAVEMAN_CONFIG:-$HOME/.caveman/caveman.yaml}"

PID=""
MUTATING=0
SHELL_CA_VALUE=""
SETTINGS_CA_VALUE=""
CA_SOURCES=""
CA_PROFILE_WRITTEN=0
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
# would corrupt the shim and the manifest, and a $ or backtick would expand in
# the double-quoted shell-profile export (ADR-0020).
for _pv in "HOME=$HOME" "ANTHROUTER_HOME=$HOME_DIR" "CLAUDE_DIR=$CLAUDE_DIR"; do
  case "${_pv#*=}" in
    *\"*|*\\*|*\'*|*\$*|*\`*) die "${_pv%%=*} contains a quote, backslash, \$ or backtick — unsupported path: ${_pv#*=}" ;;
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

# ------------------------------------------------------ desktop proxy (ADR-0019) ---
#
# Helpers for wiring Claude Desktop's Code tab through anthrouter's CONNECT
# proxy. Each is a self-contained function so tests can exercise it without a
# full install run (which forks the server).

desktop_app_present() {
  # ANTHROUTER_UNAME / ANTHROUTER_DESKTOP_APPS exist so tests can simulate a
  # Mac with Claude.app on any machine; they are not user-facing knobs.
  [ "${ANTHROUTER_UNAME:-$(uname -s)}" = "Darwin" ] || return 1
  local app _old_ifs="$IFS"
  IFS=':'
  for app in ${ANTHROUTER_DESKTOP_APPS:-/Applications/Claude.app:$HOME/Applications/Claude.app}; do
    if [ -d "$app" ]; then
      IFS="$_old_ifs"
      return 0
    fi
  done
  IFS="$_old_ifs"
  return 1
}

desktop_shell_probe() {  # sets SHELL_PROBE_VALUE: the NODE_EXTRA_CA_CERTS a login, interactive $SHELL ends with
  # ANTHROUTER_SHELL_PROBE / ANTHROUTER_SHELL_PROBE_TIMEOUT exist so tests can
  # stand in a shell; they are not user-facing knobs.
  local shell="${ANTHROUTER_SHELL_PROBE:-${SHELL:-/bin/sh}}" timeout="${ANTHROUTER_SHELL_PROBE_TIMEOUT:-10}" kind
  python3 - "$shell" "$timeout" > "$STEP_OUT" <<'PY' || die "could not run the login-shell probe ($shell -ilc)"
import os, signal, subprocess, sys
shell, timeout = sys.argv[1], int(sys.argv[2])
begin, end = 'ANTHROUTER_PROBE_BEGIN', 'ANTHROUTER_PROBE_END'
# Only the profile may set it: Desktop starts from launchd's environment (checked
# on its own in pre-flight), not from the shell that runs this installer.
env = {k: v for k, v in os.environ.items() if k != 'NODE_EXTRA_CA_CERTS'}
cmd = f"printf '%s%s%s' {begin} \"$NODE_EXTRA_CA_CERTS\" {end}"
# stdin from /dev/null: under `curl | bash` an interactive shell would read the
# rest of this script. A new session keeps it off the terminal's job control.
proc = subprocess.Popen([shell, '-ilc', cmd], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE, env=env, start_new_session=True)
try:
    out, err = proc.communicate(timeout=timeout)
except subprocess.TimeoutExpired:
    os.killpg(proc.pid, signal.SIGKILL)
    try:
        proc.communicate(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    print('timeout')
    raise SystemExit(0)
text = out.decode('utf-8', 'replace')
raw = (text + err.decode('utf-8', 'replace')).replace(begin, '').replace(end, '')
i = text.rfind(begin)
j = text.find(end, i + len(begin)) if i >= 0 else -1
if proc.returncode != 0:
    print(f'exit {proc.returncode}\n{raw}')
elif j < 0:
    print(f'nosentinel\n{raw}')
else:
    sys.stdout.write('ok\n' + text[i + len(begin):j])
PY
  kind="$(head -1 "$STEP_OUT")"
  case "$kind" in
    ok) SHELL_PROBE_VALUE="$(tail -n +2 "$STEP_OUT")" ;;
    timeout) die "the login-shell probe ($shell -ilc) did not finish within $timeout s. Nothing was changed. Fix the shell profile, or re-run with --no-desktop-proxy." ;;
    exit\ *) die "the login-shell probe ($shell -ilc) exited with status ${kind#exit }. Nothing was changed. Its output:
$(tail -n +2 "$STEP_OUT")" ;;
    *) die "the login-shell probe ($shell -ilc) printed no value markers. Nothing was changed. Its output:
$(tail -n +2 "$STEP_OUT")" ;;
  esac
}

desktop_ca_source_check() {  # desktop_ca_source_check label path — abort unless path is a usable bundle source
  # The same readable test anthrouter applies at each bundle rebuild
  # (interception.pem_problem); tests hold the two in step.
  python3 - "$2" > "$STEP_OUT" <<'PY' || die "could not check $1 ($2)"
import base64, binascii, re, sys
path = sys.argv[1]
if ':' in path:
    print("contains ':', which separates the bundle sources")
    raise SystemExit(0)
if "'" in path or '\n' in path:
    print('contains a quote or newline')
    raise SystemExit(0)
try:
    data = open(path, 'rb').read()
except OSError as exc:
    print(f'cannot be read ({exc.strerror})')
    raise SystemExit(0)
begin = re.compile(r'-----BEGIN ([A-Z0-9 ]+)-----')
label, body, certs, problem = None, [], 0, None
for raw in data.decode('latin-1').splitlines():
    line = raw.strip()
    if label is None:
        m = begin.fullmatch(line)
        if m:
            label, body = m.group(1), []
    elif line == f'-----END {label}-----':
        if label == 'CERTIFICATE':
            try:
                der = base64.b64decode(''.join(body), validate=True)
            except (binascii.Error, ValueError):
                problem = 'a CERTIFICATE block is not valid base64'
                break
            if not der.startswith(b'\x30'):
                problem = 'a CERTIFICATE block is not a DER certificate'
                break
            certs += 1
        label = None
    else:
        body.append(line)
if problem is None and label is not None:
    problem = f'it ends inside a {label} block'
if problem is None and not certs:
    problem = 'it holds no PEM certificate'
if problem:
    print(f'cannot be read as a CA bundle: {problem}')
PY
  [ ! -s "$STEP_OUT" ] || die "$1 is '$2', which $(cat "$STEP_OUT"). Nothing was changed. Fix or remove it, or re-run with --no-desktop-proxy."
}

desktop_proxy_preflight() {  # desktop_proxy_preflight settings-path tls-dir
  local settings="$1" tls_dir="$2" var val launchctl_bin
  # A proxy already in force means the network may need it, and anthrouter
  # dials direct (ADR-0018 Context); settings.json would also silently
  # override it.
  for var in HTTPS_PROXY https_proxy; do
    val="$(printenv "$var" 2>/dev/null || true)"
    [ -z "$val" ] || die "$var is set in this shell's environment ($val). anthrouter's Desktop proxy dials direct and cannot chain behind another proxy. Nothing was changed. Unset it, or re-run with --no-desktop-proxy."
  done
  launchctl_bin="${ANTHROUTER_LAUNCHCTL:-launchctl}"
  if command -v "$launchctl_bin" >/dev/null 2>&1; then
    for var in HTTPS_PROXY https_proxy; do
      val="$("$launchctl_bin" getenv "$var" 2>/dev/null || true)"
      [ -z "$val" ] || die "$var is set in launchctl's environment ($val). anthrouter's Desktop proxy dials direct and cannot chain behind another proxy. Nothing was changed. Clear it (launchctl unsetenv $var), or re-run with --no-desktop-proxy."
    done
    # A launchd value is assumed not to reach the shell probe, so no profile
    # block would override it and the TLS failure would return unseen (ADR-0020).
    val="$("$launchctl_bin" getenv NODE_EXTRA_CA_CERTS 2>/dev/null || true)"
    [ -z "$val" ] || die "NODE_EXTRA_CA_CERTS is set in launchctl's environment ($val). Claude Desktop would keep that value and never trust anthrouter's certificate authority. Nothing was changed. Run: launchctl unsetenv NODE_EXTRA_CA_CERTS (move the value to your shell profile if you still need it), or re-run with --no-desktop-proxy."
  fi
  SETTINGS_PATH="$settings" python3 - "$NODE_CA_BACKUP_KEY" > "$STEP_OUT" <<'PY' || die "could not read $settings — is it valid JSON?"
import json, os, sys
backup_key = sys.argv[1]
with open(os.environ['SETTINGS_PATH']) as fh:
    data = json.load(fh)
env = data.get('env', {})
for key in ('HTTPS_PROXY', 'https_proxy'):
    if key in env:
        print('proxy\n' + key + '\n' + str(env[key]))
        raise SystemExit(0)
if backup_key in env:
    print('orphan')
    raise SystemExit(0)
print('ok\n' + str(env.get('NODE_EXTRA_CA_CERTS') or ''))
PY
  local verdict kind
  verdict="$(cat "$STEP_OUT")"
  kind="$(printf '%s\n' "$verdict" | head -1)"
  case "$kind" in
    proxy)
      echo "[anthrouter] error: $settings already sets env.$(printf '%s\n' "$verdict" | sed -n 2p) to:" >&2
      echo "    $(printf '%s\n' "$verdict" | tail -n +3)" >&2
      die "anthrouter's Desktop proxy cannot chain behind another proxy. Nothing was changed. Remove that key, or re-run with --no-desktop-proxy."
      ;;
    orphan)
      die "$settings holds $NODE_CA_BACKUP_KEY but no install manifest exists at $MANIFEST. A prior install was interrupted — restore its value to NODE_EXTRA_CA_CERTS by hand, remove the backup key, and re-run."
      ;;
  esac
  SETTINGS_CA_VALUE="$(printf '%s\n' "$verdict" | tail -n +2)"
  [ -z "$SETTINGS_CA_VALUE" ] \
    || desktop_ca_source_check "$settings env.NODE_EXTRA_CA_CERTS" "$SETTINGS_CA_VALUE"
  [ ! -e "$tls_dir" ] \
    || die "$tls_dir already exists. It is left over from an earlier install; remove it and re-run."
  command -v openssl >/dev/null 2>&1 \
    || die "openssl not found on PATH (needed to create the Desktop proxy certificates). Install it, or re-run with --no-desktop-proxy."
  # Desktop starts Code tab sessions with the shell's environment, and Bun reads
  # NODE_EXTRA_CA_CERTS only at start, so a shell value beats settings.json.
  desktop_shell_probe
  SHELL_CA_VALUE="$SHELL_PROBE_VALUE"
  if [ -n "$SHELL_CA_VALUE" ]; then
    case "${SHELL:-}" in
      */zsh|*/bash) ;;
      *) die "unsupported shell: ${SHELL:-unset}. Your login shell sets NODE_EXTRA_CA_CERTS ($SHELL_CA_VALUE), and anthrouter can override it only in a bash or zsh profile. Nothing was changed. Re-run with --no-desktop-proxy." ;;
    esac
    desktop_ca_source_check "NODE_EXTRA_CA_CERTS from your login shell" "$SHELL_CA_VALUE"
  fi
  CA_SOURCES="$SHELL_CA_VALUE"
  if [ -n "$SETTINGS_CA_VALUE" ]; then
    CA_SOURCES="${CA_SOURCES:+$CA_SOURCES:}$SETTINGS_CA_VALUE"
  fi
}

desktop_proxy_make_certs() {  # desktop_proxy_make_certs tls-dir
  # Called as `... || die`, which suspends set -e inside, so every step
  # returns explicitly on failure.
  local dir="$1" serial
  # umask 077 so no key is ever world-readable, not even between write and chmod.
  ( umask 077 && mkdir -p "$dir" ) || return 1
  chmod 700 "$dir" || return 1
  cat > "$dir/openssl.cnf" <<'CNF' || return 1
[req]
distinguished_name = dn
prompt = no
[dn]
CN = anthrouter
[v3_ca]
basicConstraints = critical,CA:TRUE,pathlen:0
keyUsage = critical,keyCertSign,cRLSign
nameConstraints = critical,permitted;DNS:api.anthropic.com
subjectKeyIdentifier = hash
[v3_leaf]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature
extendedKeyUsage = serverAuth
subjectAltName = DNS:api.anthropic.com
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid
CNF
  ( umask 077 && openssl ecparam -name prime256v1 -genkey -noout -out "$dir/ca.key" ) >/dev/null 2>&1 \
    || return 1
  chmod 600 "$dir/ca.key" || return 1
  openssl req -x509 -new -key "$dir/ca.key" -sha256 -days 3650 \
    -config "$dir/openssl.cnf" -extensions v3_ca \
    -subj "/CN=anthrouter local interception CA" -out "$dir/ca.pem" >/dev/null 2>&1 \
    || return 1
  ( umask 077 && openssl ecparam -name prime256v1 -genkey -noout -out "$dir/leaf.key" ) >/dev/null 2>&1 \
    || return 1
  chmod 600 "$dir/leaf.key" || return 1
  openssl req -new -key "$dir/leaf.key" -config "$dir/openssl.cnf" \
    -subj "/CN=api.anthropic.com" -out "$dir/leaf.csr" >/dev/null 2>&1 \
    || return 1
  serial="0x$(openssl rand -hex 16)" || return 1
  openssl x509 -req -in "$dir/leaf.csr" -CA "$dir/ca.pem" -CAkey "$dir/ca.key" \
    -set_serial "$serial" -days 3650 -sha256 \
    -extfile "$dir/openssl.cnf" -extensions v3_leaf -out "$dir/leaf.pem" >/dev/null 2>&1 \
    || return 1
  rm -f "$dir/leaf.csr" "$dir/openssl.cnf"
  chmod 644 "$dir/ca.pem" "$dir/leaf.pem" || return 1
}

desktop_proxy_config_block() {  # prints the config.env desktop-proxy block
  echo "$BLOCK_BEGIN desktop-proxy"
  echo "ANTHROUTER_CONNECT_PROXY=1"
  if [ "$TOPOLOGY" = "chained" ]; then
    echo "ANTHROUTER_INTERCEPTION_TARGET=$CAVEMAN_URL"
  fi
  if [ "$HOME_DIR" != "$HOME/.anthrouter" ]; then
    echo "ANTHROUTER_INTERCEPTION_TLS_DIR=$TLS_DIR"
  fi
  # Always written, empty included: present means anthrouter rebuilds
  # ca-bundle.pem at every start (ADR-0020 step 2). Single quotes keep paths
  # with spaces whole; pre-flight refuses a quote in a source.
  echo "ANTHROUTER_INTERCEPTION_CA_SOURCES='$CA_SOURCES'"
  echo "$BLOCK_END desktop-proxy"
}

desktop_ca_bundle_check() {  # desktop_ca_bundle_check tls-dir — anthrouter built the bundle, ending with ca.pem
  python3 - "$1/ca-bundle.pem" "$1/ca.pem" <<'PY'
import sys
try:
    bundle, ca = (open(p, 'rb').read() for p in sys.argv[1:3])
except OSError:
    raise SystemExit(1)
raise SystemExit(0 if ca and bundle.endswith(ca) else 1)
PY
}

desktop_ca_profile_write() {  # desktop_ca_profile_write profile-file tls-dir
  # Called as `... || die`, which suspends set -e inside, so every step
  # returns explicitly on failure.
  local profile="$1" tls_dir="$2"
  mf_set caBundleProfileFile "$profile" || return 1
  touch "$profile" || return 1
  if [ -s "$profile" ] && [ -n "$(tail -c 1 "$profile")" ]; then
    echo >> "$profile" || return 1
  fi
  {
    echo "$BLOCK_BEGIN ca-bundle"
    echo "export NODE_EXTRA_CA_CERTS=\"$tls_dir/ca-bundle.pem\""
    echo "$BLOCK_END ca-bundle"
  } >> "$profile" || return 1
}

desktop_ca_profile_verify() {  # desktop_ca_profile_verify bundle-path — the block must win
  local hint=""
  desktop_shell_probe
  [ "$SHELL_PROBE_VALUE" = "$1" ] && return 0
  case "${SHELL:-}" in
    */bash) hint=" On bash a login shell reads ~/.bash_profile, which must source ~/.bashrc: add '. ~/.bashrc' to ~/.bash_profile and re-run." ;;
  esac
  die "after the profile block was written, a login shell still sets NODE_EXTRA_CA_CERTS to '$SHELL_PROBE_VALUE', not $1. A later export wins (for example in ~/.zlogin); remove it and re-run.$hint"
}

desktop_proxy_probe() {  # desktop_proxy_probe proxy-url ca-file method url [json-body]
  local proxy="$1" cacert="$2" method="$3" url="$4" body="${5:-}" hdr code
  hdr="$(mktemp -t anthrouter-probe)"
  # --noproxy '' stops a NO_PROXY in the environment from bypassing -x.
  if [ -n "$body" ]; then
    code="$(curl -sS -o /dev/null -D "$hdr" -w '%{http_code}' --max-time 20 \
      --noproxy '' -x "$proxy" --cacert "$cacert" -X "$method" \
      -H 'content-type: application/json' -H 'anthropic-version: 2023-06-01' \
      --data "$body" "$url" 2>&1)" || { echo "$code"; rm -f "$hdr"; return 1; }
  else
    code="$(curl -sS -o /dev/null -D "$hdr" -w '%{http_code}' --max-time 20 \
      --noproxy '' -x "$proxy" --cacert "$cacert" -X "$method" \
      -H 'anthropic-version: 2023-06-01' "$url" 2>&1)" || { echo "$code"; rm -f "$hdr"; return 1; }
  fi
  if grep -qi '^x-anthrouter-error:' "$hdr"; then
    echo "HTTP $code, $(grep -i '^x-anthrouter-error:' "$hdr" | head -1 | tr -d '\r')"
    rm -f "$hdr"
    return 1
  fi
  rm -f "$hdr"
  case "$code" in
    [1-5][0-9][0-9]) echo "HTTP $code" ;;
    *) echo "no HTTP response (code $code)"; return 1 ;;
  esac
}

desktop_proxy_health_check() {  # desktop_proxy_health_check proxy-url ca-file
  local out
  out="$(desktop_proxy_probe "$1" "$2" GET https://api.anthropic.com/v1/models)" \
    || die "proxy check GET https://api.anthropic.com/v1/models through $1 failed: $out"
  say "proxy check GET /v1/models: $out"
  out="$(desktop_proxy_probe "$1" "$2" POST https://api.anthropic.com/v1/messages/count_tokens \
    '{"model":"claude-sonnet-4-5","messages":[{"role":"user","content":"ping"}]}')" \
    || die "proxy check POST https://api.anthropic.com/v1/messages/count_tokens through $1 failed: $out${INTERCEPTION_HINT:-}"
  say "proxy check POST /v1/messages/count_tokens: $out"
}

desktop_proxy_wire_settings() {  # desktop_proxy_wire_settings settings-path tls-dir
  # Called as `... || die`, which suspends set -e inside, so every step
  # returns explicitly on failure.
  local settings="$1" tls_dir="$2" state
  mf_set httpsProxy created || return 1
  SETTINGS_PATH="$settings" python3 - "$ANTHROUTER_URL" <<'PY' || return 1
import json, os, sys
path = os.environ['SETTINGS_PATH']
with open(path) as fh:
    data = json.load(fh)
data.setdefault('env', {})['HTTPS_PROXY'] = sys.argv[1]
tmp = path + '.tmp'
with open(tmp, 'w') as fh:
    json.dump(data, fh, indent=2)
    fh.write('\n')
os.replace(tmp, path)
PY
  SETTINGS_PATH="$settings" python3 - > "$STEP_OUT" <<'PY' || return 1
import json, os
with open(os.environ['SETTINGS_PATH']) as fh:
    env = json.load(fh).get('env', {})
print('present' if env.get('NODE_EXTRA_CA_CERTS') not in (None, '') else 'absent')
PY
  state="$(cat "$STEP_OUT")"
  if [ "$state" = "absent" ]; then
    mf_set nodeExtraCaCerts created || return 1
  else
    mf_set nodeExtraCaCerts backed_up || return 1
  fi
  # Both paths install the bundle anthrouter builds (ADR-0020 step 1); the
  # original value is one of its sources.
  mf_set nodeExtraCaCertsValue "$tls_dir/ca-bundle.pem" || return 1
  SETTINGS_PATH="$settings" python3 - "$state" "$tls_dir/ca-bundle.pem" "$NODE_CA_BACKUP_KEY" <<'PY' || return 1
import json, os, sys
path = os.environ['SETTINGS_PATH']
state, bundle, backup_key = sys.argv[1:4]
with open(path) as fh:
    data = json.load(fh)
env = data.setdefault('env', {})
if state == 'present':
    env[backup_key] = env['NODE_EXTRA_CA_CERTS']
env['NODE_EXTRA_CA_CERTS'] = bundle
tmp = path + '.tmp'
with open(tmp, 'w') as fh:
    json.dump(data, fh, indent=2)
    fh.write('\n')
os.replace(tmp, path)
PY
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
DESKTOP_PROXY_OPT=1
while [ $# -gt 0 ]; do
  case "$1" in
    --no-browser) BROWSER_OPEN=0 ;;
    --no-desktop-proxy) DESKTOP_PROXY_OPT=0 ;;
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

# Desktop proxy wiring (ADR-0019): macOS with Claude Desktop installed, unless
# opted out. Its pre-flight runs here, before the fork, like the topology check.
DESKTOP_PROXY=false
if [ "$DESKTOP_PROXY_OPT" = "1" ] && desktop_app_present; then
  DESKTOP_PROXY=true
  say "Claude Desktop found — wiring its Code tab through anthrouter (--no-desktop-proxy skips this)"
  desktop_proxy_preflight "$SETTINGS" "$TLS_DIR"
elif [ "$DESKTOP_PROXY_OPT" = "0" ]; then
  say "Desktop proxy wiring skipped (--no-desktop-proxy)"
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
mf_write desktopProxy "$DESKTOP_PROXY"
mf_write tlsDir null
mf_write configEnvDesktopBlock null
mf_write httpsProxy null
mf_write nodeExtraCaCerts null
mf_write nodeExtraCaCertsValue null
mf_write caBundleProfileFile null
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

if [ "$DESKTOP_PROXY" = "true" ]; then
  # The leaf must exist before the fork: anthrouter refuses to start with
  # CONNECT on and no leaf (ADR-0018 step 6).
  say "Creating interception certificates in $TLS_DIR..."
  mf_set tlsDir "$TLS_DIR"
  desktop_proxy_make_certs "$TLS_DIR" || die "openssl could not create the certificates in $TLS_DIR"
  mf_write caNameConstrained true

  # A marked block, appended even to a kept config.env, so uninstall can take
  # it out again: left behind, it would make the next start abort on the
  # deleted certificates.
  say "Enabling CONNECT proxy in $CONFIG_ENV..."
  mf_write configEnvDesktopBlock true
  # A kept config.env without a final newline would glue the marker onto its last line.
  if [ -s "$CONFIG_ENV" ] && [ -n "$(tail -c 1 "$CONFIG_ENV")" ]; then
    echo >> "$CONFIG_ENV"
  fi
  desktop_proxy_config_block >> "$CONFIG_ENV"
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

if [ "$DESKTOP_PROXY" = "true" ]; then
  # Before any settings.json write, so a broken CONNECT path rolls back with
  # Claude Code still untouched.
  say "Checking the CONNECT proxy path to api.anthropic.com..."
  if [ "$TOPOLOGY" = "chained" ]; then
    INTERCEPTION_HINT=" (chained topology: decrypted Messages requests go to caveman at $CAVEMAN_URL; is caveman running?)"
  fi
  desktop_proxy_health_check "$ANTHROUTER_URL" "$TLS_DIR/ca.pem"
  # anthrouter is the one bundle builder; its first start must have produced it.
  desktop_ca_bundle_check "$TLS_DIR" \
    || die "anthrouter did not build $TLS_DIR/ca-bundle.pem ending with ca.pem — see $HOME_DIR/anthrouter.log"
fi

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

if [ "$DESKTOP_PROXY" = "true" ] && [ -n "$SHELL_CA_VALUE" ]; then
  # The login shell's own export would beat settings.json (ADR-0020 step 4).
  say "Overriding NODE_EXTRA_CA_CERTS in $PROFILE_FILE with $TLS_DIR/ca-bundle.pem..."
  desktop_ca_profile_write "$PROFILE_FILE" "$TLS_DIR" || die "could not write the ca-bundle block to $PROFILE_FILE"
  CA_PROFILE_WRITTEN=1
  desktop_ca_profile_verify "$TLS_DIR/ca-bundle.pem"
fi

if [ "$DESKTOP_PROXY" = "true" ]; then
  say "Pointing $SETTINGS HTTPS_PROXY and NODE_EXTRA_CA_CERTS at anthrouter..."
  desktop_proxy_wire_settings "$SETTINGS" "$TLS_DIR" || die "could not update $SETTINGS for the Desktop proxy"
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
if [ "$DESKTOP_PROXY" = "true" ]; then
  if [ "$CA_PROFILE_WRITTEN" = "1" ]; then
    say "Claude Desktop: QUIT and reopen it, so new Code tab sessions get the new NODE_EXTRA_CA_CERTS from $PROFILE_FILE."
    say "Every Node and Bun process started from your shell now trusts anthrouter's CA, for api.anthropic.com only."
    case "${SHELL:-}" in
      */bash) say "bash: the check ran a login shell (~/.bash_profile, which sources ~/.bashrc); the block is in ~/.bashrc. If Desktop reads only one of the two, it may miss the block." ;;
    esac
  else
    say "Claude Desktop: open a NEW Code tab session — existing sessions keep their old environment."
  fi
  say "All Claude Code HTTPS traffic now goes through anthrouter; if anthrouter stops, it fails."
  say "$HOME_DIR/uninstall.sh reverses the Desktop proxy wiring."
  if [ -n "$CA_SOURCES" ]; then
    say "note: anthrouter rebuilds $TLS_DIR/ca-bundle.pem from $CA_SOURCES plus its CA at every start; a rotated source reaches it on the next restart."
  fi
fi

if [ "$BROWSER_OPEN" = "1" ]; then
  if command -v open &>/dev/null; then
    open "$ANTHROUTER_URL/ui/" &
  elif command -v xdg-open &>/dev/null; then
    xdg-open "$ANTHROUTER_URL/ui/" &
  fi
fi
