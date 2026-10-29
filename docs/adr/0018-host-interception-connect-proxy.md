---
artifact-type: adr
lineage-rules: root
---

**Status**: Accepted

# Host interception: anthrouter answers CONNECT and decrypts `api.anthropic.com`

The Claude Desktop Code tab ignores `ANTHROPIC_BASE_URL`: in a local session signed in with a Claude account, Desktop sets it to `https://api.anthropic.com` itself. But the same session does read `HTTPS_PROXY` and `NODE_EXTRA_CA_CERTS` from `~/.claude/settings.json` (`docs/research/2026-10-28-etc-hosts-redirect-for-claude-desktop.md` §2). We make anthrouter the session's HTTPS proxy. anthrouter decrypts only `api.anthropic.com` and sends the two Messages endpoints into the same chain the terminal CLI uses. Other hosts on port 443 pass through as opaque tunnels. Other ports are refused (step 4). See **Host interception** in `GLOSSARY.md`.

## Context

- With `HTTPS_PROXY` set, Claude Code sends `CONNECT host:443` to the proxy for *every* HTTPS host it calls (the API, GitHub MCP servers, WebFetch targets). It does not send only `api.anthropic.com`. `NO_PROXY` is a deny list, so it cannot say "only this host".
- anthrouter is a plain-HTTP `ThreadingHTTPServer` (`anthrouter/server.py`). It has only `do_POST` and `do_GET`, and it answers only `POST /v1/messages` and `POST /v1/messages/count_tokens` (`handlers.py:554-574`).
- `api.anthropic.com` also serves paths that Desktop and Claude Code need but anthrouter does not own: `/api/oauth/claude_cli/roles`, `/api/oauth/claude_cli/create_api_key`, `/v1/models`, and feature-flag paths (research note §4).
- anthrouter's own upstream calls use stdlib `http.client`, which does not read proxy variables. So proxying Claude Code does not loop anthrouter back into itself.
- The terminal CLI keeps `ANTHROPIC_BASE_URL=http://…`. Corrected 2026-10-28: Claude Code 2.1.300 does send that `http://` traffic through `HTTPS_PROXY`, as absolute-form requests (`POST http://127.0.0.1:8787/w/claude/v1/messages`). Only `NO_PROXY` keeps the CLI path unchanged, so ADR-0019 writes it for loopback (`docs/research/2026-10-28-cli-model-error-through-anthrouter.md`).
- **Direct egress is assumed.** anthrouter opens every tunnel and every passthrough connection directly to the target host. It does not chain to a parent proxy. On a network that allows outbound HTTPS only through a corporate proxy, interception does not work. ADR-0019 step 2 refuses to install when a proxy variable is set. When the proxy is configured some other way, the ADR-0019 step 6 health check fails on the direct connect and rolls the install back.

## Decision

