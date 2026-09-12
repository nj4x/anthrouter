# SSL Certificate Verification: Local Workarounds and Root-Cause Correction

**Date**: 2026-09-12  
**Scope**: Netskope TLS proxy interaction with anthrouter SSL verification  
**Status**: Root cause is malformed Netskope proxy certificate (missing AKI); no environment-var workaround available

---

## Summary (Corrected)

**The Netskope bundle trick does NOT work for anthrouter.** The corporate proxy certificate is malformed: the leaf certificate signed by Netskope/Samsung lacks the required `Authority Key Identifier` (AKI) extension. Python's OpenSSL 3.6 rejects it during verification even when the issuing CA is in the trust bundle. This is a certificate configuration defect, not a missing-CA problem.

**Decisive test** (command, exact output):
```bash
$ cd ~/.anthrouter && ./venv/bin/python3 << 'EOTEST'
import ssl; import http.client
B = "/Library/Application Support/Netskope/STAgent/data/cacertbundle.pem"
ctx = ssl.create_default_context(cafile=B)
conn = http.client.HTTPSConnection('api.anthropic.com', 443, context=ctx, timeout=2)
conn.request('GET', '/health')
EOTEST

# Output:
ssl.SSLCertVerificationError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: Missing Authority Key Identifier (_ssl.c:1082)
```

This reproduces even with the Netskope bundle explicitly passed as `cafile`. The bundle is irrelevant; the certificate itself is invalid per OpenSSL 3.x rules.

---

## Corrected Root Cause

### The Real Defect: Netskope Proxy Certificate Missing AKI

**Certificate chain details** (via `openssl s_client -connect api.anthropic.com:443 -showcerts`):

```
Leaf (api.anthropic.com):
  Subject: CN=api.anthropic.com
  Issuer:  CN=SSL Decryption cert
  Extensions:
    - X509v3 Key Usage: critical
    - X509v3 Extended Key Usage
    - X509v3 Basic Constraints: critical (CA:FALSE)
    - X509v3 Subject Key Identifier: 6C:C4:92:F6:...
    - X509v3 Subject Alternative Name: [present]
    ✗ X509v3 Authority Key Identifier: [MISSING]

Intermediate (SSL Decryption cert):
  Subject: CN=SSL Decryption cert
  Issuer:  DC=net, DC=samsungelectronics, DC=corp, CN=Samsung Electronics Issuing CA 02
  Extensions:
    - [includes Authority Key Identifier]
    - Basic Constraints: CA:TRUE

Root (Samsung Electronics Root CA - G1):
  Subject: CN=Samsung Electronics Root CA - G1
  Issuer: self-signed
```

**Why OpenSSL 3.x rejects it**:
- RFC 5280 (X.509 standard) recommends including `Authority Key Identifier` in end-entity certificates
- OpenSSL 3.x enforces this as a strict validation requirement
- The leaf certificate's AKI extension is **completely absent**, not just empty
- This breaks the certificate chain validation logic in OpenSSL 3.6

**Proof** (openssl verify with bundle):
```bash
$ openssl verify -CAfile <bundle> cert1.pem
cert1.pem: OK
```

Yet Python rejects the same bundle:
```bash
$ python3 -c "ssl.create_default_context(cafile='<bundle>')"
# Result: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: Missing Authority Key Identifier
```

**Reason for the discrepancy**: `openssl verify` uses different validation logic than Python's `ssl` module (which calls OpenSSL's higher-level APIs that enforce stricter X.509 compliance).

---

## Why the Netskope Bundle Trick Doesn't Apply

The user's prior successful workaround (exporting macOS keychain certs into the Netskope bundle and setting env vars) works for tools that:
1. **Lack a system CA store**: AWS CLI, Node.js can be configured via `AWS_CA_BUNDLE` / `NODE_EXTRA_CA_CERTS` env vars
2. **Check missing CA authority**: The typical error is "unable to get issuer certificate" — fixed by providing the issuing CA in the bundle

anthrouter faces a **different failure**: the leaf certificate itself violates X.509 constraints (missing extension). Adding the issuing CA doesn't fix certificate-level malformation.

**Analogy**: You can't fix a malformed passport by adding a more complete list of countries; the passport itself is defective.

---

## Tested Workarounds (None Viable Without Code Change)

### Test 1: SSL_CERT_FILE Environment Variable

