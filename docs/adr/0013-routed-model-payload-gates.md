---
artifact-type: adr
lineage-rules: root
---

# Routed-model payload gates strip fields invalid for downtiered models

Model-tier routing changes the requested model to a cheaper tier (haiku, sonnet) or vice versa. Four payload fields valid for the client's requested tier can be rejected by the routed tier with HTTP 400:

1. **`fallbacks`**: Anthropic validates each fallback target against the routed model's `allowed_fallback_models`. A fallback chosen for opus is invalid for haiku.
2. **`max_tokens`**: Each model family has an output ceiling — Haiku 4.5 64k, Haiku 5.5/Sonnet/Opus 128k, Fable 32k. A `max_tokens` value above the ceiling is rejected.
3. **`output_config.effort`**: Haiku 4.x rejects top-level effort control with HTTP 400; Haiku 5.5 accepts it. Fable accepts top-level effort at every level but rejects per-message effort (an `output_config` inside a `messages[]` entry) unless the client sent the `mid-conversation-output-config-2026-07-01` beta.
4. **`thinking.type`**: Fable rejects `thinking.type='disabled'` (already handled by `_supports_disabled_thinking`, which drops the field). Sonnet 5.5 also rejects `'disabled'` but accepts `'between_tools'`; it is rewritten rather than dropped so thinking stays operational. Haiku 5.5 rejects `'disabled'` at `xhigh` and `max` effort and has no `between_tools`; `_rejects_disabled_thinking_at_high_effort` drops the field there.

**Gates fail open** (one deliberate exception: the Haiku effort and adaptive-thinking gates match any `haiku` ID except `haiku-5-5`, so an unknown future Haiku is stripped like Haiku 4.x until its docs are checked): an unknown model family keeps the field and lets any rejection surface naturally. `build_body` in `anthropic_transform.py` is the single enforcement point for body fields, and `merge_betas` is the single enforcement point for the matching beta headers (it drops the `server-side-fallback` beta alongside the `fallbacks` strip). The gates run on every outbound request, keyed on the resolved model only — not on whether routing changed the model — because the mapper cannot see the routing decision and an unconditional gate is correct in both cases. Nothing runs post-dispatch in handlers. Every gate logs at debug, matching the existing gates in the module.

## Decision

**Fallbacks:** Remove `payload['fallbacks']` and the `'server-side-fallback'` beta entirely from every outbound body in `build_body`, unconditionally. Anthropic validates each fallback target against the primary model; a sonnet-specific list sent with any other tier returns `"... is not a valid fallback target"`. The proxy cannot know the client's original target rule so cannot preserve fallbacks; stripping avoids the error. No model family is modeled as accepting fallbacks, the gate is presence-in-payload, so absence is always safe. The cost is losing server-side retry after a safety refusal.

**max_tokens:** Define a `MODEL_OUTPUT_LIMITS` dict in `anthrouter/model_config.py` with per-model ceilings keyed by resolved model ID (claude-fable-5-1: 32768, claude-opus-5-5: 128000, claude-sonnet-5-5: 128000, claude-sonnet-4-6: 128000, claude-sonnet-4-5: 128000, claude-haiku-4-5-20251001: 64000, claude-haiku-5-5: 128000). In `build_body`, after model resolution, clamp `payload['max_tokens']` down to the ceiling for the resolved model when the ceiling exists and `max_tokens` exceeds it. Unknown model IDs pass through unchanged (fail open).

**output_config.effort:** Strip top-level `output_config.effort` from the payload when the resolved model does not support it (`'haiku'` substring in the resolved ID, except Haiku 5.5, which supports effort — matched by `'haiku-5-5'`). Remove the entire `output_config` object if effort was its only key. Fable is deliberately absent from this gate: it accepts top-level effort, and the earlier anthproxy strip (c079c4e) was over-broad. Separately, strip `output_config.effort` from each `messages[]` entry when the resolved ID contains `'fable'` and no client beta starts with `mid-conversation-output-config` (`_supports_per_message_effort`). The beta is prefix-matched so a date-stamp revision keeps the gate open. Other keys in a per-message `output_config` are kept; the object is removed when effort was its only key. Non-dict entries and non-dict `output_config` values cross untouched.

**thinking.type:** Add `_requires_between_tools(model_id)`, true when `'sonnet-5-5'` is a substring of the resolved ID. When it is true and `thinking.type == 'disabled'`: if `output_config.effort` is `'xhigh'` or `'max'`, drop `thinking` (between_tools is not accepted at those efforts); otherwise rewrite `thinking` to `{'type': 'between_tools'}`. Log at debug naming the model and effort. `'adaptive'` and `'enabled'` are never touched by this gate. A second gate drops `thinking` when `thinking.type == 'disabled'`, the resolved ID contains `'haiku-5-5'` and effort is `'xhigh'` or `'max'`. Order inside `build_body`: the effort strip runs before this gate, so the effort check reads the value the request will actually carry.

## Consequences

- All outbound bodies have fallbacks stripped, eliminating fallbacks-not-supported HTTP 400 errors. Clients lose server-side fallback retry on safety refusals.
- Requests routed to lower tiers no longer produce max_tokens-exceeds-ceiling HTTP 400s; max_tokens is clamped to the routed model's ceiling in build_body.
- Top-level effort is removed from Haiku 4.x requests and kept for Haiku 5.5 and for Fable, so Fable clients retain the effort levels the model supports.
- Per-message effort is removed from Fable requests that lack the `mid-conversation-output-config` beta, eliminating the `requires a model that supports per-turn effort` rejection; with the beta it crosses unchanged. Other models keep per-message effort (fail open).
- Sonnet 5.5 receives thinking as `'between_tools'` when the client sent `'disabled'`, keeping thinking operational.
- Thinking is dropped entirely when effort is `'xhigh'` or `'max'` and Sonnet 5.5 is the resolved model, avoiding an unworkable state (between_tools unsupported at those efforts). Haiku 5.5 likewise loses `'disabled'` thinking at those efforts.
- Gates fail open: unknown models keep fields (except unknown future Haiku IDs, see above). A model outside every supported family list is left alone.

## Origin

Ported from anthproxy commits f672ba6 (`fix: never send fallbacks to Anthropic`), 40487a8 (`fix: drop fallbacks and clamp max_tokens when routing changes model`), c079c4e (`fix: strip output_config.effort for Fable models`), 78051f5 (`fix: add haiku to system-role rejection gate, eliminating per-message effort rejection`), and c3fdb0e (`Use between_tools thinking type for sonnet-5-5`). The Fable effort gate was narrowed to per-message only in anthrouter #16, after `docs/research/2026-10-07-fable-effort-support.md` showed Fable accepts top-level effort.