1. **Flag.** A new `Config.connect_proxy: bool`, set by `--connect-proxy` / `ANTHROUTER_CONNECT_PROXY`, defaults to off. When the flag is off, `CONNECT` gets `405`. The field stays out of `EDITABLE_FIELDS`: a runtime toggle that opens a tunnel endpoint does not belong behind the admin token.
2. **Loopback only.** `parse_args()` refuses `connect_proxy` together with a non-loopback `--host`. An open CONNECT relay on the LAN would forward any caller's traffic.
3. **Same listener.** `do_CONNECT` is added to the existing handler on `127.0.0.1:8083`. Clients set `HTTPS_PROXY=http://127.0.0.1:8083`. This needs no second port and no second health check.
4. **Port 443 only.** `CONNECT` to any port other than 443 gets `403`. The reason: a loopback CONNECT relay that accepts any port lets any local process reach any TCP service through anthrouter, for example SMTP or a database on another host. Port 443 is the only port the use case needs. This is a user-confirmed decision. See Consequences for what it breaks.
5. **Opaque tunnel for other hosts.** anthrouter compares the CONNECT host to `api.anthropic.com` case-insensitively, after it removes one trailing dot. An IP-literal CONNECT host gets `403`, in any form the resolver accepts (`127.1` and `2130706433` included), because an IP that belongs to `api.anthropic.com` would otherwise skip decryption without a trace. For any other host, anthrouter opens TCP to `host:443`, replies `200 Connection Established`, and copies bytes both ways until either side closes or no byte moves in either direction for `--tunnel-idle-timeout` / `ANTHROUTER_TUNNEL_IDLE_TIMEOUT` seconds (default 600; `0` turns the timeout off). A long-lived MCP stream that sends data or keep-alive bytes more often than the timeout stays open. A stream that is silent for longer is closed, and the client must reconnect. When the TCP connect to `host:443` fails, anthrouter replies `502` with `X-Anthrouter-Error: upstream-connect` to the `CONNECT` and opens no tunnel. The idle timeout also bounds a blocked write, so a peer that stops reading cannot hold the tunnel open. It records nothing to the DB. It logs only the host, at DEBUG.
6. **Decrypt `api.anthropic.com`.** For `CONNECT api.anthropic.com:443`, anthrouter replies `200`, wraps the client socket in a server-side `ssl.SSLContext` loaded with a fixed leaf certificate, and offers ALPN `http/1.1` only. It then serves the decrypted request with the same handler class. That class speaks HTTP/1.0, so each decrypted connection carries one request and then closes, exactly like the plain listener; the client opens a new CONNECT for the next request. The wait for that request uses the same `--tunnel-idle-timeout` as opaque tunnels. The certificate files live in `--interception-tls-dir` (default `~/.anthrouter/tls`), and ADR-0019 creates them. When `connect_proxy` is on and the leaf or its key cannot be loaded, startup aborts. It does not fall back to tunnelling `api.anthropic.com` unseen, because that would silently skip routing. ADR-0009's `truststore.inject_into_ssl()` replaces `ssl.SSLContext` process-wide, and truststore is built for client-side verification. So anthrouter builds the server-side context from the stdlib `ssl.SSLContext` class, captured before the injection runs. The server-side context does not verify client certificates and does not depend on the OS trust store.
7. **Messages endpoints take the CLI's entry point.** Only a decrypted `POST /v1/messages` or `POST /v1/messages/count_tokens` (query string allowed) goes to `--interception-target` / `ANTHROUTER_INTERCEPTION_TARGET`. Every other path or method, `/v1/messages/batches` and `GET /v1/messages/...` included, follows step 8. These are the two endpoints anthrouter already serves (Context).
   - Empty (direct topology): anthrouter handles the request in-process, exactly as a plain `POST /v1/messages`. Routing, sanitizing, the DB record and the model echo all apply.
   - A URL (chained topology, for example `http://127.0.0.1:8787/w/claude`): anthrouter forwards the request over HTTP to that URL with the path appended, unchanged. Caveman then calls anthrouter's plain `/v1/messages` endpoint, which does the routing and records the row, the same way it does for the CLI. The forwarding hop itself does not route and does not record, so one request never yields two rows.
   - **Target failure.** When the TCP connect to the interception target fails or times out, anthrouter returns `502` with an Anthropic-style `api_error` body and the header `X-Anthrouter-Error: interception-target-unreachable`. It does not fall back to in-process handling and does not tunnel the request unseen. This is the same fail-closed rule as step 6. When the target answers with any HTTP status, that status and body pass back to the client unchanged. When the connect succeeds but the request fails before a response arrives, the client gets a plain `502` `api_error` with no `X-Anthrouter-Error` header.
