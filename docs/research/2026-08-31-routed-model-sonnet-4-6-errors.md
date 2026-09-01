# Investigation: sonnet-4-6 Model Errors on Session 6f6d395e

## Summary

All 10 errors (5+5 in bursts at 2026-09-01T05:12:45Z and 05:13:57Z) routed to `claude-sonnet-4-6` received `400 — invalid_request_error` from the Anthropic API within 459–684ms, producing zero tokens. The root cause is a **model lock configuration that bypasses alias resolution**, causing an unrecognized literal model name to reach the API.

## Ground-Truth Evidence from SQLite

All 10 error rows in `~/.anthrouter/anthrouter.db` show:
- `status`: `error`
- `error`: `400 — invalid_request_error`
- `routed_model`: `claude-sonnet-4-6`
- `reason_code`: `missing_final_user_text`
- `duration_ms`: 459–684 (fast rejects, not retries)
- `input_tokens`, `output_tokens`: NULL (API rejected before processing)

Successful rows in the same session all show:
- `routed_model`: either `sonnet` or `haiku` (never the full ID)
- `status`: `success`
- `input_tokens`, `output_tokens`: populated

## Code Path: Why Only Errors Get `claude-sonnet-4-6`

### The failure path (missing_final_user_text)

When `build_routing_summary()` returns None (line 1667 in `anthrouter/model_router.py`):

1. **Line 1671**: `fallback = routing_baseline if baseline_model else requested`
2. **Line 1627**: `baseline_model = lock if lock != 'off' else None` (from `anthrouter/handlers.py:627`)
3. **Line 1672**: `payload['model'] = fallback` — sets the routed_model to the lock
4. **Line 1675**: Returns `ModelRoutingDecision(...routed_model=fallback...)`

**The model lock is hardcoded in config** (`~/.anthrouter/config.env`):
```
ANTHROUTER_ARGS="--enable-ui --lock-requested-model claude-sonnet-4-6"
```

**But custom aliases override the alias table** (same config.env):
```
ANTHROUTER_MODEL_ALIASES="fable:claude-fable-5,opus:claude-opus-5,sonnet:claude-sonnet-5"
```

### Why successful rows don't hit this path

Successful rows have final user text and are routed via the classifier (or cached tier):

- **Classifier path** (`line 1787+`): The routing summary is built successfully, passes to the classifier, which returns a tier label (`'sonnet'`, `'haiku'`, etc.). These tier aliases are then resolved by `resolve_model()` (model_config.py:31–54) using the custom alias table.
- **Walkback/cache path** (`line 1685–1725`): Text-less continuations replay the cached tier (e.g., `'sonnet'`), which resolves correctly via the alias table.

### Model resolution: the missing step

When `payload['model'] = fallback` is set to the literal lock string `'claude-sonnet-4-6'`, it is **never passed through `resolve_model()`**:

- **`resolve_model()` location** (`anthrouter/model_config.py:31–54`): Resolves tier aliases (`'sonnet'` → custom alias → `'claude-sonnet-5'`) and full model IDs. Line 54 returns unknown names verbatim.
- **Where it IS called**: In the mapper (`anthropic_transform.py`) when constructing the upstream request (after routing decisions are made).
- **Why this breaks**: The lock `'claude-sonnet-4-6'` is not a key in the custom alias table (which only has `fable`, `opus`, `sonnet`), so it passes through literally.
- **API rejects it**: The Anthropic API does not recognize `claude-sonnet-4-6` as a valid model ID and returns `400 — invalid_request_error`.

The correct ID (given the custom aliases) would be `'claude-sonnet-5'`, which IS in the upstream model list and would succeed.

## Why Missing Final User Text Triggers This

The `missing_final_user_text` reason code (triggered at `line 1668–1680` in `model_router.py`) occurs when:
- `build_routing_summary()` fails (malformed `messages`, no usable user text)
- Common in tool-result-only continuations OR when a sub-agent is spawned with a highly synthetic payload

The 10 errors cluster at times when the user was spawning 4–5 parallel sub-agents ("critic coordinator" spawning multiple reviewers). These sub-agents may have had:
- Tool-result-only payloads (no fresh user text)
- Synthetic/boilerplate final message that didn't survive `build_routing_summary()`

## Root Cause: Configuration Bug

**This is a configuration problem, not a code bug.** The `--lock-requested-model` flag is intended to lock all routing to a single tier for cost control, but:

1. **Inconsistency**: The lock is set to a full model ID (`claude-sonnet-4-6`) while the router expects tier aliases (`sonnet`, `opus`, etc.). Tier aliases are what get resolved via the alias table; full IDs bypass that step.
2. **Mismatch with custom aliases**: The config overrides the default alias table to map `sonnet → claude-sonnet-5`, but the lock still points to `claude-sonnet-4-6` (the OLD default).
3. **No fallback validation**: When the lock is set to a value not present in the alias table, the code doesn't validate or resolve it; it flows through literally to the API.

## Fix

Set the lock to a tier alias that exists in the custom alias table:

```bash
--lock-requested-model sonnet
# or
ANTHROUTER_ARGS="--enable-ui --lock-requested-model sonnet"
```

This will resolve via the custom alias table to `claude-sonnet-5` and match the successful routing path.

Alternatively, update the lock to an explicit model ID that the custom aliases table contains:

```bash
--lock-requested-model claude-sonnet-5
```

Both will ensure `missing_final_user_text` fallback paths resolve correctly to a model the Anthropic API recognizes.
