---
artifact-type: adr
lineage-rules: root
---

# TLS verification delegates to the OS trust store by default

`--tls-system-trust` shipped as an opt-in flag with `truststore` as an optional extra. Every fresh install on a TLS-intercepting corporate network failed all upstream calls until the user discovered the flag. We flip the default to on and make `truststore` a hard dependency.

## Context

Python 3.13 made `VERIFY_X509_STRICT` part of `ssl.create_default_context()`'s default `verify_flags` ([cpython#119562](https://github.com/python/cpython/issues/119562)). Under strict mode OpenSSL 3.x rejects any chain where a certificate lacks the Authority Key Identifier extension (RFC 5280 §4.2.1.1) with `X509_V_ERR_MISSING_AUTHORITY_KEY_IDENTIFIER`, surfaced to Python as `[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: Missing Authority Key Identifier`.

TLS-interception proxies forge a leaf certificate per origin. The one observed in the reference deployment (Netskope) omits AKI on the leaf. macOS Security.framework, Windows CryptoAPI, and browsers accept the chain; OpenSSL strict mode does not. The failure hits every upstream client anthrouter builds — passthrough, classifier, OAuth usage meter — and is retried three times per call, so a single request produces a wall of warnings and a 502.

Background: `docs/research/2026-09-12-ssl-missing-authority-key-identifier.md`, `2026-09-12-ssl-certificate-verify-failed.md`, `2026-09-12-ssl-local-workarounds.md`, `2026-09-12-ssl-truststore-integration.md`.

## Decision

- `Config.tls_system_trust` defaults to `True`; the argparse default is `_env_bool('ANTHROUTER_TLS_SYSTEM_TRUST', True)`.
- `truststore>=0.10.0` moves from the `systrust` optional extra into `[project] dependencies`. The extra is removed.
- `truststore.inject_into_ssl()` still runs in `_maybe_inject_system_trust()` before `create_server()`, so every `ssl.SSLContext` built afterwards uses the OS store.
- Opt-out is unchanged: `--no-tls-system-trust` or `ANTHROUTER_TLS_SYSTEM_TRUST=0` restores OpenSSL's bundled-CA verification.
- The fail-closed branch stays: if `truststore` cannot be imported while the flag is on, startup aborts with an error rather than silently falling back to verification that is known to break on intercepted networks.

## Consequences

**Trust follows the OS, not Python's bundle.** Any CA the operating system trusts, anthrouter now trusts — including corporate roots pushed by MDM. This matches what the user's browser already accepts, so it widens trust only to the same set of issuers the machine is already configured to honour. On Linux `truststore` delegates to OpenSSL's system store, which is the same bundle Python used before; behaviour there is unchanged in practice.

**Process-wide monkeypatch.** `inject_into_ssl()` replaces `ssl.SSLContext` at import time and cannot be undone or toggled per request. The field stays out of `EDITABLE_FIELDS` for that reason (see ADR-0005).

**One more wheel.** `truststore` is pure Python with no native dependencies; the installer's `pip install` picks it up automatically. `update.sh` installs it into existing venvs on the next run.

**Strict-mode protections are traded for OS policy.** `VERIFY_X509_STRICT` also enforces other RFC 5280 rules (basicConstraints on CAs, key-usage bits, no v1 certificates in the chain). Delegating to the OS store means those checks are whatever the platform applies. We accept this: the alternative — clearing `VERIFY_X509_STRICT` on our own context while keeping OpenSSL's bundle — would still reject the corporate root itself unless the user also imported it into the bundle, which is the manual step this change exists to remove.

## Considered options

- **Clear `VERIFY_X509_STRICT` on anthrouter's `SSLContext`.** Fixes the AKI error but not the underlying trust problem: OpenSSL's bundle still lacks the corporate root. Rejected.
- **Document the flag and keep it opt-in.** Every new user on an intercepted network hits the error, reads a log full of retries, and has to find the flag. That is the outcome this ADR removes. Rejected.
- **Auto-detect the failure and retry with truststore.** Adds a second code path and a runtime toggle to a patch that must happen before any context exists. Rejected as more complex for no gain over default-on.