8. **Other paths on `api.anthropic.com` pass through.** Any other decrypted path goes to a fixed `https://api.anthropic.com`, not to `--upstream-base-url`. That is the host the client asked for, and OAuth and feature-flag calls must not reach a hop that was set up for Messages traffic. The method, path, headers, body and response stream all pass unchanged, except hop-by-hop headers and `Host`, which is set to `api.anthropic.com`. A chunked request body is forwarded with a `Content-Length`. On these forwarded hops (step 7 with a target, and step 8) the body is buffered, so anthrouter refuses with `400` `invalid_request_error`: malformed framing, a body over 32 MiB (the Messages API's own request limit), and any `Transfer-Encoding` other than plain `chunked` (the header is hop-by-hop and dropped on forward, so another coding would be lost). The in-process path of step 7 keeps the plain listener's body handling. There is no routing, no sanitizing and no DB row. When the connect to `api.anthropic.com` fails, anthrouter returns `502` with the header `X-Anthrouter-Error: upstream-connect`, so a caller can tell it from a `502` that Anthropic sent.
9. **Credentials.** Interception adds no credential and removes none. `extract_client_credentials()` still guards the Messages path (step 7). The passthrough paths in step 8 forward whatever the client sent.
10. **Absolute-form targets.** anthrouter is not a plain-HTTP forward proxy. An absolute-form request that names anthrouter's own listener (`127.0.0.1`, `localhost`, `::1` or `--host`, at the bound port) is served as its path. Any other host gets `502` with `X-Anthrouter-Error: forward-proxy-unsupported` and a hint to add the host to `NO_PROXY`, and is never forwarded. It is not `404`, because Claude Code reports a `404` from `/v1/messages` as an unknown model, which hides the cause.
11. **No leaf renewal.** The leaf lives as long as the certificate authority, 10 years (ADR-0019). anthrouter does not check its expiry and has no renewal path. After expiry, clients refuse the TLS handshake for `api.anthropic.com`. The remedy is to run `uninstall.sh`, then `install.sh`.

## Use Case Sequence

Chained topology, Desktop Code tab:

1. Claude Code (Desktop) → anthrouter: `CONNECT api.anthropic.com:443` → `200`.
2. TLS handshake against anthrouter's leaf. Claude Code trusts the leaf through `NODE_EXTRA_CA_CERTS`.
3. `POST /v1/messages?beta=true`, decrypted → anthrouter forwards it to `http://127.0.0.1:8787/w/claude/v1/messages?beta=true` (caveman).
4. Caveman → anthrouter `POST /v1/messages` → routing, sanitizing, DB row → `https://api.anthropic.com`.
5. The response streams back the same way.

Terminal CLI, unchanged: Claude Code → caveman (`ANTHROPIC_BASE_URL`) → anthrouter → `api.anthropic.com`.

## Consequences

- **anthrouter becomes a hard dependency for all Claude Code HTTPS traffic** wherever `HTTPS_PROXY` points at it. This covers the CLI too, for every host other than its `http://` base URL, which `NO_PROXY` keeps direct (ADR-0019 step 7). If anthrouter stops, MCP calls, WebFetch and Desktop model calls all fail. We accept this. The installer says so (ADR-0019).
- **Port 443 only breaks some targets.** MCP servers, WebFetch targets and other HTTPS hosts on a port other than 443 (for example `https://host:8443`) fail with `403` while `HTTPS_PROXY` points at anthrouter. The user can add such hosts to `NO_PROXY`. The rejected alternative: allow any port for opaque tunnels and decrypt only `api.anthropic.com:443`.
- **No parent proxy.** On a network that requires an outbound proxy, every tunnelled and passthrough call fails (Context).
- **Thread per tunnel.** Each tunnel holds one thread of the threading server for its whole life. A long-lived MCP stream holds its thread. This is acceptable for a single-user loopback proxy.
- **No HTTP/2 on the decrypted leg.** ALPN offers only `http/1.1`. We expect Claude Code to use HTTP/1.1, but this is not yet verified. The ADR-0019 step 6 health check uses `curl`, not Claude Code, so it does not prove it. A manual Desktop session test before release must confirm it.
- **Decrypted traffic is plaintext inside anthrouter.** This is no change from the `ANTHROPIC_BASE_URL` path, which already sees the same bytes in clear.
- **Large passthrough uploads are refused.** The 32 MiB cap of step 8 applies to every forwarded path, `/v1/files` included, because the forwarder buffers the body. A larger upload through the proxy gets `400`.
- **Fixed host string.** The decrypted host is `api.anthropic.com`, hard-coded. A second Anthropic host would need a new leaf and a new decision.

## Considered options

- **`/etc/hosts` redirect.** It needs port 443 (a `pf` redirect or a root daemon), a loop breaker for anthrouter's own upstream, and it hijacks the host for every program on the machine. Rejected (research note §3–§6, §8).
- **mitmproxy as a dependency.** It solves the problem directly but adds a large dependency tree and a second process to keep running. Rejected for the stdlib route.
- **Caddy as the front.** It cannot be installed with `pip install`, which conflicts with the installer's single install step. Rejected.
- **Desktop third-party inference "Gateway" mode.** Supported by Anthropic, but it replaces the claude.ai subscription login. Rejected for subscription users. API-key users can still choose it by hand.
