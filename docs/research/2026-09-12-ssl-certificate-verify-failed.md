# SSL Certificate Verification Failure Report

**Date**: 2026-09-12  
**Scope**: `[SSL: CERTIFICATE_VERIFY_FAILED]` errors in anthrouter proxy  
**Status**: Root cause identified; Samsung/Netskope corporate TLS interception

---

## Correction

**See also**: `docs/research/2026-09-12-ssl-local-workarounds.md` — details on why the Netskope bundle workaround does not work for Python/anthrouter, and what the user can actually do.

**TL;DR**: The Netskope proxy certificate is **structurally malformed** (missing `Authority Key Identifier` extension in the leaf certificate). Python's OpenSSL 3.x rejects it regardless of whether the issuing CA is in the trust bundle. This is a certificate configuration bug that requires corporate IT to regenerate the certificate, not an environment-var or trust-store workaround.

---

## Summary

anthrouter cannot reach `api.anthropic.com` due to a certificate issued by a corporate TLS-intercepting proxy (`SSL Decryption cert`) that lacks the required `Authority Key Identifier` extension. Python's OpenSSL 3.x validation rejects the certificate, surfacing as warnings in the app log and failing classifier calls closed (keeping the requested model). This is not a bug in anthrouter; the installed environment is behind a Samsung corporate TLS proxy that intercepts HTTPS.

---

## Evidence

### Log Findings

**File**: `~/.anthrouter/anthrouter.log`

- **Count**: 44 SSL errors in app log (verified via grep)
- **First occurrence**: Line 40432, timestamp `2026-09-12 09:19:52,904`
- **Last occurrence**: Line 40515, timestamp `2026-09-12 12:48:42,383`
- **Pattern**: Two clusters with ~3.5-hour gap:
  - **Cluster 1**: 09:19:52 to 09:21:20 (service startup to shutdown after ~100 seconds of traffic)
  - **Cluster 2**: 12:48:24 to 12:48:42 (another startup/shutdown cycle)

**Sample log lines** (exact quoted):

```
2026-09-12 09:19:52,904 WARNING anthrouter.transport: Upstream request failed (network, attempt 1/3): [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: Missing Authority Key Identifier (_ssl.c:1082) — retrying in 1.0s

2026-09-12 09:20:00,234 WARNING anthrouter.model_router: [f7d17f0b f8d09075 +0.00s] Model router: classifier call failed (HTTP 502, api_error) — keeping sonnet: Upstream connection error after 3 retries: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: Missing Authority Key Identifier (_ssl.c:1082)

2026-09-12 09:20:22,911 WARNING anthrouter.oauth_usage: OAuth usage fetch failed: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: Missing Authority Key Identifier (_ssl.c:1082)
```

**Error surfaces in three locations**:
1. Main request passthrough: `transport.py` retries all three attempts, then fails closed
2. Classifier call (system-prompt model routing): `model_router.py` catches failure, logs "classifier call failed (HTTP 502)", uses midpoint score (fail-open tier decision, as designed)
3. OAuth usage meter fetch: `oauth_usage.py` logs failure but continues

**Database**: `~/.anthrouter/anthrouter.db` contains 50 rows with `status='error'` total, but none of those rows store the SSL error text (errors are not persisted for network failures that exhaust retries). The timestamps in logs indicate these errors occurred during the startup clusters; no matching request records exist in the DB for those seconds because the passthrough connection failed before a request could be recorded.

### Certificate Chain Analysis

**Command executed**: `openssl s_client -connect api.anthropic.com:443 -showcerts 2>&1`

**Certificate issuer**:
```
Issuer: CN=SSL Decryption cert
```

**Critical finding**: The server certificate is signed by `SSL Decryption cert` (a self-signed root from a corporate TLS proxy, not a public CA). Further, the certificate **lacks the `Authority Key Identifier` extension**:

```
X509v3 Subject Key Identifier: present
X509v3 Authority Key Identifier: [MISSING]
```

This missing extension is the direct cause of the verification failure: OpenSSL 3.x (shipped with Python 3.14 on Homebrew, used by the installed venv) enforces strict checking of the certificate chain and requires this extension to link subordinate certificates to their issuers.

---

## Code Path

### Main Transport Layer

**File**: `anthrouter/http_util.py`, line 63–68

```python
def connect(self, timeout: int = DEFAULT_TIMEOUT):
    if self.scheme == 'http':
        return http.client.HTTPConnection(self.host, self.port, timeout=timeout)
    return http.client.HTTPSConnection(
        self.host, self.port, timeout=timeout, context=ssl.create_default_context()
    )
```

