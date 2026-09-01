# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

anthrouter is a single-backend Anthropic Messages API proxy. It holds no credential of its own — it forwards whichever `x-api-key`/`Authorization` the client sent, byte-for-byte, to `api.anthropic.com` (or any configured upstream). Around that passthrough it does three things: classifies each request and rewrites its model to a cheaper/pricier tier, strips volatile system-prompt blocks that would otherwise defeat prompt caching, and records what it did to SQLite for a read-only admin UI.

## Commands

```bash
make test           # pytest tests/ -v
make lint           # ruff check .
make ui-test        # cd anthrouter/ui && npm run lint (oxlint) && npm test (vitest run)
make ui-build       # cd anthrouter/ui && npm run build — rebuild anthrouter/ui/dist
```

Single test: `python -m pytest tests/test_model_router.py::test_name -v`

`anthrouter/ui/dist` is checked in — the server ships it and the installer only ever runs `pip install`, never `npm build`. After touching anything under `anthrouter/ui/src`, rebuild with `make ui-build` and commit the generated `dist/`. CI (`dist-check.yml`) fails a PR if `dist/` is stale relative to `src/`. For live UI development against a running proxy, `npm run dev` in `anthrouter/ui` proxies `/admin` to `127.0.0.1:8083`.

Run the proxy locally: `python -m anthrouter` (reads flags / `ANTHROUTER_*` env vars; see `anthrouter/config.py` for the full flag list, or `anthrouter --help`).

## Architecture

### Request path (`anthrouter/handlers.py`)

One inbound `/v1/messages` request walks, in order: local-command interception → model-tier routing → system-prompt sanitization → upstream dispatch (`AnthropicTransport`) → single-pass SSE/JSON usage-and-text extraction → DB record → response, with the client's originally-requested model name echoed back regardless of what was actually routed to.

There is no handler-level retry. `AnthropicTransport` already retries 429s (honoring upstream retry timing) and 5xx in-connection; with one backend there's nowhere else for a failed request to go. This means the routing decision and the prompt-hash pair are derived exactly once per request — no stash/restore across attempts.

`ProxyRequestHandler` is a `BaseHTTPRequestHandler` reused across keep-alive requests, so `_reset_request_state()` clears all per-request instance attributes at the top of `do_GET`/`do_POST`. Collaborators (`config`, `transport`, `sessions`, `request_db`, `oauth_cache`) are class attributes injected once by `server.make_handler_class()` — never touch `self.__class__` state per-request.

### Model-tier routing (`anthrouter/model_router.py`, `model_tier.py`, `session_state.py`)

Central decision function is `route_model()`. Order of precedence, each outranking the next:

1. **Long-context size floor** — a deterministic, pre-classifier check. If `estimate_input_tokens()` (calibrated by a per-session actual/estimate ratio) or the last measured session context crosses `--auto-model-routing-long-context-threshold`, force the `--auto-model-routing-long` target (e.g. `opus[1m]`) and inject the `context-1m` beta. Applies even to text-less continuation turns.
2. **Walk-back cache** — a text-less turn (tool-result-only, or no usable final-user text) replays the session's last classified tier from `SessionState` rather than reclassifying recovered boilerplate. Capped so a replay can never *upgrade* past what the client actually requested (`_cap_cached_tier`); tool-result-only continuations bypass that cap since the client resends the base model unconditionally on every agentic turn.
3. **Affirmation inheritance** — a bare "yes"/"proceed" turn inherits the conversation's established tier instead of being freshly (mis)classified as trivial.
4. **Classifier** — three modes selected by `--auto-model-routing-mode`: `classifier` (default, calls a lightweight LLM — the configured `--auto-model-routing-classifier-model` — with a bounded JSON summary and gets back a 0–100 complexity score, thresholded into trivial/standard/deep), `rules` (deterministic keyword matching, no LLM call), `tag` (client-supplied task name maps directly to a configured tier).

Every `route_model()` call returns a `ModelRoutingDecision` with a stable `reason_code` used for both the INFO log line and the DB row — never invent a new code without adding it to the `ReasonCode` literal.

**Classifier-input privacy contract**: the classifier only ever sees a bounded `RoutingSummary` — final user text (truncated/head-tail-capped) plus message/tool counts. It never sees the system prompt, tool schemas/names/descriptions, full history, provider metadata, or headers. `RoutingSummary.to_classifier_json()` is the enforcement point — any new field must be deliberately included there, not just added to the dataclass. System-prompt role classification (ADR 0010/0012, the weighted blend) is a *separate* classifier call with its own bounded preview, gated by `auto_model_routing_system_prompt_weight`.

