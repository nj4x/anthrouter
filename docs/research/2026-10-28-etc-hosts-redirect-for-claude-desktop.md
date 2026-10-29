# Redirecting the Claude Desktop Code tab to anthrouter with /etc/hosts

**Date**: 2026-10-28
**Status**: Research complete. Recommendations only. Nothing changed.
**Question**: The Claude Desktop app (macOS, Code tab) ignores `ANTHROPIC_BASE_URL`. Can `/etc/hosts` send `api.anthropic.com` to a local anthrouter (plain HTTP on `127.0.0.1:8083`) instead? What setup would that need?
**Scope**: Read-only. Anthropic, Apple, Chromium, Electron, Bun, Python, Caddy and mitmproxy primary docs; anthrouter source; read-only checks on this machine (process environment, `strings` on the bundled binaries, one non-root `bind()` test, `dig`, `curl -I`). `/etc/hosts`, `pf`, the keychain and all config files were not touched.

Related notes, not repeated here:
- [2026-09-12 truststore integration](2026-09-12-ssl-truststore-integration.md): how anthrouter verifies the upstream certificate.
- [2026-10-14 classifier 429 vs anthproxy](2026-10-14-classifier-429-anthproxy-diff.md): the OAuth "identity gate".

---

## 1. Summary (TL;DR)

1. **Yes, Desktop ignores `ANTHROPIC_BASE_URL` in a local Code tab session.** Anthropic documents this. The live process environment of this session confirms it (§2).
2. **`/etc/hosts` can work, but it is the hardest route.** It needs four extra parts: a TLS terminator with a locally trusted certificate for `api.anthropic.com`, a port 443 listener (a `pf` redirect, or a bind on `0.0.0.0:443`), path routing (anthrouter answers only two paths), and a loop breaker so anthrouter itself still reaches the real API. It also hijacks the host for every program on the machine (§3–§6).
3. **Recommended route: `HTTPS_PROXY` in `~/.claude/settings.json`.** For a claude.ai-signed-in local Code tab session, Claude Code reads proxy and CA variables from every settings scope, and Desktop does not override them. Point it at a local mitmproxy that intercepts only `api.anthropic.com` and sends `/v1/messages*` to anthrouter. Same TLS work as `/etc/hosts`, but no loop, no port 443, and no effect on other programs (§2.2, §7.1).
4. **Supported route for API-key users: Desktop's third-party inference "Gateway" mode** (Developer → Configure Third-Party Inference). It takes a gateway base URL and a gateway key. It replaces the claude.ai login, so it does not fit a subscription (OAuth) user (§2.3).
5. **No certificate pinning found.** Anthropic documents TLS-inspection proxies as supported. The Electron app only keeps a deny list of distrusted keys and otherwise uses Chromium's default verifier. `api.anthropic.com` is not HSTS-preloaded (§5).

---

## 2. Q1: Does Desktop ignore `ANTHROPIC_BASE_URL`? What are the alternatives?

### 2.1 Desktop sets the variable itself

Anthropic's Desktop page, section "Environment variables Desktop sets itself" ([code.claude.com/docs/en/desktop](https://code.claude.com/docs/en/desktop)):

> If you set `API_TIMEOUT_MS` or `ANTHROPIC_BASE_URL` and nothing changed in your local sessions, it's because Desktop sets those variables, among others, itself. […] Moving yours to a settings file doesn't help, because Claude Code ignores that value in a local session.
>
> `ANTHROPIC_BASE_URL`: a local session gets Anthropic's API host when you're signed in with a Claude account. To use a gateway, set up Desktop's third-party inference configuration instead.