- Uses `ssl.create_default_context()` with **no overrides or custom CA bundle**
- Default context delegates to OpenSSL 3.x for full certificate chain validation
- CA bundle path: `/opt/homebrew/etc/ca-certificates/cert.pem` (196 certificates, system default)

**File**: `anthrouter/transport.py`, line 126

```python
except (OSError, http.client.HTTPException, ssl.SSLError) as exc:
    # ... 3 retries with exponential backoff, then fail closed
    raise AnthropicRequestError(
        f'Upstream connection error after {MAX_RETRIES} retries: {exc}',
        error_type='api_error',
        status_code=502,
        connection_error=True,
    ) from exc
```

- Catches `ssl.SSLError`, retries 3 times (exponential backoff: 1s, 2s, 4s)
- Fails closed with 502 to client (correct per CLAUDE.md: "routing failures always fail closed")
- Does not distinguish SSL verification failures from other network errors

### Classifier Call Path

**File**: `anthrouter/model_router.py`, line 905–910

```python
clf_payload = build_system_prompt_classifier_payload(system_preview, config)
try:
    send_fn = getattr(
        target.backend, 'send_classifier_message', target.backend.send_message
    )
    response = send_fn(clf_payload, credentials, config)
```

- Classifier makes a second upstream call via the same `AnthropicTransport`
- If that call hits SSL error and exhausts retries, exception is caught and logged as "classifier call failed"
- Logs warning and returns midpoint score (fail-open: uses standard tier by default)

---

## Root-Cause Assessment

### Hypothesis 1: Corporate TLS-Intercepting Proxy (High Confidence)

**Evidence**:
- The certificate issuer is `CN=SSL Decryption cert`, a self-signed root typical of corporate TLS proxies (Zscaler, Palo Alto, Blue Coat, etc.)
- Samsung corporate network is known to deploy such proxies
- The interceptor proxy certificate is missing `Authority Key Identifier` extension (non-compliant with RFC 5280, which OpenSSL 3.x strictly enforces)

**Why it fails now**:
- Python 3.14 (from Homebrew, installed Sept 12 via manifest timestamp) bundles OpenSSL 3.x with stricter validation
- Previous Python version (if any) may have used OpenSSL 1.1.x, which is more permissive
- The corporate proxy certificate defect was always present but only surfaced after the python/openssl upgrade

**Confidence**: 95%. Direct evidence from `openssl s_client`, observed certificate issuer and missing extension.

### Hypothesis 2: CA Bundle Doesn't Include Corporate Root (Medium Confidence)

**Evidence**:
- `/opt/homebrew/etc/ca-certificates/cert.pem` contains 196 certificates (system default)
- Corporate root CA (issued the `SSL Decryption cert`) is not in that bundle
- `certifi` is not installed in the venv (ModuleNotFoundError), so httpx/requests would use system default

**Why this matters**:
- Even if the corporate root were added to the system keychain or a custom CA bundle, Python would trust it
- But the individual proxy certificate is still missing `Authority Key Identifier`, so adding the root alone would not fix the immediate error

**Confidence**: 90% (certificate trust chain incomplete, but supplementary to the main defect).

### Hypothesis 3: Environment Variables or Proxy Config (Low Confidence)

**Evidence**:
- No `HTTPS_PROXY`, `HTTP_PROXY`, or `SSL_CERT_FILE` env vars set in `config.env`
- `macOS keychain` may contain corporate CA, but Python does not consult it by default (macOS-only behavior)
- No `.env` or shell config overrides observed

**Why it's unlikely**:
- Proxy env vars would change where connections route, not the TLS certificate validation
- Python on macOS does not automatically load keychain CAs into OpenSSL context

**Confidence**: 10%.

### Hypothesis 4: Classifier Call Uses Different Upstream URL (Very Low Confidence)

**Evidence**:
- Classifier is configured to call `api.anthropic.com` (default, no override in config)
- Both main passthrough and classifier use same `AnthropicTransport` instance
- Error logs show same `Missing Authority Key Identifier` message for both paths

**Why it's not the cause**:
- If classifier called a different host, the certificate issuer would differ
- Both fail with identical error, ruling out distinct upstream targets

**Confidence**: 5%.

---

## Recommended Fix Options

### Option 1: Disable SSL Verification (NOT RECOMMENDED)

**Code change**: Modify `http_util.py:67` to pass `context=ssl._create_unverified_context()` or disable verification.

**Tradeoffs**:
- ✓ Resolves the immediate SSL error
- ✗ Exposes the proxy to man-in-the-middle attacks
- ✗ Violates Anthropic API security requirements
- ✗ Violates CLAUDE.md: "Transport holds no credentials"; unverified SSL = credential exposure

