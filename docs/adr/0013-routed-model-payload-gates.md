---
artifact-type: adr
lineage-rules: root
---

# Routed-model payload gates strip fields invalid for downtiered models

Model-tier routing changes the requested model to a cheaper tier (haiku, sonnet) or vice versa. Four payload fields valid for the client's requested tier can be rejected by the routed tier with HTTP 400:

1. **`fallbacks`**: Anthropic validates each fallback target against the routed model's `allowed_fallback_models`. A fallback chosen for opus is invalid for haiku.
2. **`max_tokens`**: Each model family has an output ceiling — Haiku 64k, Sonnet/Opus 128k, Fable 32k. A `max_tokens` value above the ceiling is rejected.
3. **`output_config.effort`**: Haiku and Fable reject per-turn effort control with HTTP 400.
4. **`thinking.type`**: Fable rejects `thinking.type='disabled'` (already handled by `_supports_disabled_thinking`, which drops the field). Sonnet 5.5 also rejects `'disabled'` but accepts `'between_tools'`; it is rewritten rather than dropped so thinking stays operational.

**Every gate fails open:** an unknown model family keeps the field and lets any rejection surface naturally. `build_body` in `anthropic_transform.py` is the single enforcement point for body fields, and `merge_betas` is the single enforcement point for the matching beta headers (it drops the `server-side-fallback` beta alongside the `fallbacks` strip). The gates run on every outbound request, keyed on the resolved model only — not on whether routing changed the model — because the mapper cannot see the routing decision and an unconditional gate is correct in both cases. Nothing runs post-dispatch in handlers. Every gate logs at debug, matching the existing gates in the module.

## Decision

**Fallbacks:** Remove `payload['fallbacks']` and the `'server-side-fallback'` beta entirely from every outbound body in `build_body`, unconditionally. Anthropic validates each fallback target against the primary model; a sonnet-specific list sent with any other tier returns `"... is not a valid fallback target"`. The proxy cannot know the client's original target rule so cannot preserve fallbacks; stripping avoids the error. No model family is modeled as accepting fallbacks, the gate is presence-in-payload, so absence is always safe. The cost is losing server-side retry after a safety refusal.

**max_tokens:** Define a `MODEL_OUTPUT_LIMITS` dict in `anthrouter/model_config.py` with per-model ceilings keyed by resolved model ID (claude-fable-5-1: 32768, claude-opus-5-5: 128000, claude-sonnet-5-5: 128000, claude-sonnet-4-6: 128000, claude-sonnet-4-5: 128000, claude-haiku-4-5-20251001: 64000). In `build_body`, after model resolution, clamp `payload['max_tokens']` down to the ceiling for the resolved model when the ceiling exists and `max_tokens` exceeds it. Unknown model IDs pass through unchanged (fail open).

**output_config.effort:** Strip `output_config.effort` from the payload when the resolved model does not support it (`'haiku'` or `'fable'` substrings in the resolved ID). Remove the entire `output_config` object if effort was its only key.

**thinking.type:** Add `_requires_between_tools(model_id)`, true when `'sonnet-5-5'` is a substring of the resolved ID. When it is true and `thinking.type == 'disabled'`: if `output_config.effort` is `'xhigh'` or `'max'`, drop `thinking` (between_tools is not accepted at those efforts); otherwise rewrite `thinking` to `{'type': 'between_tools'}`. Log at debug naming the model and effort. `'adaptive'` and `'enabled'` are never touched by this gate. Order inside `build_body`: the effort strip runs before this gate, so the effort check reads the value the request will actually carry.

## Consequences

- All outbound bodies have fallbacks stripped, eliminating fallbacks-not-supported HTTP 400 errors. Clients lose server-side fallback retry on safety refusals.
- Requests routed to lower tiers no longer produce max_tokens-exceeds-ceiling HTTP 400s; max_tokens is clamped to the routed model's ceiling in build_body.
- The effort field is removed from Haiku and Fable requests, eliminating per-turn effort rejection.
- Sonnet 5.5 receives thinking as `'between_tools'` when the client sent `'disabled'`, keeping thinking operational.
- Thinking is dropped entirely when effort is `'xhigh'` or `'max'` and Sonnet 5.5 is the resolved model, avoiding an unworkable state (between_tools unsupported at those efforts).
- Every gate fails open: unknown models keep fields. A model outside every supported family list is left alone.

## Origin

Ported from anthproxy commits f672ba6 (`fix: never send fallbacks to Anthropic`), 40487a8 (`fix: drop fallbacks and clamp max_tokens when routing changes model`), c079c4e (`fix: strip output_config.effort for Fable models`), 78051f5 (`fix: add haiku to system-role rejection gate, eliminating per-message effort rejection`), and c3fdb0e (`Use between_tools thinking type for sonnet-5-5`).
