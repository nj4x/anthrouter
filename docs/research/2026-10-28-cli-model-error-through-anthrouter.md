# Terminal CLI "issue with the selected model" through anthrouter

**Date**: 2026-10-28
**Status**: Diagnosis complete. Fix proposed, not implemented. Nothing changed.
**Question**: After the ADR-0020 install (PR nj4x/anthrouter#46), Claude Desktop Code tab sessions work, but the terminal CLI (`claude` 2.1.300) answers every prompt at once with "There's an issue with the selected model (claude-sonnet-5-5)". Why?
**Scope**: Read-only. `~/.claude/settings.json`, `~/.anthrouter/{manifest.json,config.env,anthrouter.log,anthrouter.out,anthrouter.db}`, the `~/.zshrc` ca-bundle block, anthrouter source on branch `ccdtp/ca-bundle-override-impl-098901`, and `claude -p` runs with one-off env or `--settings` overrides. No file, profile, keychain or running process was changed.

Related notes and decisions, not repeated here:
- [ADR-0018](../adr/0018-host-interception-connect-proxy.md): host interception (CONNECT proxy).
- [ADR-0019](../adr/0019-installer-desktop-proxy-wiring.md): installer writes `HTTPS_PROXY` and `NODE_EXTRA_CA_CERTS` to `settings.json`.
- [ADR-0020](../adr/0020-shell-profile-ca-bundle-override.md): shell-profile CA bundle.
- [2026-10-28 /etc/hosts redirect](2026-10-28-etc-hosts-redirect-for-claude-desktop.md).

---

## 1. Summary (TL;DR)

1. **Claude Code 2.1.300 sends its `http://` base URL requests through `HTTPS_PROXY`.** It sends them as plain-HTTP forward-proxy requests with an absolute-form request target, for example `POST http://127.0.0.1:8787/w/claude/v1/messages?beta=true`. ADR-0018 (Context, 5th bullet) and ADR-0019 (Consequences) assume the opposite: "`HTTPS_PROXY` does not apply to an `http://` base URL".
2. **anthrouter answers those requests `404 not_found_error`.** `RequestHandler.do_POST` (`anthrouter/handlers.py`) matches `self.path` literally against `/v1/messages`. An absolute-form target never matches, so it falls to the `else` branch.
3. **The CLI maps a `404` from `/v1/messages` to the "selected model" message.** The model name, account and routing tier play no part. The requests never reach routing, transport or the DB.
4. **Desktop works because its base URL is `https://api.anthropic.com`.** That goes through `CONNECT` and the decrypted leg, which is the tested path.
5. **Workaround and fix:** put `NO_PROXY=127.0.0.1,localhost,::1` next to `HTTPS_PROXY` in `settings.json` (installer, ADR-0019 amendment). Also make anthrouter fail loudly, not with `404`, on an absolute-form request (§5).

---

## 2. Symptom

Reported by the user after the PR #46 install:

- Claude Desktop Code tab: works.
- Terminal CLI, `claude` 2.1.300, "Sonnet 5.5 with medium effort · Claude Enterprise", cwd `~/workspace/group-management`. Typing `hi` gives, at once ("Cogitated for 0s"):
  > There's an issue with the selected model (claude-sonnet-5-5). It may not exist or you may not have access to it. Run /model to pick a different model.

### 2.1 Live state (relevant parts)

`~/.claude/settings.json` `env` (no `NO_PROXY`, no `HTTP_PROXY`):

```json
"ANTHROPIC_BASE_URL": "http://127.0.0.1:8787/w/claude",
"HTTPS_PROXY": "http://127.0.0.1:8083",
"NODE_EXTRA_CA_CERTS": "/Users/r.herasymenk/.anthrouter/tls/ca-bundle.pem"
```

`~/.anthrouter/manifest.json`: `"topology": "chained"`, `"desktopProxy": true`, `"httpsProxy": "created"`, `"nodeExtraCaCerts": "created"`, `"caBundleProfileFile": "/Users/r.herasymenk/.zshrc"`.

`~/.anthrouter/config.env` (admin token masked): `ANTHROUTER_CONNECT_PROXY=1`, `ANTHROUTER_INTERCEPTION_TARGET=http://127.0.0.1:8787/w/claude`, `ANTHROUTER_LOCK_REQUESTED_MODEL="sonnet"`, `ANTHROUTER_AUTO_MODEL_ROUTING="true"`.

`~/.zshrc`:

```sh
# >>> anthrouter >>> ca-bundle
export NODE_EXTRA_CA_CERTS="/Users/r.herasymenk/.anthrouter/tls/ca-bundle.pem"
# <<< anthrouter <<< ca-bundle
```

The interactive login shell sets no `HTTP_PROXY`, `HTTPS_PROXY`, `NO_PROXY` or `ANTHROPIC_BASE_URL` of its own (checked with `zsh -ic env` inside a Desktop session; a fresh Terminal.app shell was not checked, see §7).

### 2.2 The failure in `anthrouter.log` before this investigation

The user's failing sessions left this pattern, three requests per prompt, all `404`, all within 15 ms, no session or request tag:

```
2026-10-28 21:37:47,645 INFO anthrouter.handlers: [-------- -------- +0.00s] "POST http://127.0.0.1:8787/w/claude/v1/messages/count_tokens?beta=true HTTP/1.1" 404 -
2026-10-28 21:37:47,656 INFO anthrouter.handlers: [-------- -------- +0.00s] "POST http://127.0.0.1:8787/w/claude/v1/messages?beta=true HTTP/1.1" 404 -
2026-10-28 21:37:47,659 INFO anthrouter.handlers: [-------- -------- +0.00s] "POST http://127.0.0.1:8787/w/claude/v1/messages?beta=true HTTP/1.1" 404 -
```

The same pattern is at 19:46:27, 19:46:43, 21:38:00 and 21:40:36. The `requests` table in `anthrouter.db` has no row for any of them. The failures are rejected before `_handle_messages`, so nothing is recorded.

---

## 3. Reproduction

All runs: `cd ~/workspace/group-management && claude -p hi --model <model> </dev/null`, killed after 25–60 s, while `anthrouter.log` was watched. "Terminal-like env" means `env -u ANTHROPIC_BASE_URL -u CLAUDECODE -u CLAUDE_CODE_ENTRYPOINT -u __CFBundleIdentifier`. This removes the variables that Claude Desktop puts into a Code tab session, so `settings.json` decides the base URL as it does in Terminal.app.

| # | Variant | CLI output | Request line(s) at anthrouter `:8083` |
|---|---|---|---|
| 1 | Desktop session env as-is (`ANTHROPIC_BASE_URL=https://api.anthropic.com`, `CLAUDE_CODE_ENTRYPOINT=claude-desktop`), default model | `Hi. What do you need? …` | `POST /v1/messages/count_tokens?beta=true` 200, `POST /v1/messages?beta=true` 200 (decrypted leg, then caveman to anthrouter, routed `haiku`) |
| 2 | Same, `--model claude-sonnet-5-5` | `Hi. …` | same, `requested=claude-sonnet-5-5 routed=haiku reason=classifier_trivial` |
| 3 | Terminal-like env, `claude-sonnet-5-5` | **`There's an issue with the selected model (claude-sonnet-5-5)…`** | `POST http://127.0.0.1:8787/w/claude/v1/messages/count_tokens?beta=true` **404**, 2x `POST http://127.0.0.1:8787/w/claude/v1/messages?beta=true` **404** |
| 4 | Terminal-like env, `claude-haiku-5-5` | **same error, `(claude-haiku-5-5)`** | same three 404s |
| 5 | Terminal-like env + `-u HTTPS_PROXY` | **error** | same three 404s (`settings.json` `env` sets it again) |
| 6 | Terminal-like env + `-u NODE_EXTRA_CA_CERTS` | **error** | same three 404s |
| 7 | Terminal-like env + `ANTHROPIC_BASE_URL=https://api.anthropic.com` | **error** | same three 404s (`settings.json` `env` wins over the process env) |
| 8 | Desktop env with only `-u CLAUDE_CODE_ENTRYPOINT` | **error** | same three 404s |
| 9 | Desktop env with only `-u ANTHROPIC_BASE_URL` | **error** | same three 404s |
| 10 | Desktop env with only `-u CLAUDECODE` | `Hi. …` | origin-form 200s, as in row 1 |
| 11 | Terminal-like env + `NO_PROXY=127.0.0.1,localhost` | `Hi. …` | `POST /v1/messages/count_tokens?beta=true` 200, `POST /v1/messages?beta=true` 200, `routed=haiku` |
| 12 | Terminal-like env + `--settings '{"env":{"HTTPS_PROXY":""}}'` | `Hi. …` | origin-form 200s |
| 13 | Terminal-like env + `--settings '{"env":{"ANTHROPIC_BASE_URL":"http://127.0.0.1:8083"}}'` (direct topology) | **error** | `POST http://127.0.0.1:8083/v1/messages/count_tokens?beta=true` **404**, 2x `POST http://127.0.0.1:8083/v1/messages?beta=true` **404** |

Direct checks with curl:

| Command | Result |
|---|---|
| `curl --noproxy '' -x http://127.0.0.1:8083 -X POST http://127.0.0.1:8787/w/claude/v1/messages -d '{}'` (absolute-form, no credential) | `404 {"type":"error","error":{"type":"not_found_error","message":"Not found"}}` |
| `curl -X POST http://127.0.0.1:8083/v1/messages -d '{}'` (origin-form, no credential) | `401 authentication_error` "Missing credentials …" (the path matched) |

What the table shows:

- The error depends only on whether the `http://` base URL request goes through the proxy (rows 3 vs 11, 12). The model (rows 3, 4), the CA file (row 6) and the topology (rows 3, 13) do not matter.
- Rows 8–10: the Desktop Code tab avoids it only because Desktop sets both `CLAUDE_CODE_ENTRYPOINT=claude-desktop` and `ANTHROPIC_BASE_URL=https://api.anthropic.com`. With both present the process value wins over `settings.json`, and the base URL is `https://`, so the request goes by `CONNECT`. Without either one, `settings.json`'s `http://127.0.0.1:8787/w/claude` is used.
- Rows 5, 7: a process-env override of a key that `settings.json` `env` also sets has no effect in a terminal session. Only keys absent from `settings.json` (row 11) or a `--settings` flag (rows 12, 13) change the outcome.

---

## 4. Root cause

### 4.1 The CLI proxies plain-HTTP requests through `HTTPS_PROXY`

With `HTTPS_PROXY=http://127.0.0.1:8083` and no `HTTP_PROXY` or `NO_PROXY`, Claude Code 2.1.300 opens a TCP connection to `127.0.0.1:8083` for its `http://127.0.0.1:8787/w/claude` base URL. It sends the request in forward-proxy form, with the full URL as the request target (log lines in §2.2 and rows 3, 4, 13). Why the CLI uses `HTTPS_PROXY` for an `http://` URL was not traced in the CLI's code. It is an observed behaviour only. Possible causes include a "use `HTTPS_PROXY` for every scheme" rule in its HTTP client setup. That is an assumption.

This breaks the premise in two ADRs:

- `docs/adr/0018-host-interception-connect-proxy.md`, Context: "The terminal CLI keeps `ANTHROPIC_BASE_URL=http://…`. `HTTPS_PROXY` does not apply to an `http://` base URL, so the CLI path does not change."
- `docs/adr/0019-installer-desktop-proxy-wiring.md`, Consequences: "The CLI's model traffic uses its `http://` base URL, so that path does not change."

`install.sh` `desktop_proxy_wire_settings` writes `env.HTTPS_PROXY` to `settings.json` and nothing for `NO_PROXY` (`data.setdefault('env', {})['HTTPS_PROXY'] = sys.argv[1]`). The proxy therefore applies to every Claude Code process that reads that file, the terminal CLI included.

### 4.2 anthrouter treats an absolute-form target as an unknown path

`anthrouter/handlers.py`, `RequestHandler.do_POST`:

```python
path = self.path.split('?')[0].rstrip('/')
if self.intercepted:
    self._handle_intercepted_post(path)
elif path == '/v1/messages':
    self._handle_messages()
elif path == '/v1/messages/count_tokens':
    self._handle_count_tokens()
elif path == '/admin/config':
    self._handle_admin_config_post()
else:
    self._send_json(404, anthropic_error_payload('not_found_error', 'Not found'))
```

`BaseHTTPRequestHandler` puts the raw request target into `self.path`. For a forward-proxy request that is `http://127.0.0.1:8787/w/claude/v1/messages?beta=true`, so `path` becomes `http://127.0.0.1:8787/w/claude/v1/messages`. No branch matches it and the handler sends `404 not_found_error`. `do_GET` has the same literal match. anthrouter has `do_CONNECT` for `https://` targets but nothing for absolute-form plain-HTTP targets. It is not an HTTP forward proxy.

The `404` is sent before `_handle_messages`. So there is no model lock line, no `route_model()` call, no `X-Anthrouter-Error` header, no upstream call and no DB row. The log line has no session tag (`[-------- --------`).

### 4.3 The CLI turns that 404 into the model message

A `404` with `not_found_error` from `POST /v1/messages` is how the Messages API reports an unknown model. The CLI shows its "issue with the selected model … may not exist or you may not have access" text for it. The model name in the message is only the one the CLI asked for (rows 3, 4). This is inferred from the output and the status code. The CLI's mapping code was not read.

### 4.4 What it is not

- Not routing to a tier the Enterprise account lacks: no request reached routing; rows 11 and 12 route the same `claude-sonnet-5-5` to `haiku` and get `200`.
- Not the CA bundle or `NODE_EXTRA_CA_CERTS` (row 6), and not TLS. No TLS is used on the failing leg.
- Not a lost header or credential: the origin-form path with no credential gives `401`, not `404`; the failing requests are rejected before credential parsing.
- Not `/v1/models` or another passthrough path: the only failing requests are the three `POST`s in §2.2. Earlier `GET /v1/models` `401` lines (19:48:05, 21:35:53) come from the decrypted passthrough leg and are not tied to this symptom.

---

## 5. Proposed fix

Two parts. Part A removes the failure. Part B makes the same mistake visible next time.

**A. Keep loopback traffic off the proxy (installer, ADR-0019 amendment).**

- `install.sh` `desktop_proxy_wire_settings`: also write `env.NO_PROXY` to `settings.json`, with `127.0.0.1,localhost,::1` (and the host of `ANTHROPIC_BASE_URL` if it is not loopback). If the key already exists, append the missing entries; do not replace the user's list.
- Track it in the manifest like `httpsProxy` (`created` / `merged` plus the added entries), so `uninstall.sh` removes only what the installer added (ADR-0001 mutation policy).
- Desktop is not affected: its base URL is `https://api.anthropic.com`, and `NO_PROXY` does not list that host, so its `CONNECT` path stays the same.
- Amend ADR-0018 (Context) and ADR-0019 (Consequences): the CLI does apply `HTTPS_PROXY` to its `http://` base URL; `NO_PROXY` is what keeps the CLI path unchanged.
- Users who installed PR #46 need the new key once. Until a fixed installer is out, a user can add `"NO_PROXY": "127.0.0.1,localhost,::1"` to `settings.json` `env` by hand (row 11 shows the effect).

**B. Fail loudly on absolute-form targets (anthrouter, `handlers.py`).**

- In `do_POST`, `do_GET` and `_other_method`, before path matching: if `self.path` starts with `http://` or `https://`, parse it with `urllib.parse.urlsplit`.
  - If the authority is anthrouter's own listen address (`config.host:config.port`, or `localhost`/`127.0.0.1`/`[::1]` with that port), use the path and query as the origin-form target and continue. This makes the direct topology (row 13) work even without `NO_PROXY`.
  - Otherwise do not forward it. anthrouter must not become an open plain-HTTP proxy. Answer with a status that the CLI does not read as a model error, for example `502` with `api_error` "anthrouter is not an HTTP forward proxy; add this host to NO_PROXY", and the header `X-Anthrouter-Error: forward-proxy-unsupported`. Log one WARNING naming the target host (not the path or query).
- Do not answer such a request with `404`. In Claude Code, `404` from `/v1/messages` means "unknown model" and hides the real cause.
- Tests: absolute-form self target routes like origin-form; absolute-form foreign target gets the new status and header and no upstream call; origin-form behaviour is unchanged.

A only, without B, fixes this user. B only, without A, fixes the direct topology but still breaks the chained one, which needs caveman on `127.0.0.1:8787` reached directly. Do both.

---

## 6. Verification steps

1. **Before the fix (baseline):** in Terminal.app, `cd ~/workspace/group-management && claude -p hi --model claude-sonnet-5-5 </dev/null`. Expect the "selected model" error and three `POST http://127.0.0.1:8787/w/claude/… 404` lines in `~/.anthrouter/anthrouter.log`.
2. **Workaround check, no file edit:** `NO_PROXY=127.0.0.1,localhost,::1 claude -p hi --model claude-sonnet-5-5 </dev/null`. Expect a normal answer, origin-form `POST /v1/messages?beta=true 200` lines with a `Model routing: requested=claude-sonnet-5-5 …` line, and a new row in `requests`.
3. **After fix A:** run `install.sh` (or `update.sh`) on a test account. Check that `settings.json` `env` holds `NO_PROXY` with the loopback entries, and that the manifest records it. Repeat step 1 in a new Terminal.app shell and expect a normal answer. Start an interactive `claude`, type `hi`, and expect an answer.
4. **Desktop still works:** open a new Desktop Code tab session and send `hi`. Expect `Host interception on …` at startup and, per prompt, an untagged decrypted `POST /v1/messages?beta=true 200` plus a tagged routed one, as in §3 row 1.
5. **Uninstall:** `uninstall.sh` removes only the `NO_PROXY` entries it added and keeps any user entries.
6. **After fix B:** `curl --noproxy '' -x http://127.0.0.1:8083 -X POST http://127.0.0.1:8787/w/claude/v1/messages -d '{}'` gives the new non-404 status and `X-Anthrouter-Error: forward-proxy-unsupported`. `curl --noproxy '' -x http://127.0.0.1:8083 -X POST http://127.0.0.1:8083/v1/messages -d '{}'` gives `401` "Missing credentials", which shows the self target was rewritten to origin-form. `make test` passes.

---

## 7. Open questions

- **Why the CLI proxies `http://` through `HTTPS_PROXY`.** Observed in 2.1.300 only. The CLI's HTTP client code was not read. Unknown whether `HTTP_PROXY` alone, or a lowercase `no_proxy`, would also be honoured. Fix A should be tested with both cases, and `no_proxy` should be written too if the CLI reads only lowercase.
- **Interactive mode.** All reproductions used `claude -p`. The user's interactive session shows the same message and left the same log pattern (§2.2), so the cause is assumed to be the same.
- **Terminal.app environment.** The terminal-like env was built by removing Desktop's variables from a Desktop session. A fresh Terminal.app shell was not inspected. It is assumed to add no proxy variables, based on `zsh -ic env`.
- **Does `NO_PROXY` reach MCP or WebFetch calls to loopback hosts?** It should keep them off the proxy too, which matches ADR-0018 (anthrouter gives IP-literal `CONNECT` targets `403`). Not tested.
- **`settings.json` env precedence.** Rows 5 and 7 show `settings.json` `env` wins over the process env in a terminal session, and rows 8–9 show the Desktop pair wins over `settings.json`. The exact precedence rule was not found in a primary doc. This also means a user cannot work around the bug with a one-off `HTTPS_PROXY=` on the command line, only with `NO_PROXY` or `--settings`.