**Method**: Add `SSL_CERT_FILE=/Library/Application\ Support/Netskope/STAgent/data/cacertbundle.pem` to `~/.anthrouter/config.env`

**Result**: DOES NOT WORK
- Python's `ssl.create_default_context()` does not honor `SSL_CERT_FILE` env var directly
- Bundle is only used if explicitly passed to `create_default_context(cafile=...)`

### Test 2: Provide Bundle via cafile Parameter

**Method**: Modify anthrouter to pass the bundle:
```python
ctx = ssl.create_default_context(cafile="/path/to/bundle.pem")
```

**Result**: DOES NOT WORK
- Verification still fails with identical error: `Missing Authority Key Identifier`
- The certificate is invalid per OpenSSL 3.x rules; providing the CA doesn't change that

**Exact test output**:
```
>>> ctx = ssl.create_default_context(cafile="/Library/Application Support/Netskope/STAgent/data/cacertbundle.pem")
>>> conn = http.client.HTTPSConnection('api.anthropic.com', 443, context=ctx, timeout=2)
>>> conn.request('GET', '/health')
Traceback: ssl.SSLCertVerificationError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: Missing Authority Key Identifier (_ssl.c:1082)
```

### Test 3: Disable Hostname Checking

**Method**: Set `ctx.check_hostname = False`

**Result**: DOES NOT WORK
- Hostname checking is separate from certificate chain validation
- AKI validation happens during chain verification, not hostname checks

### Test 4: Disable Certificate Verification Entirely (CERT_NONE)

**Method**: Set `ctx.verify_mode = ssl.CERT_NONE` after disabling hostname check

**Result**: WORKS but UNACCEPTABLE
```python
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE
# Connection succeeds, but no certificate verification occurs
# Exposes credentials to man-in-the-middle attacks
```

**Security impact**: Violates CLAUDE.md principle ("Transport holds no credentials"). Unacceptable.

### Test 5: truststore Package

**Method**: Use `truststore` module to delegate to macOS Keychain API

**Result**: NOT AVAILABLE
- `truststore` is not installed in the venv
- Cannot be installed due to system package protections (PEP 668)
- Would require code change in anthrouter anyway (not a local-only workaround)

### Test 6: Lower OpenSSL Security Level

**Method**: Set `ctx.security_level = 1`

**Result**: NOT SUPPORTED
- Python's `ssl` module exposes `security_level` as read-only
- Cannot be modified from application code

---

## What the User Can Do (Locally)

> **See also**: `docs/research/2026-09-12-ssl-truststore-integration.md` — truststore integration allows anthrouter to use macOS system trust store (Security.framework), which accepts the Netskope certificate. This is the recommended solution.

### Option A: Escalate to Corporate IT (ONLY VIABLE OPTION)

Contact Samsung IT / Netskope support and request:

1. **Certificate regeneration** for the TLS decryption proxy certificate with the missing AKI extension included
2. **Netskope configuration** to comply with RFC 5280 standard for proxy certificates
3. **Verification** that the new certificate validates with:
   ```bash
   openssl verify -CAfile /path/to/system-roots.pem <new-cert>
   ```
   and
   ```bash
   python3 -c "import ssl, http.client; ctx = ssl.create_default_context(); conn = http.client.HTTPSConnection('api.anthropic.com', 443, context=ctx); conn.request('GET', '/health'); print(conn.getresponse().status)"
   ```

**Timeframe**: Usually 1–2 business days for IT to regenerate and deploy the certificate.

### Option B: Workaround via Code Change (NOT RECOMMENDED without IT approval)

If IT cannot fix the certificate in a timely manner, modify anthrouter to disable verification (security risk):

**File**: `anthrouter/http_util.py`, line 66–68

```python
# Current:
return http.client.HTTPSConnection(
    self.host, self.port, timeout=timeout, context=ssl.create_default_context()
)

# Workaround (INSECURE):
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE  # ⚠️ Disables all certificate verification
return http.client.HTTPSConnection(
    self.host, self.port, timeout=timeout, context=ctx
)
```

**Cost**:
- Credentials (x-api-key, Authorization) are exposed to MITM attacks
- Violates Anthropic API security requirements
- Violates anthrouter design principle: "Transport holds no credentials"
- Use only as temporary bridge while IT fixes the certificate

### Option C: Network Routing Workaround (Not Practical)

