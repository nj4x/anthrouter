---
artifact-type: adr
lineage-rules: root
---

**Status**: Accepted. Superseded in part by ADR-0020, for installs made under ADR-0020, once ADR-0020 is accepted: step 4, the Consequence "Nothing else trusts it" and the step-11 "new Code tab session" message, all three only when ADR-0020's profile block is written; and always, the `NODE_EXTRA_CA_CERTS` part of step 7, the `nodeExtraCaCerts` rules of step 8, the final "Then delete `~/.anthrouter/tls/`" bullet of step 8, and the Consequences "The CA bundle is a snapshot" and "uninstall deletes it".

# Installer wires `HTTPS_PROXY` and `NODE_EXTRA_CA_CERTS` for Claude Desktop

ADR-0018 lets anthrouter act as Claude Code's HTTPS proxy and decrypt `api.anthropic.com`. That works only when `~/.claude/settings.json` points `HTTPS_PROXY` at anthrouter and Claude Code trusts anthrouter's certificate authority. On a Mac with Claude Desktop installed, the installer sets this up. Then the CLI and the Desktop Code tab reach the same anthrouter, directly or through caveman. This ADR extends ADR-0001's mutation, backup and rollback rules to the new keys. It does not change ADR-0001.

## Decision

1. **When.** The wiring runs when `uname` is `Darwin` and `/Applications/Claude.app` or `~/Applications/Claude.app` exists. `--no-desktop-proxy` skips it. The manifest records `desktopProxy: true|false`.
2. **Pre-flight, before the fork** (in line with ADR-0001's startup ordering):
   - When `settings.json` `env.HTTPS_PROXY` or `env.https_proxy` is already set to anything, abort. Print the value. Change nothing. Chaining anthrouter behind a corporate or other proxy is out of scope.
   - Apply the same rule to the environment: abort when `HTTPS_PROXY` or `https_proxy` is set in the installer's own process environment or in `launchctl getenv`. A proxy there means the network may need it, and anthrouter dials direct (ADR-0018 Context). The `settings.json` value would also override it without notice.
   - When `env.NODE_EXTRA_CA_CERTS` is set but the file it names cannot be read, abort.
   - When `env.NODE_EXTRA_CA_CERTS_ANTHROUTER_BACKUP` exists without a manifest, abort. This is the same orphan rule as ADR-0001's `ANTHROPIC_BASE_URL_ANTHROUTER_BACKUP`.
   - When `~/.anthrouter/tls/` already exists, abort, so an existing key is never overwritten. When `openssl` is not on `PATH`, abort.
3. **Certificates, before the fork.** anthrouter refuses to start with `connect_proxy` on and no leaf (ADR-0018 step 6), so the certificates must exist before the fork. The installer uses the `openssl` command-line tool to create `~/.anthrouter/tls/` with mode 0700, containing:
   - `ca.key` (0600) and `ca.pem`: an ECDSA P-256 certificate authority, valid for 10 years, with `basicConstraints=critical,CA:TRUE,pathlen:0`.
   - `leaf.key` (0600) and `leaf.pem`: ECDSA P-256, with `subjectAltName=DNS:api.anthropic.com` only, `extendedKeyUsage=serverAuth`, valid for 10 years (the same as the certificate authority), and signed by the certificate authority.

   The manifest records `tlsDir` before the first file is written, so rollback removes a partial directory.
4. **Trust scope.** The certificate authority is trusted only through `NODE_EXTRA_CA_CERTS`. The installer never adds it to the macOS keychain. So only Claude Code processes that read `settings.json` trust it. Browsers, other apps and anthrouter itself do not.
5. **`config.env`.** The installer appends a block between `# >>> anthrouter >>> desktop-proxy` and `# <<< anthrouter <<< desktop-proxy` markers, also to a `config.env` kept from an earlier install, and records `configEnvDesktopBlock` in the manifest first. Uninstall removes every such block; left behind, it would make the next start abort on the deleted certificates. The block holds `ANTHROUTER_CONNECT_PROXY=1`, and `ANTHROUTER_INTERCEPTION_TLS_DIR` when the install home is not `~/.anthrouter`. In the chained topology it also writes `ANTHROUTER_INTERCEPTION_TARGET=http://127.0.0.1:8787/w/claude` (the caveman URL from ADR-0001). In the direct topology it leaves that key out, so anthrouter handles decrypted Messages requests in-process.
6. **Health check.** After the existing `GET /health` check passes, the installer sends two requests through the proxy, with `curl -x http://127.0.0.1:8083 --cacert ~/.anthrouter/tls/ca.pem` and no credentials:
   - `GET https://api.anthropic.com/v1/models`. This tests the CONNECT path, the leaf, the trust chain and the step-8 passthrough to Anthropic (ADR-0018).
   - `POST https://api.anthropic.com/v1/messages/count_tokens` with a minimal JSON body. This tests the step-7 path. In the direct topology it runs in-process. In the chained topology it proves only that anthrouter reaches caveman: the check runs before caveman's slot is pointed at anthrouter, and the installer cannot restart caveman, so caveman's own hop back is not tested. When caveman is not running, the check fails with a message that names it, and the install rolls back.

   For each request, any HTTP status passes, including `401`, except a response that carries an `X-Anthrouter-Error` header (ADR-0018 steps 7 and 8). A TLS error, a proxy error or that header fails the check and rolls back. The check runs before any `settings.json` write. It does not prove that Claude Code itself accepts the leaf or uses HTTP/1.1 (ADR-0018 Consequences).
7. **`settings.json` writes, after the health check.** Each step is recorded in the manifest before its write:
   - `env.HTTPS_PROXY = "http://127.0.0.1:8083"`. Manifest: `httpsProxy: created`.
   - `env.NODE_EXTRA_CA_CERTS`:
     - Absent: set it to `~/.anthrouter/tls/ca.pem`. Manifest: `nodeExtraCaCerts: created`.
     - Present: write `~/.anthrouter/tls/ca-bundle.pem`, which holds the user's file contents followed by `ca.pem`. Copy the original value to `env.NODE_EXTRA_CA_CERTS_ANTHROUTER_BACKUP`, then point the key at the bundle. Manifest: `nodeExtraCaCerts: backed_up`. This uses ADR-0001's in-file backup-key convention.
8. **Uninstall.**
   - `httpsProxy: created`: delete `env.HTTPS_PROXY` only when it still equals `http://127.0.0.1:8083`. Otherwise leave it and print a warning, because the user changed it after install.
   - `nodeExtraCaCerts: created`: delete the key only when it still names `~/.anthrouter/tls/ca.pem`. Otherwise leave it and print a warning.
   - `nodeExtraCaCerts: backed_up`: restore the backup value and remove the backup key only when the live value still names `~/.anthrouter/tls/ca-bundle.pem`. Otherwise leave both keys and print a warning, because the user changed the value after install.
   - Then delete `~/.anthrouter/tls/`.

   Each step reads the live file and the manifest, so uninstall stays idempotent, as ADR-0001 requires.
9. **No renewal.** The leaf is valid for 10 years, so there is no renewal action. The 398-day limit is a browser and Apple rule for public certificate authorities. No known rule makes Node.js, which Claude Code runs on, apply it to a private certificate authority that is trusted only through `NODE_EXTRA_CA_CERTS`. A short leaf would add a renewal action, an expiry warning and a failure mode when renewal is missed. A long leaf adds none of these.
10. **Existing installs.** In line with ADR-0006, `update.sh` never adds this wiring. When the Desktop app is present and the manifest has no `desktopProxy` field, `update.sh` prints one line: run `uninstall.sh`, then `install.sh`.
11. **Messages printed.** On success, the installer prints three facts:
    - Open a **new** Code tab session, because existing sessions keep their environment.
    - All Claude Code HTTPS traffic now depends on anthrouter running (ADR-0018 consequences).
    - `uninstall.sh` reverses the wiring.

## Consequences

- **The CA bundle is a snapshot.** When `NODE_EXTRA_CA_CERTS` was already set, later edits to the user's original file do not reach `ca-bundle.pem` until the user runs uninstall and then install. The installer prints this when it takes the `backed_up` path.
- **The CA private key on disk can impersonate any host** to any Claude Code process that reads this `settings.json`, not only `api.anthropic.com`: the CA has no name constraint (corrected by ADR-0020, which adds one). Nothing else trusts it (step 4). The key is mode 0600 in a 0700 directory, outside the repo, and uninstall deletes it.
- **`HTTPS_PROXY` also covers the terminal CLI.** The CLI's model traffic uses its `http://` base URL, so that path does not change. The CLI's own calls to `api.anthropic.com` on other paths (OAuth, feature flags) are decrypted and pass through under ADR-0018 step 8. They are not opaque tunnels. Its calls to other hosts (MCP, WebFetch) are opaque tunnels.
- **The leaf is valid for 10 years.** When it expires, `api.anthropic.com` traffic through the proxy fails: all Desktop model calls, and the CLI's non-Messages calls to that host. Opaque tunnels to other hosts keep working. To fix it, run `uninstall.sh` and then `install.sh`. If a future Claude Code build applies the 398-day rule to private certificate authorities, this decision must change.
- **Linux and Windows get no wiring.** Claude Desktop has no Linux build, so Linux has no client that needs it. `install.sh` is a bash script with no native Windows path, so Windows is out of the installer's reach.
