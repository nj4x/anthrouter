# SSL Certificate Verification via macOS System Trust: truststore Integration

**Date**: 2026-09-12  
**Scope**: Fixing Netskope proxy certificate rejection in anthrouter via macOS Security.framework  
**Status**: truststore solves the problem; simple integration recommended

---

## Summary

**Does truststore fix it? YES.** Decisive test:

```bash
$ PYTHONPATH=/tmp/ts /opt/homebrew/bin/python3.14 -c "
import truststore; truststore.inject_into_ssl()
import ssl, http.client
c = http.client.HTTPSConnection('api.anthropic.com', 443, context=ssl.create_default_context(), timeout=10)
c.request('GET', '/'); print('HTTP', c.getresponse().status, '(TLS verify OK)')
"
# Output: HTTP 404 (TLS verify OK)
```

Without truststore: `[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: Missing Authority Key Identifier`.  
With truststore: HTTP 404 (connection succeeds).

**Recommended wiring**: **Option A (opt-in config flag)** — add `--tls-system-trust` flag and call `truststore.inject_into_ssl()` at startup before any SSL context is created. Simple, safe, can be enabled in `config.env` for this machine only.

**Files to touch**:
- `anthrouter/config.py`: add `tls_system_trust: bool = False` field (line ~370), argparse entry, env var cross-check
- `anthrouter/__main__.py`: call `inject_into_ssl()` after `parse_args()` if flag is set, before `create_server()` (line ~21)
- `pyproject.toml`: add `truststore>=0.10.0` to optional dependencies `[project.optional-dependencies]`; update to hard dependencies once pilot confirms

---

## Evidence

### Test 1: truststore.SSLContext + urllib (Portable)

```bash
$ pip install --target /tmp/ts truststore
$ PYTHONPATH=/tmp/ts python3.14 -c "
import truststore, urllib.request
ctx = truststore.SSLContext()
urllib.request.urlopen('https://api.anthropic.com/', context=ctx, timeout=10)
# HTTP 404 (TLS verify succeeds via macOS Security.framework)
"
```

**Result**: ✓ Works

### Test 2: truststore.inject_into_ssl() + ssl.create_default_context() (anthrouter Path)

```bash
$ PYTHONPATH=/tmp/ts python3.14 -c "
import truststore; truststore.inject_into_ssl()
import ssl, http.client
ctx = ssl.create_default_context()
conn = http.client.HTTPSConnection('api.anthropic.com', 443, context=ctx, timeout=10)
conn.request('GET', '/')
print('HTTP', conn.getresponse().status)
"
# Output: HTTP 404
```

**Result**: ✓ Works. `inject_into_ssl()` patches the `ssl` module globally; subsequent `create_default_context()` calls use macOS Security.framework for verification.

### Test 3: Control (No truststore, Homebrew Python)

```bash
$ /opt/homebrew/bin/python3.14 -c "
import ssl, http.client
ctx = ssl.create_default_context()
conn = http.client.HTTPSConnection('api.anthropic.com', 443, context=ctx, timeout=10)
conn.request('GET', '/')
"
# Output: ssl.SSLError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: Missing Authority Key Identifier
```

**Result**: ✗ Fails (reproduces the error).

### Test 4: Apple System Python (LibreSSL, Not Affected)

```bash
$ /usr/bin/python3 -c "
import urllib.request
urllib.request.urlopen('https://api.anthropic.com/', timeout=10)
# HTTP 404
"
# (Python 3.9.6, LibreSSL 2.8.3)
```

**Result**: ✓ Works (uses macOS system trust natively via LibreSSL; OpenSSL 3.x strictness does not apply).

---

## SSL Client Construction Sites (Inventory)

All TLS client construction in anthrouter uses `ssl.create_default_context()` or direct `HTTPSConnection` without custom context:

| Site | File:Line | Type | Notes |
|------|-----------|------|-------|
| Main passthrough | `http_util.py:63–68` | `http.client.HTTPSConnection` | `self.target.connect()` called from `transport.py:122` |
| Count tokens | `transport.py:220–230` | Same as main (calls `self.target.connect()`) | Reuses `UpstreamTarget` instance |
| Classifier call | `model_router.py:907–910` | Same as main (calls `target.backend.send_message()`) | Uses same `AnthropicTransport` |
| OAuth usage meter | `oauth_usage.py:135` | Direct `http.client.HTTPSConnection` | No SSL context passed (uses default) |
| Tests | `tests/test_transport.py` | Mock via monkeypatch (no real SSL) | N/A |

**Shared factory pattern**: `UpstreamTarget` singleton, instantiated once in `server.py:39` (`AnthropicTransport.__init__`). All requests (main passthrough, classifier, tokens) share this instance. `oauth_usage.py` builds its own connection but uses no custom context.

