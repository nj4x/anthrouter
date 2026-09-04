# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

anthrouter is a single-backend Anthropic Messages API proxy. It holds no credential of its own — it forwards whichever `x-api-key`/`Authorization` the client sent, byte-for-byte, to `api.anthropic.com` (or any configured upstream). Around that passthrough it does three things: classifies each request and rewrites its model to a cheaper/pricier tier, strips volatile system-prompt blocks that would otherwise defeat prompt caching, and records what it did to SQLite for an admin UI that is read-only by default (`POST /admin/config` is the sole write endpoint, enabled only when `ANTHROUTER_ADMIN_TOKEN` is set — see ADR-0005).

## Core commands

```bash
make test               # pytest tests/ -v
make lint               # ruff check .
python -m anthrouter    # run the proxy; flags in anthrouter/config.py, or --help
```

`anthrouter/ui/dist` is checked in — the installer only ever runs `pip install`, never `npm build`, so the committed `dist/` is how the server ships the UI. After touching anything under `anthrouter/ui/src`, rebuild with `make ui-build` and commit the generated `dist/`. CI (`dist-check.yml`) fails a PR if `dist/` is stale relative to `src/`.

See `docs/agents/build-and-test.md` for UI testing and live development.

## Request path

One inbound `/v1/messages` request walks, in order: local-command interception → model-tier routing → system-prompt sanitization → upstream dispatch (`AnthropicTransport`) → single-pass SSE/JSON usage-and-text extraction → DB record → response, with the client's originally-requested model name echoed back regardless of what was actually routed to. See `docs/agents/architecture.md` for full details.

Routing failures always fail closed to the originally-requested model — never fail open to a more expensive or more permissive tier.

Transport holds no credentials — `extract_client_credentials()` rejects (401) any request missing both `x-api-key` and `Authorization`, never substitutes anthrouter's own.

## Routing decisions

Every `route_model()` call returns a `ModelRoutingDecision` with a stable `reason_code` used for both the INFO log line and the DB row — never invent a new code without adding it to the `ReasonCode` literal in `model_router.py`.

Routing is governed by four layers of precedence: long-context size floor (deterministic), walk-back cache, affirmation inheritance, and classifier. See `docs/agents/architecture.md`.

## Classifier-input privacy

The classifier only ever sees a bounded `RoutingSummary` — final user text (truncated/head-tail-capped) plus message/tool counts. It never sees the system prompt, tool schemas/names/descriptions, full history, provider metadata, or headers. `RoutingSummary.to_classifier_json()` is the enforcement point — any new field must be deliberately included there, not just added to the dataclass. System-prompt role classification (ADR 0010/0012, the weighted blend) is a *separate* classifier call with its own bounded preview, gated by `auto_model_routing_system_prompt_weight`.

Classifier payloads carry the sentinel key `_anthproxy_internal_classifier: True` to prevent recursive classification across chained anthrouter instances.

## Code conventions

- No comments explaining *what* code does — only load-bearing *why* (invariants, workarounds, ordering constraints). This codebase leans heavily on docstrings for that; match the existing style rather than adding inline comments.
- New config flags: add the field to `Config`, the `argparse` entry (with matching `ANTHROUTER_*` env var), and any cross-field validation at the bottom of `parse_args()`. Flags here are extensively cross-validated (e.g. weight pairs summing to 1.0, threshold ordering).
- Design decisions with non-obvious tradeoffs go in `docs/adr/`. See `docs/adr/0001-installer-config-mutation-policy.md` for the installer's file-mutation policy — required reading before touching `install.sh`/`uninstall.sh`.

## Domain and architecture

See `docs/agents/domain.md` for reading the domain model (`CONTEXT.md`) and design decisions (`docs/adr/`).

See `docs/agents/architecture.md` for full details on request path, routing, sanitizer, transport, persistence, and local commands.

See `docs/agents/issue-tracker.md` for issue tracking conventions.

See `docs/agents/triage-labels.md` for PR triage label vocabulary.
