---
artifact-type: adr
lineage-rules: root
---

# Inline role:'system' messages are rewritten as user-turn blocks, gated by model

anthrouter never had the unconditional system-role fold that anthproxy inherited from Sonnet 4.6's rejection of inline `role:'system'` messages. This decision adds two related pieces: a per-model gate that identifies rejecting models, and an inline rewrite for those models that preserves `cache_control` breakpoints by embedding system text into adjacent user messages instead of folding it into top-level `system[]`.

Modern models (Sonnet 5.5, Opus 5.5) accept `role:'system'` in `messages[]` natively. Some models (Sonnet 4.6, Haiku, Claude 4.5 Sonnet) reject them with HTTP 400. For rejecting models, anthrouter routes requests toward cheaper tiers, so a payload valid for the requested tier can be rejected by the routed tier. The rewrite solves this by removing inline system messages entirely while preserving their content and position in the conversation.

## Decision

Add a `_rejects_system_role_in_messages(model_id: str) -> bool` predicate and a tuple `_SYSTEM_ROLE_REJECTING_FAMILIES` that names model families known to reject inline system messages. Start with `('sonnet-4-6', 'claude-4-5-sonnet', 'claude-sonnet-4-6', 'claude-haiku-4-5-20251001', 'haiku')`, matched as substrings of the resolved model ID.

Rewrite inline `role:'system'` messages as `<system-reminder>` text blocks appended to the adjacent user message or inserted as standalone user turns, but only when the resolved model matches the rejecting gate. The rewrite processes `messages[]` in document order and builds a new list. For each `role:'system'` message, take its text (a string, or the `text` blocks joined by newlines), wrap it as `<system-reminder>\n…\n</system-reminder>`, and make one `{"type": "text"}` block. If the message carried `cache_control` (on any text block, or at message level), copy the last such value onto the new block. Place the block by the first rule that matches:

1. **The last message in the new list is `role:'user'`:** append the block to that message's content. A string content becomes a one-block list first. This preserves the system content at its original position.
2. **The last message is `role:'assistant'` with at least one `tool_use` block:** drop the block and log a WARN naming the model. A user message here would separate the tool call from its result, which the API rejects.
3. **Otherwise** (the list is empty, or the last message is an assistant turn without `tool_use`): insert the block as a standalone `role:'user'` message at that position.

**Model gate runs at dispatch time.** `build_body(payload, *, force_rewrite=False)` in `anthropic_transform.py` checks the gate before the rewrite. When `force_rewrite` is true or `_rejects_system_role_in_messages(resolved_model)` is true, run the rewrite. When false, send `role:'system'` entries as-is.

**Fail-open retry.** When `build_body` is called with `force_rewrite=False` and the gate returns False (the model accepts inline system), the payload is sent unchanged. If the model rejects it with HTTP 400 whose body mentions both `system` and `role`, exactly one retry is performed: rebuild with `force_rewrite=True`, record the new outbound body, log a WARN naming the resolved model and the tuple, and retry once. A second 400 is returned to the client unchanged. A model already in the tuple is rewritten on the first attempt, so it never retries.

**Scope.** Only the Anthropic-protocol mapper (`anthropic_transform.py`) changes. The rewrite is specific to Anthropic Messages shape and does not apply to other backends. It is an addition, not a replacement — anthrouter never had the fold that ADR-0032 gated in anthproxy, so this ADR establishes the rewrite pattern that anthproxy later formalized.

## Consequences

- Models in `_SYSTEM_ROLE_REJECTING_FAMILIES` receive inline system text embedded as `<system-reminder>` blocks in user turns, not in the top-level `system` field. This shifts instruction authority from system-level (system-prompt tokens) to user-level (user-message tokens). The tags signal to the model that the block is a harness instruction, not user input.
- Expected effect: `_SYSTEM_ROLE_REJECTING_FAMILIES` routed requests preserve `cache_control` breakpoints and avoid top-level `system[]` growth per turn, restoring prompt-cache read ratios toward the 85%+ baseline achieved by models that accept inline system turns.
- Top-level `system[]` is never folded into; the rewrite only embeds into existing user turns or creates new standalone user turns.
- When a system message appears after an assistant `tool_use` turn (the unobserved case), the block is dropped and logged at WARN. That request loses the system content and its trailing `cache_control` breakpoint, but remains valid. The message count may rise by one if a standalone user turn is created instead, but the placement rules are conservative and avoid message-sequence errors.
- Fable and future models are initially outside the tuple. If they reject inline system with HTTP 400, a single retry rewrites and retries. If they genuinely require the rewrite consistently, they are added to the tuple and rewritten on first attempt thereafter.
- The retry lives in `transport.py`'s `_send_with_retries` (line ~110–180) alongside the existing thinking-block retry, using the same pattern: detect the specific error, set a flag, and retry once with the corrected payload.

## Origin

Ported from anthproxy ADR-0032 (`docs/adr/0032-system-role-fold-gated-by-model.md`, commit 444b941) and ADR-0036 (`docs/adr/0036-inline-system-rewrite-as-user-turn.md`, commit 2c31e9a). The gate and fail-open retry come from ADR-0032; the rewrite design and placement rules come from ADR-0036. Combined into one ADR for anthrouter because the rewrite replaces any fold from the start — anthrouter adds both pieces together, not sequentially.
