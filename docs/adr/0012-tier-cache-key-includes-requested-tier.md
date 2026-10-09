---
artifact-type: adr
lineage-rules: root
---

**Status**: Approved

# Tier-cache key includes the requested model's tier

The tier-cache key was `(session_key, first-user-message-hash)` — the same `ctx_key` used for context-floor tracking. A tier written when the client requested a Haiku-tier model was stored under the same key and replayed when the client later requested a Sonnet-tier model. `_cap_cached_tier` prevented the cached entry from *upgrading* the requested model, but it intentionally allowed downgrades: a cached `'haiku'` tier was kept when the requested tier was `'sonnet'`, routing the request to Haiku.

This mismatch became the remaining driver of `model_changed` cache misses after ADR-0010 increased classifier coverage and rewrite gates reduced the fold's impact. DB measurement showed `session_cached_tier` as reason_code for dozens of requests per day, with cross-tier downgrades producing turquoise prompt-cache misses because the cached prefix was keyed on the model that wrote it.

## Decision

Include the requested model's resolved tier (`haiku` / `sonnet` / `opus` / `fable`) as a third component of the tier-cache key: `(ctx_key, requested_tier)` a two-element tuple. The context key is the first element (a string: `session_key + '\x00' + first-user-message-hash`); the requested tier (a string) is the second.

Update the in-memory tier cache (`SessionState._routed_tier` OrderedDict) to use tuple keys instead of bare `ctx_key` strings. A Haiku-tier entry written by one turn is only replayed by subsequent turns whose requested model also resolves to Haiku. Sonnet-tier turns write and read their own cache slot.

**Key derivation:** The tier is derived from the requested model (the client's original `payload['model']`, or the baseline if `lock_requested_model` is set) using `classify_model_tier(baseline_model)` from `anthrouter/model_tier.py`. Non-tier model identifiers (models where `classify_model_tier` returns `'other'`) use a sentinel value `'_' + model` so a custom model matches only itself — consistent with the existing behavior in `_cap_cached_tier` for non-tier identifiers — preserving the allow-any-tier-to-a-custom-model fallback.

**No change to `_cap_cached_tier`:** The upgrade cap is still enforced within a tier slot. The key change ensures the cap is applied within the right scope.

## Consequences

- Tier-cache entries are more numerous (one slot per session × first-user-message × tier rather than per session × first-user-message).
- Cross-tier downgrade via `session_cached_tier` is eliminated.
- Same-tier replays (e.g. a Haiku affirmation replaying Haiku for the next Haiku-requested turn) still work as before.
- Sessions that genuinely switch requested-model tier mid-conversation (e.g. the operator changes `--lock-requested-model`) start each tier with a cold cache slot — correct behavior.

## Note

anthrouter has no bulk admin tier pin feature and does not need one. The three-component key is sufficient.

## Origin

Ported from anthproxy ADR-0034 (`docs/adr/0034-tier-cache-key-includes-requested-tier.md`), commit 2c31e9a and commit 5676a43.