Classifier payloads carry the sentinel key `_anthproxy_internal_classifier: True`. `route_model()` no-ops immediately on any payload already carrying it, preventing recursive classification; the mapper strips it before the request goes upstream. The `anthproxy`-spelled sentinel is intentional — if `--upstream-base-url` points at an anthproxy hop instead of the real API, that hop recognizes the same marker and skips reclassifying the classifier's own traffic.

Routing failures always fail closed to the originally-requested model — never fail open to a more expensive or more permissive tier.

`SessionState` (in-memory, bounded to 1000 entries per map, LRU-evicted, nothing survives a restart) stores two *different* key shapes side by side: the routing on/off override is keyed by the bare session key (`metadata.user_id`), while the routed-tier cache and context observations are keyed by the *context key* — session key + hash of the first user message — so a Claude Code Task sub-agent sharing its parent's `user_id` doesn't clobber the parent's cached tier.

### Sanitizer (`anthrouter/sanitizer.py`, `prompt_volatility.py`)

`sanitize_system_prompt()` runs once per attempt, after hashing the client's system prompt and before dispatch. In `strip` mode it removes allowlisted volatile blocks (e.g. a per-request billing header that invalidates the cached prefix every turn); `warn` only logs; `off` is inert. The pre-strip hash is always what gets recorded as "the prompt as the client sent it" — the sanitized hash is separate and NULL means "sanitizer didn't run," not "found nothing." An unrecognized volatile block is flagged but never silently dropped.

### Transport (`anthrouter/transport.py`, `http_util.py`, `mapper/`)

`AnthropicTransport` holds no credentials — `extract_client_credentials()` rejects (401) any request missing both `x-api-key` and `Authorization`, never substitutes anthrouter's own. `mapper/anthropic_transform.py` does model-aware request shaping (alias resolution via `model_config.py`, beta merging) — needed *only* because routing can rewrite the model to a tier that rejects a field the original tier accepted (e.g. haiku rejects `output_config.effort` with HTTP 400). Every such gate fails open: an unrecognized model family keeps the field and lets the 400 surface naturally rather than guessing.

A thinking-block 400 (opus-generated signature invalid for a downgraded sonnet/haiku tier mid-session) triggers exactly one retry with all thinking/redacted_thinking blocks stripped from history.

### Persistence (`anthrouter/db.py`, `admin.py`)

SQLite, one write connection guarded by a lock, per-thread read connections over WAL. Three tables: `requests` (one row per dispatch, routing decision folded in), `prompt_store` (deduped/ref-counted system+tools content), `sanitizer_events`. FTS5 external-content trigram index over prompt/response text for substring search without a casefolded shadow column. `admin.py` exposes only `handle_get` — read-only by design; with one backend there is no runtime control to expose via POST.

### Local commands

`proxy-help`, `proxy-status`, `proxy-set-model-routing:on|off[:session]` are intercepted in `handlers.parse_local_command()` *before* any credential parsing or upstream dispatch — they never leave the proxy. Matching happens on the exact final user message text after Claude-Code wrapper-tag stripping.

## Conventions

- No comments explaining *what* code does — only load-bearing *why* (invariants, workarounds, ordering constraints). This codebase leans heavily on docstrings for that; match the existing style rather than adding inline comments.
- New config flags: add the field to `Config`, the `argparse` entry (with matching `ANTHROUTER_*` env var), and any cross-field validation at the bottom of `parse_args()` — flags in this codebase are extensively cross-validated (e.g. weight pairs summing to 1.0, threshold ordering).
- Anything touching request/response content must respect the classifier-input privacy contract above — check `RoutingSummary.to_classifier_json()` before adding a field that might leak system prompt or tool schema text into a classifier call.
- Design decisions with non-obvious tradeoffs go in `docs/adr/` (see `docs/adr/0001-installer-config-mutation-policy.md` for the installer's file-mutation policy — required reading before touching `install.sh`/`uninstall.sh`).

## Agent skills

### Issue tracker

GitHub Issues in `nj4x/anthrouter`, via `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default vocabulary (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context — root `CONTEXT.md` (not yet created) + `docs/adr/`. See `docs/agents/domain.md`.