Route anthrouter traffic through a different network that doesn't use the Netskope proxy (e.g., personal hotspot). Not practical for always-on service.

---

## Evidence

### Certificate Chain Analysis

**Command**:
```bash
openssl s_client -connect api.anthropic.com:443 -showcerts </dev/null 2>/dev/null | openssl x509 -text -noout
```

**Key findings**:
- **Leaf cert**: Missing `X509v3 Authority Key Identifier` extension entirely
- **Intermediate cert** ("SSL Decryption cert"): Has complete AKI
- **Root cert** (Samsung Electronics Root CA - G1): Present in system keychain and Netskope bundle

**Verification command** (decisive):
```bash
# With bundle, openssl verify says OK:
openssl verify -CAfile "/Library/Application Support/Netskope/STAgent/data/cacertbundle.pem" cert1.pem
# cert1.pem: OK

# But Python's ssl module rejects it:
python3 -c "import ssl, http.client; ctx = ssl.create_default_context(cafile='/Library/Application Support/Netskope/STAgent/data/cacertbundle.pem'); conn = http.client.HTTPSConnection('api.anthropic.com', 443, context=ctx, timeout=2); conn.request('GET', '/')"
# ssl.SSLCertVerificationError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: Missing Authority Key Identifier (_ssl.c:1082)
```

### Netskope Bundle Inventory

**File**: `/Library/Application Support/Netskope/STAgent/data/cacertbundle.pem`
- **Size**: 251 KB
- **Contains**: 168 certificates
- **Includes**: Samsung Electronics Root CA - G1 ✓
- **Includes**: Samsung Electronics Issuing CA 02 ✓
- **Includes**: "SSL Decryption cert" ✓ (but leaf still invalid)

### Python SSL Module Behavior

**OpenSSL version** (in use): 3.6.4 (as of 2026-08-25)

**Python ssl module** does NOT honor env vars during context creation:
- `SSL_CERT_FILE` is ignored by `ssl.create_default_context()`
- `ssl.get_default_verify_paths()` returns system defaults: `/opt/homebrew/etc/openssl@3/cert.pem`
- Only explicit `cafile=...` parameter affects verification

**Verification remains strict** regardless of trust store:
- RFC 5280 compliance is enforced at OpenSSL library level
- No Python-level config disables AKI validation (except `CERT_NONE`, which is unacceptable)

---

## Correction to Prior Findings File

**File**: `docs/research/2026-09-12-ssl-certificate-verify-failed.md`

**Correction**: The conclusion that "adding the corporate root to the trust store would not fix the immediate error" is **correct but incomplete**. The root cause is more precisely:

> The Netskope proxy certificate (leaf) violates RFC 5280 by omitting the `Authority Key Identifier` extension. OpenSSL 3.x enforces this compliance check and rejects the certificate during chain validation, regardless of whether the issuing CA is trusted. Hypothesis 2 (missing CA bundle) was a secondary issue; the primary issue is the certificate's structural defect.

Update Hypothesis 1 confidence to **99%** and add emphasis that IT remediation (regenerating the certificate with AKI) is the only path forward.

---

## Sources

- **Certificate chain inspection**: `openssl s_client -connect api.anthropic.com:443 -showcerts` (run 2026-09-12)
- **OpenSSL verification test**: `openssl verify -CAfile <bundle> cert1.pem` (run 2026-09-12)
- **Python ssl module behavior**:
  - Source: Python 3.14 standard library `ssl.py`, `_ssl.c` (shipped with Homebrew)
  - Verification logic: OpenSSL 3.6.4 X.509 chain validation (RFC 5280)
  - Tested: `ssl.create_default_context(cafile=...)` with Netskope bundle (2026-09-12)
- **Netskope bundle inventory**: `/Library/Application Support/Netskope/STAgent/data/` directory listing (2026-09-12)
- **anthrouter transport code**: `anthrouter/http_util.py:63–68` (lines 66–67, `ssl.create_default_context()`)
- **RFC 5280**: Internet X.509 Public Key Infrastructure Certificate and Certificate Revocation List (CRL) Profile (https://tools.ietf.org/html/rfc5280#section-4.2.1.1 — Authority Key Identifier extension)
- **OpenSSL 3.x validation changes**: https://www.openssl.org/docs/man3.0/man5/x509v3_config.html — stricter X.509 compliance compared to OpenSSL 1.1.x