**Injection point**: `truststore.inject_into_ssl()` is global; patches the `ssl` module once at startup. All sites automatically use macOS trust after that.

---

## Design Options Ranked

### Option A: Opt-in Config Flag (RECOMMENDED)

**What**: Add `--tls-system-trust` / `ANTHROUTER_TLS_SYSTEM_TRUST` flag. Call `truststore.inject_into_ssl()` in `main()` right after `parse_args()`, before any server/transport creation.

**Tradeoffs**:
- ✓ Process-wide, works for all clients (main, classifier, oauth, tests)
- ✓ Non-breaking: off by default; users opt-in
- ✓ Follows anthrouter config pattern (Config field + argparse + env var + validation)
- ✓ Simple rollout: `echo "ANTHROUTER_TLS_SYSTEM_TRUST=1" >> config.env && kill <pid>`
- ✗ Requires code change (3 files) and pip install in venv
- ✓ `inject_into_ssl()` must run before first `ssl.SSLContext` is created (does, at startup line 21)

**Security**: ✓ Complies with CLAUDE.md. Transport still holds no credentials; routing still fails closed. System trust store trusts whatever IT installed (same as browsers on the machine).

**Lines to add**:
```python
# anthrouter/config.py (line ~370)
tls_system_trust: bool = False  # Use macOS Security.framework for cert verification (truststore)

# argparse entry (line ~725 near admin_token)
parser.add_argument(
    '--tls-system-trust',
    action='store_true',
    default=os.environ.get('ANTHROUTER_TLS_SYSTEM_TRUST', '').lower() == 'true',
    help='Use macOS system trust store (Security.framework) for TLS verification via truststore (env: ANTHROUTER_TLS_SYSTEM_TRUST)',
)

# Cross-validation (if added)
# (no special validation needed; boolean flag)

# anthrouter/__main__.py (line ~21, after parse_args)
cfg = parse_args(argv)
if cfg.tls_system_trust:
    try:
        import truststore
        truststore.inject_into_ssl()
        logger.info('TLS verification delegated to macOS Security.framework (truststore)')
    except ImportError:
        logger.error('--tls-system-trust requested but truststore not installed; run: pip install truststore')
        return 1
```

---

### Option B: Explicit SSLContext per Client (Not Recommended)

**What**: Pass `verify=truststore.SSLContext()` to every client, or build a factory function.

**Tradeoffs**:
- ✓ Explicit per-site (easier to audit individual client configs)
- ✗ Requires changes at 3+ sites (`http_util.py`, `oauth_usage.py`, possibly `requests` if added)
- ✗ Does not scale if more clients are added; easy to miss one
- ✗ More boilerplate; `ssl.create_default_context()` is more idiomatic

**Not recommended**: Option A (global inject) is simpler and covers all sites automatically.

---

### Option C: truststore as Soft Dependency

**Current state** (from pyproject.toml):
```toml
dependencies = []  # No dependencies
```

**Proposal**:
```toml
[project.optional-dependencies]
dev = [...]  # existing
systrust = ["truststore>=0.10.0"]  # Optional: macOS system trust store
```

**Rollout** (Option A):
```bash
~/.anthrouter/venv/bin/pip install truststore
echo "ANTHROUTER_TLS_SYSTEM_TRUST=1" >> ~/.anthrouter/config.env
~/.local/bin/anthrouter  # Restart via update.sh or manual kill + relaunch
```

**Tradeoff**: Soft dependency means the feature is opt-in (good for flexibility); does not burden users who don't need it.

---

### Option D: truststore as Hard Dependency (Future)

**Not recommended now**: Wait for pilot feedback from this machine. If widely needed, promote to hard dependency after confirming no compatibility issues.

---

## truststore Platform Support & Compliance