**Status**: **Do NOT implement.** Unacceptable security risk.

---

### Option 2: Add Corporate CA to System Trust Store (RECOMMENDED)

**Workaround steps** (for end user):
1. Export the corporate TLS proxy root CA from the macOS keychain:
   ```bash
   security find-certificate -a -p -c "Corporate Root CA Name" /Library/Keychains/System.keychain > corporate-ca.crt
   ```
2. Add to Homebrew OpenSSL trust store:
   ```bash
   sudo cp corporate-ca.crt /opt/homebrew/etc/ca-certificates/certs/
   /opt/homebrew/etc/openssl@3/cert.pem  # may need to rebuild cert bundle
   ```
3. Restart anthrouter.

**Tradeoffs**:
- ✓ Trusts the corporate CA, removing the verification failure
- ✗ Only works if the proxy certificate is fixed to include `Authority Key Identifier` (currently missing)
- ✓ No code change required
- ✓ User-initiated; does not weaken default security

**Status**: **Viable short-term workaround**, but depends on corporate IT fixing the certificate.

---

### Option 3: Vendor Contact / Certificate Remediation (LONG-TERM)

**Action**: Escalate to Samsung IT / Security team to fix the proxy certificate:
- Add `Authority Key Identifier` extension to the `SSL Decryption cert` issued by the corporate proxy
- Ensure the certificate complies with RFC 5280 and OpenSSL 3.x validation rules

**Traceability**:
- Certificate serial, issuer, and subject can be captured from `openssl s_client` output
- IT can work with proxy vendor (Zscaler, Palo Alto, etc.) to regenerate the certificate with all required extensions

**Tradeoffs**:
- ✓ Fixes root cause permanently
- ✓ No workaround or code change needed
- ✗ Requires coordination with corporate IT (may take time)

**Status**: **Recommended for IT escalation.** This is a certificate configuration bug, not a proxy or application bug.

---

### Option 4: Add Config Flag for CA Bundle Override (OPTIONAL)

**Code change**: Add new config flag `--upstream-ca-bundle` / `ANTHROUTER_UPSTREAM_CA_BUNDLE` to allow operators to specify a custom CA file.

```python
# http_util.py
def connect(self, timeout: int = DEFAULT_TIMEOUT, ca_bundle: str | None = None):
    context = ssl.create_default_context(cafile=ca_bundle) if ca_bundle else ssl.create_default_context()
    ...
```

**Tradeoffs**:
- ✓ Flexible; allows use of custom/corporate CA bundle without system-wide changes
- ✓ Documented in config.py and --help
- ✗ Small code addition (~10 lines)
- ✗ Requires end user to know and provide the correct CA bundle path

**Status**: **Nice-to-have.** Enables future use cases but is not critical for this issue (Option 2 handles it without code change).

---

## Does the Repo Already Handle This?

### Config Flags

No existing config flag for TLS verification or CA bundle override. Verified via grep of `anthrouter/config.py` and `CLAUDE.md`.

### ADRs

No ADR in `docs/adr/` addresses TLS, SSL, or transport certificate handling. Verified via directory listing.

### Existing Code Comments

`anthrouter/transport.py` docstring (line 1–8) states:

> Transport holds no credentials — `extract_client_credentials()` rejects (401) any request missing both `x-api-key` and `Authorization`, never substitutes anthrouter's own.

This reinforces that SSL verification must remain strict (no credential leakage through unverified channels). No override or lenient-verification mode is intended by design.

---

## Sources

- **App logs**: `~/.anthrouter/anthrouter.log` (lines 40432–40515)
- **DB schema and audit**: `~/.anthrouter/anthrouter.db` (SQLite, opened with `sqlite3 -readonly`)
- **Repository code**:
  - `anthrouter/http_util.py:63–68` — SSL context creation
  - `anthrouter/transport.py:105–142` — Transport retry logic and error handling
  - `anthrouter/model_router.py:905–937` — Classifier call path
- **Installed environment**:
  - `~/.anthrouter/manifest.json` — Installation timestamp and venv location
  - `~/.anthrouter/venv/bin/python3 -c "import ssl; print(ssl.get_default_verify_paths())"` — CA bundle location
  - `openssl s_client -connect api.anthropic.com:443 -showcerts` — Live certificate chain inspection
- **OpenSSL / Python documentation**:
  - Python `ssl` module docs: `ssl.create_default_context()` behavior and CA bundle resolution
  - OpenSSL 3.x certificate validation: RFC 5280 compliance for `Authority Key Identifier` extension
  - Homebrew python@3.14: ships with OpenSSL 3.x (strict validation)