The gateway "connect" page says the same for the desktop surface ([code.claude.com/docs/en/llm-gateway-connect](https://code.claude.com/docs/en/llm-gateway-connect), "Desktop app"): "The desktop app reads gateway routing from its third-party inference configuration, not from `ANTHROPIC_BASE_URL` or `settings.json`."

**Observed on this machine** (this research ran inside a Desktop Code tab session):

| Source | Value |
| ------ | ----- |
| Process env `CLAUDE_CODE_ENTRYPOINT` | `claude-desktop` |
| Process env `ANTHROPIC_BASE_URL` | `https://api.anthropic.com` |
| Process env `API_TIMEOUT_MS` | `900000` (the documented Desktop value) |
| `~/.claude/settings.json` → `env.ANTHROPIC_BASE_URL` | `http://127.0.0.1:8787/w/claude` (not applied) |
| Process env `HTTPS_PROXY` / `HTTP_PROXY` | not set by Desktop |

The Code tab runs its own Claude Code copy: `~/Library/Application Support/Claude/claude-code/2.1.309/…/claude.app/Contents/MacOS/claude`. `strings` shows it is a Bun-compiled binary, `Bun v1.4.4 (macOS arm64)`.

### 2.2 `HTTPS_PROXY` and CA variables do reach a local session

[code.claude.com/docs/en/network-config](https://code.claude.com/docs/en/network-config), "mTLS authentication", last paragraph:

> In Claude Desktop sessions where the app manages the provider connection, such as the Code tab on a third-party provider and Cowork sessions, Claude Code reads these variables and the proxy variables `HTTP_PROXY`, `HTTPS_PROXY`, and `NO_PROXY` only from managed settings and `~/.claude/settings.json` […]. In a local, SSH, or WSL Code tab session signed in through claude.ai, the app doesn't manage the connection, and Claude Code reads these variables from every settings scope, like any terminal session.

"These variables" are `CLAUDE_CODE_CLIENT_CERT`, `CLAUDE_CODE_CLIENT_KEY`, `NODE_EXTRA_CA_CERTS`, `NODE_TLS_REJECT_UNAUTHORIZED` and related keys. `HTTPS_PROXY` is not on Desktop's own list of variables it overrides (§2.1). So `env.HTTPS_PROXY` plus `env.NODE_EXTRA_CA_CERTS` in `~/.claude/settings.json` is a documented way to put a local intercepting proxy in front of a subscription session. **This makes `/etc/hosts` unnecessary.** See §7.1.

The same page: Claude Code trusts "both its bundled Mozilla CA certificates and your operating system's certificate store" by default (`CLAUDE_CODE_CERT_STORE=bundled,system`), the native installer can always read the OS store, and "Enterprise TLS-inspection proxies work without additional configuration when their root certificate is installed in the OS trust store". It also says "Claude Code does not support SOCKS proxies".

### 2.3 Third-party inference "Gateway" mode

[llm-gateway-connect](https://code.claude.com/docs/en/llm-gateway-connect), "Desktop app": open Help → Troubleshooting → Enable Developer Mode, then Developer → Configure Third-Party Inference, and enter the gateway base URL. The keys are `inferenceProvider: gateway`, `inferenceGatewayBaseUrl`, `inferenceGatewayApiKey`, `inferenceGatewayAuthScheme` (`bearer` or `x-api-key`) ([claude.com/docs/third-party/claude-desktop/gateway](https://claude.com/docs/third-party/claude-desktop/gateway)). The gateway must implement `POST /v1/messages` with streaming and tool use; `GET /v1/models` is optional.

Fit with anthrouter:
- anthrouter forwards whatever credential the client sends (`AGENTS.md`). In gateway mode the client sends `inferenceGatewayApiKey`. Set that to a Console API key (`sk-ant-…`) with scheme `x-api-key`, and anthrouter forwards it unchanged. **Works for API-key billing.**
- It does **not** carry a claude.ai subscription. The gateway overview says gateway credentials replace the subscription login ([code.claude.com/docs/en/llm-gateway](https://code.claude.com/docs/en/llm-gateway), "Subscriptions and gateways"). With gateway mode active, Anthropic-hosted cloud environments and Remote Control are unavailable.
- Unverified: whether the form accepts an `http://127.0.0.1:8083` base URL. The key reference only says "Full URL of the inference gateway endpoint".

### 2.4 Other alternatives checked

| Option | Result |
| ------ | ------ |
| `env.ANTHROPIC_BASE_URL` in `~/.claude/settings.json` | Ignored in local Desktop sessions (§2.1). |
| `apiKeyHelper` | Supplies a credential, not a base URL. Replaces the subscription login ([llm-gateway-connect](https://code.claude.com/docs/en/llm-gateway-connect), "Rotate credentials with apiKeyHelper"). Does not redirect traffic. |
| Managed settings `env` + `allowedProviders: ["customEndpoint"]` | Documented for the CLI ([llm-gateway](https://code.claude.com/docs/en/llm-gateway)). For Desktop, Anthropic points admins to the MDM third-party inference profile instead (`com.anthropic.claudefordesktop` domain). Not tested whether managed-settings `ANTHROPIC_BASE_URL` beats Desktop's own value. **Unverified.** |
| Terminal `claude` CLI | Honours `ANTHROPIC_BASE_URL` normally. Only the Desktop Code tab is affected. |

---

## 3. Q2: `/etc/hosts` mechanics on macOS

### 3.1 Who reads `/etc/hosts`

- macOS `hosts(5)`: "The hosts file is read by mDNSResponder(8) and used to supply results for calls to getaddrinfo(3), getnameinfo(3), etc." (`man 5 hosts` on macOS 26.6.2).
- **Claude Code (Bun)**: Bun's DNS backend defaults to `"system"` on macOS, "the non-blocking system API on macOS" ([bun.com/docs/api/dns](https://bun.com/docs/api/dns)). That is the libinfo/mDNSResponder path, so `/etc/hosts` applies. Bun caches answers for 30 s by default (`BUN_CONFIG_DNS_TIME_TO_LIVE_SECONDS`). Restart the Code tab session after an edit, or wait 30 s.
- **Electron / Chromium**: Electron's `app.configureHostResolver` says the built-in resolver is "Enabled by default on macOS" and `secureDnsMode` "Defaults to 'automatic'" (DoH first if available) ([electronjs.org/docs/latest/api/app](https://www.electronjs.org/docs/latest/api/app)). Chromium's built-in resolver parses the hosts file itself: `kFilePathHosts[] = FILE_PATH_LITERAL("/etc/hosts")` in [`net/dns/dns_config_service_posix.cc`](https://chromium.googlesource.com/chromium/src/+/main/net/dns/dns_config_service_posix.cc). **Partly verified**: the source shows Chromium reads the file. That a hosts-file hit wins over a DoH answer in `automatic` mode is my reading of Chromium's resolver design, not re-checked line by line.
- **anthrouter (Python)**: `http.client.HTTPConnection.connect` uses `socket.create_connection((self.host, self.port))`, which calls `getaddrinfo`. So anthrouter is redirected too. This is the loop in §6.

Flush after an edit: `sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder` (`dscacheutil(1)`: "-flushcache Flushes the entire cache").

### 3.2 IPv6

`api.anthropic.com` has both records: `dig` returned `A 160.79.104.10` and `AAAA 2607:6bc0::10`. Anthropic's inbound ranges are `160.79.104.0/23` and `2607:6bc0::/48` ([platform.claude.com/docs/en/api/ip-addresses](https://platform.claude.com/docs/en/api/ip-addresses)). Add both `127.0.0.1` and `::1` lines. Without the `::1` line, a client that prefers IPv6 goes to the real API.

### 3.3 Ports: hosts has no port field

`/etc/hosts` maps a name to an address only. The client still dials port 443. Test on this machine (macOS 26.6.2, non-root, Python `socket.bind`):

| Address | Port 443 bind as non-root |
| ------- | ------------------------- |
| `0.0.0.0` | OK |
| `127.0.0.1` | `[Errno 13] Permission denied` |
| `::1` | `[Errno 13] Permission denied` |

So a non-root listener can take 443 only on the wildcard address. That exposes the TLS front to the local network. **Do not do that.** Options:

1. Listen on `127.0.0.1:8443` (non-root) and add a `pf` redirect on `lo0`, for example `rdr pass on lo0 inet proto tcp from any to 127.0.0.1 port 443 -> 127.0.0.1 port 8443` plus the `inet6` twin for `::1`. Load it as an anchor (macOS `/etc/pf.conf` already declares `rdr-anchor "com.apple/*"`), then `sudo pfctl -ef`. See `pfctl(8)`, `pf.conf(5)`. Not tested here.
2. Run the TLS front as root on `127.0.0.1:443`. Simpler, but a root daemon.

---

## 4. What anthrouter can and cannot terminate

From the source:

- anthrouter is plain HTTP only. `server.py:53` builds a `ThreadingHTTPServer`. There is no `ssl.wrap_socket` / `load_cert_chain` anywhere in `anthrouter/`. A separate TLS terminator is required.
- It answers only `POST /v1/messages` and `POST /v1/messages/count_tokens` (`handlers.py:557-559`). Every other POST and GET path returns `404 not_found_error` (`handlers.py:564`, `573-574`).

Claude Code and the Desktop app also call other paths on `api.anthropic.com`. The Electron bundle (`app.asar`) contains `https://api.anthropic.com/api/oauth/claude_cli/roles` and `…/create_api_key`. Anthropic lists feature flags, telemetry and the WebFetch domain check on the same host ([network-config](https://code.claude.com/docs/en/network-config), "Network access requirements"). With a blanket redirect, those calls get anthrouter's 404. **The TLS front must route by path**: `/v1/messages*` to anthrouter, everything else to the real API.

---

## 5. Q3: TLS

| Client | Trust source | Pinning |
| ------ | ------------ | ------- |
| Claude Code (Bun, native) | Bundled Mozilla set **and** the OS store by default; `NODE_EXTRA_CA_CERTS` adds more ([network-config](https://code.claude.com/docs/en/network-config), "CA certificate store") | None documented. Anthropic documents TLS-inspection proxies as working when their root is in the OS store, which would fail under pinning. |
| Electron main / renderer | Chromium verifier. Chromium honours local trust: on macOS, certificates in the Default and System keychains marked "Always Trust" are used for TCP TLS; for QUIC, only local distrust applies ([Chrome Root Store FAQ](https://chromium.googlesource.com/chromium/src/+/main/net/data/ssl/chrome_root_store/faq.md)) | `app.asar` calls `setCertificateVerifyProc` with a deny list ("its certificate chain holds the key %s, which this app distrusts"). Otherwise it returns `-3` (use Chromium's result). So it blocks specific keys, it does not pin. The bundle also strips `ignore-certificate-errors` and `host-resolver-rules` from command-line switches. |
| anthrouter (Python) | `ssl.create_default_context()` (`http_util.py:66-67`), patched to the OS store by truststore when `--tls-system-trust` is on (default `True`, `config.py:386`; `__main__.py:34-41`) | n/a |

HSTS: `hstspreload.org/api/v2/status` reports `unknown` for both `anthropic.com` and `api.anthropic.com` (not preloaded). `curl -I https://api.anthropic.com/` returned no `Strict-Transport-Security` header. HSTS would not block a locally trusted certificate in any case. It only forces HTTPS.

Certificate: issue a leaf for `api.anthropic.com` from a local CA (for example mkcert, or the mitmproxy CA), and mark the CA "Always Trust" in the System keychain. For Claude Code, also set `NODE_EXTRA_CA_CERTS` to the CA PEM, so the setup does not depend on the OS store read.

Note: Desktop's own `http-intake`/claude.ai traffic is not affected. Only clients of `api.anthropic.com` see the local certificate.

---

## 6. Q4: The loop

With `/etc/hosts` in place, anthrouter's default upstream `https://api.anthropic.com` (`config.py:11`) resolves to `127.0.0.1` and anthrouter calls itself.

What anthrouter supports today:
- `--upstream-base-url` / `ANTHROUTER_UPSTREAM_BASE_URL` (`config.py:533-536`), parsed by `UpstreamTarget` (`http_util.py:29-67`). Scheme `http` or `https`, any host, optional port and path prefix.
- No connect-address override. `HTTPSConnection.connect` sends SNI and checks the certificate against `self.host` (CPython `http/client.py:1517`: `server_hostname = self._tunnel_host if self._tunnel_host else self.host`). `Host` also comes from `self.host`.

Consequences:

| Option | Works? |
| ------ | ------ |
| `--upstream-base-url https://160.79.104.10` | No. Certificate check fails (the certificate names `api.anthropic.com`, not the IP), and `Host` becomes the IP. |
| A second hostname in `/etc/hosts` pointing at `160.79.104.10` | No. Same certificate-name failure. |
| **Plain-HTTP egress hop**: run a local reverse proxy on `127.0.0.1:9080` that dials `160.79.104.10:443` with SNI `api.anthropic.com`, then set anthrouter `--upstream-base-url http://127.0.0.1:9080` | Yes, with no anthrouter code change. Caddy: `reverse_proxy https://160.79.104.10 { transport http { tls_server_name api.anthropic.com } header_up Host api.anthropic.com }`. `tls_server_name` "sets the server name used when verifying the certificate received in the TLS handshake" ([caddyserver.com reverse_proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy)). The same Caddy can be the TLS front from §4. |
| Run anthrouter in a container with `--add-host api.anthropic.com:160.79.104.10` | Likely yes. The container has its own `/etc/hosts`. Not tested here. |
| Small anthrouter patch: an `--upstream-connect-address` flag (like `curl --connect-to`) whose `HTTPSConnection` subclass dials the IP but keeps `self.host` for SNI and `Host` | Yes. About 15 lines in `http_util.py` plus a `Config` field, argparse entry and env var per `AGENTS.md`. Would need an ADR: it pins an IP that Anthropic may change "with notice". |

The IP is stable on paper: Anthropic says inbound addresses "will not change without notice" ([ip-addresses](https://platform.claude.com/docs/en/api/ip-addresses)). Responses come with `Server: cloudflare` and a `CF-RAY` header, so a front CDN sits on that address. Pin the address, not a resolved Cloudflare edge picked at random.

---

## 7. Setup recipes

### 7.1 Recommended: `HTTPS_PROXY` to a selective mitmproxy (no `/etc/hosts`)

Not tested end to end. Each step is grounded in the docs cited above.

1. Install mitmproxy. Start it once so it writes `~/.mitmproxy/mitmproxy-ca-cert.pem` ([mitmproxy certificates](https://docs.mitmproxy.org/stable/concepts/certificates/)).
2. Write an addon that rewrites only the Messages paths:
   ```python
   # route_to_anthrouter.py
   def request(flow):
       if flow.request.pretty_host == "api.anthropic.com" and flow.request.path.startswith("/v1/messages"):
           flow.request.scheme = "http"
           flow.request.host = "127.0.0.1"
           flow.request.port = 8083
   ```
3. Run it on loopback, intercepting only the API host and tunnelling the rest untouched:
   ```bash
   mitmdump --listen-host 127.0.0.1 --listen-port 8899 \
     --allow-hosts '^api\.anthropic\.com(:443)?$' \
     -s route_to_anthrouter.py
   ```
   (`allow_hosts` is the opposite of `ignore_hosts`, a regex on host or IP: [mitmproxy options](https://docs.mitmproxy.org/stable/concepts/options/).)
4. Add to `~/.claude/settings.json`:
   ```json
   { "env": {
       "HTTPS_PROXY": "http://127.0.0.1:8899",
       "NODE_EXTRA_CA_CERTS": "/Users/<you>/.mitmproxy/mitmproxy-ca-cert.pem"
   } }
   ```
5. Start a new Code tab session. Check `/status`: the **Proxy** row should show the URL, and the debug log should show `CA certs: Appended extra certificates from NODE_EXTRA_CA_CERTS` ([network-config](https://code.claude.com/docs/en/network-config), "Verify your configuration").
6. Leave anthrouter's upstream at the default. stdlib `http.client` does not read proxy variables ([docs.python.org http.client](https://docs.python.org/3/library/http.client.html) documents only explicit `set_tunnel`), and DNS is untouched, so there is no loop.

Effects: only Claude Code processes that read that settings file use the proxy, including terminal `claude`. Other programs are not touched. No root, no `pf`, no keychain change (the CA is trusted through `NODE_EXTRA_CA_CERTS` only).

### 7.2 `/etc/hosts` recipe (if a system-wide redirect is really wanted)

Not tested. Order matters.

1. Start the egress hop first (§6): Caddy on `127.0.0.1:9080` → `https://160.79.104.10` with `tls_server_name api.anthropic.com` and `header_up Host api.anthropic.com`. Check with `curl -s http://127.0.0.1:9080/v1/models -H 'x-api-key: …'`.
2. Restart anthrouter with `--upstream-base-url http://127.0.0.1:9080`. Note: anthrouter's `oauth_usage.py:224` still dials `api.anthropic.com` directly by name, so the OAuth usage meter will loop to the front unless the front routes `/api/oauth/usage` to the egress hop (it does in step 3).
3. Create a local CA and a leaf for `api.anthropic.com` (mkcert or `openssl`). Trust the CA "Always Trust" in the System keychain.
4. Add a Caddy site `https://api.anthropic.com` on `127.0.0.1:8443` and `[::1]:8443` with that leaf, and two routes:
   - `/v1/messages*` → `reverse_proxy http://127.0.0.1:8083` (anthrouter);
   - everything else → `reverse_proxy http://127.0.0.1:9080` (the egress hop).
5. Load a `pf` anchor redirecting `127.0.0.1:443` and `::1:443` to port 8443 on `lo0` (§3.3). `sudo pfctl -ef`.
6. Only now edit `/etc/hosts`:
   ```
   127.0.0.1 api.anthropic.com
   ::1       api.anthropic.com
   ```
   Then `sudo dscacheutil -flushcache; sudo killall -HUP mDNSResponder`.
7. Add `NODE_EXTRA_CA_CERTS` (the local CA PEM) to `~/.claude/settings.json` `env`, as a belt-and-braces for Bun.
8. Restart the Claude app, then open a new Code tab session. Check anthrouter's log for `Model routing:` lines.

Rollback: remove the two hosts lines, flush the cache, `sudo pfctl -d` (or flush the anchor), stop Caddy, reset anthrouter's upstream. Do step 6's rollback first if anything fails, since every API client on the machine depends on it.

---

## 8. Q5: Risks and side effects

- **Machine-wide hijack.** Every program that calls `api.anthropic.com` (SDKs, scripts, other agents, Electron's own OAuth calls) goes through the local front. If the front or anthrouter is down, all of them fail. §7.1 avoids this.
- **Exposure.** A wildcard `0.0.0.0:443` bind (the only non-root option) exposes the front to the LAN. The front then accepts any credential and forwards it. Use loopback plus `pf`.
- **Credential handling.** The local CA private key can impersonate `api.anthropic.com` for every client that trusts it, and the front sees plaintext API keys and OAuth bearer tokens. Keep the CA key outside the repo, scope trust where possible (`NODE_EXTRA_CA_CERTS` instead of the System keychain), and delete the CA when done.
- **IP pinning.** The egress hop pins `160.79.104.10`. Anthropic promises notice before inbound IPs change, but a silent change breaks the hop.
- **QUIC.** Chromium honours local trust additions only for TCP TLS. If Electron ever reached `api.anthropic.com` over QUIC, the local certificate would be refused. Not observed, and the redirect target does not speak QUIC anyway, so Chromium should fall back to TCP. **Unverified.**
- **Features that depend on the API host.** Fast-mode availability, the WebFetch domain check and feature flags all go to `api.anthropic.com`. With path routing they still reach the real API. Without it, they break.
- **Terms of service.** Intercepting one's own traffic is not forbidden by any doc read here. However, the earlier [OAuth identity-gate note](2026-10-14-classifier-429-anthproxy-diff.md) shows Anthropic checks subscription traffic for a Claude Code shape. Anything that changes the request (anthrouter's routing and sanitizing) applies here as it does with `ANTHROPIC_BASE_URL`. No new ToS text was checked for this note. **Unverified.**

---

## 9. Open and unverified items

1. Whether `inferenceGatewayBaseUrl` accepts plain `http://127.0.0.1:8083` (§2.3).
2. Whether managed-settings `env.ANTHROPIC_BASE_URL` overrides Desktop's injected value in a local session (§2.4).
3. Chromium resolver order (hosts file vs DoH in `automatic` mode). Source shows the hosts file is read; ordering not re-checked (§3.1).
4. `pf` `rdr` on `lo0` for locally originated traffic: standard practice, not tested on macOS 26 (§3.3).
5. Whether the Electron main process sends any `api.anthropic.com` traffic outside the two OAuth paths found in `app.asar` (§4).
6. End-to-end run of either recipe in §7. Neither was executed.