**From truststore docs** (https://truststore.readthedocs.io/):
- **macOS ≥ 10.8**: Security.framework (Apple Keychain) — ✓ Used here
- **Windows**: CryptoAPI (Windows certificate store)
- **Linux**: Falls back to OpenSSL (system CA bundle, no improvement for this issue)

**Real-world precedent**: `pip` package manager ships with `--use-feature=truststore` since pip 24.2 (now default). Uses identical `truststore.inject_into_ssl()` pattern. Reference: https://pip.pypa.io/en/latest/user_guide/#using-a-proxy-server

**httpx compatibility**: httpx 0.27+ accepts `verify=ssl.SSLContext` (documented at https://www.python-httpx.org/advanced/ssl/#ssl-certificates). anthrouter does not use httpx (uses stdlib `http.client`), but for reference: after `truststore.inject_into_ssl()`, all httpx clients also use macOS trust automatically.

---

## Exact Rollout Steps (For This Machine)

### Step 1: Code Change (One-time, by Development)

1. **Edit** `anthrouter/config.py`:
   - Add `tls_system_trust: bool = False` field (line ~370)
   - Add argparse entry with `--tls-system-trust` and `ANTHROUTER_TLS_SYSTEM_TRUST` env var (line ~725)

2. **Edit** `anthrouter/__main__.py`:
   - After `cfg = parse_args(argv)` (line ~21), add 5 lines:
     ```python
     if cfg.tls_system_trust:
         try:
             import truststore; truststore.inject_into_ssl()
             logger.info('TLS via macOS Security.framework (truststore)')
         except ImportError:
             logger.error('--tls-system-trust set but truststore not installed')
             return 1
     ```

3. **Edit** `pyproject.toml`:
   - Add to `[project.optional-dependencies]`:
     ```toml
     systrust = ["truststore>=0.10.0"]
     ```

4. Commit and merge to main.

### Step 2: Install truststore (User, On This Machine)

```bash
~/.anthrouter/venv/bin/pip install truststore>=0.10.0
```

### Step 3: Enable Flag in config.env

```bash
echo "ANTHROUTER_TLS_SYSTEM_TRUST=1" >> ~/.anthrouter/config.env
```

### Step 4: Restart Service

```bash
# Kill current process:
pkill -f "~/.anthrouter/venv/bin/anthrouter" || true

# Restart via shim (loads config.env):
nohup ~/.local/bin/anthrouter >> ~/.anthrouter/anthrouter.out 2>&1 &

# Or via update.sh (same effect):
~/workspace/anthrouter/update.sh restart
```

### Step 5: Verify

```bash
# Check logs:
tail -20 ~/.anthrouter/anthrouter.log | grep -i "truststore\|TLS\|api.anthropic"

# Test a request:
curl -X POST http://127.0.0.1:8083/v1/messages \
  -H "x-api-key: <your-key>" \
  -H "Content-Type: application/json" \
  -d '{"model": "claude-3-5-sonnet-20241022", "max_tokens": 10, "messages": [{"role": "user", "content": "hi"}]}'
```

---

## How the Service Launches (For Context)

**No systemd/launchd**: anthrouter is started manually or via `update.sh`:

1. **Shim** (`~/.local/bin/anthrouter`):
   ```bash
   #!/usr/bin/env bash
   if [ -f "~/.anthrouter/config.env" ]; then
     set -a
     . "~/.anthrouter/config.env"
     set +a
   fi
   exec "~/.anthrouter/venv/bin/anthrouter" "$@"
   ```
   Loads `config.env` before exec, making all `ANTHROUTER_*` env vars visible to the Python process.

2. **Startup in `__main__.py`**:
   - `parse_args(argv)` reads env vars (line 21)
   - Before `create_server()` (line 28): inject truststore if flag is set
   - Server starts listening (line 29–30)

3. **update.sh** (lines 241–242):
   ```bash
   nohup "$SHIM_PATH" >> "$HOME_DIR/anthrouter.out" 2>&1 &
   ```
   Restarts via the shim, which loads `config.env`.

**Result**: Adding `ANTHROUTER_TLS_SYSTEM_TRUST=1` to `config.env` activates the feature on next restart (via shim).

---

## Sources

- **truststore proof-of-concept**: Test runs (2026-09-12) with `/opt/homebrew/bin/python3.14` and `truststore==0.10.4`
- **macOS system Python difference**: `/usr/bin/python3` (LibreSSL 2.8.3, succeeds); `/opt/homebrew/bin/python3.14` (OpenSSL 3.6.4, fails without truststore)
- **truststore docs**: https://truststore.readthedocs.io/ (Security.framework API, platform support, usage with urllib/httpx)
- **truststore in pip**: https://pip.pypa.io/en/latest/user_guide/#using-a-proxy-server (precedent: pip uses identical `inject_into_ssl()` pattern since pip 24.2)
- **httpx SSL docs**: https://www.python-httpx.org/advanced/ssl/#ssl-certificates (verify parameter accepts `ssl.SSLContext`)
- **anthrouter code**:
  - `anthrouter/__main__.py:20–37` — startup entry point and order
  - `anthrouter/config.py` (~370) — Config dataclass field pattern; (~725) — argparse pattern
  - `anthrouter/server.py:39` — Transport instantiation (once at startup)
  - `anthrouter/http_util.py:63–68` — `UpstreamTarget.connect()` (all TLS clients use this)
  - `pyproject.toml:12–14` — Optional dependencies section
- **RFC 5280**: https://tools.ietf.org/html/rfc5280#section-4.2.1.1 (Authority Key Identifier extension, why OpenSSL 3.x enforces it)
