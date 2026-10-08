---
artifact-type: adr
lineage-rules: root
---

# Routing summary walks back over trailing non-user messages

`build_routing_summary` returned `None` when the last entry in `messages[]` was not `role:'user'`, triggering `reason_code='missing_final_user_text'` and bypassing classification. Modern Claude models (Sonnet 5.5, Opus 5.5) append `role:'system'` messages at the end of tool-call loops, so nearly every turn failed the guard and fell back to the tier cache. This degraded routing decisions to the cache alone, even for turns where the client sent fresh user text in an earlier message.

The router already performed content walk-back for short affirmations and truncated transcripts. A structural walk-back over the final-message role check applies the same pattern one step earlier: walk backwards over trailing non-user messages to find the last `role:'user'` message, use that as the final message, and proceed as before.

## Decision

Before the `role != 'user'` guard in `build_routing_summary`, walk backwards over `messages` to find the last entry whose `role` is `'user'`. Bound the walk with a new constant `_TRAILING_SCAN_LIMIT = 8`. If no `role:'user'` message is found within the limit, return `None` as before — conversations that genuinely carry no user text remain unclassifiable.

Use that final user message as `final_msg` for all subsequent processing (content extraction, tool-result detection, affirmation detection). Non-user trailing messages (`role:'system'`, `role:'assistant'`) are walked over. The walk-back stops at the last user message regardless of content — even if that message contains only tool results. If that final user turn has no text content, the text floor in the classifier input is empty and the classifier must handle it, or the tier cache is consulted as fallback.

**Exclusion from classifier input:** The `trailing_skipped` field (count of non-user messages skipped during walk-back) exists in the `RoutingSummary` dataclass as internal routing state and is never included in `to_classifier_json()`. The classifier input includes only content that actually describes THIS turn's complexity — message text, not the positions of system-message interleaving.

## Consequences

- The classifier runs for the large majority of 5.x session turns where it was previously skipped.
- `reason_code='missing_final_user_text'` reverts to its pre-5.x rate (~5%).
- Tier-cache usage drops to its intended role: fallback for short affirmations and tool-result-only turns, not primary routing for every classifier-able turn.
- Conversations that end on a genuine tool-result-only user turn (no text content at all, even after walk-back) still produce `missing_final_user_text` — no regression for that case.
- `_TRAILING_SCAN_LIMIT` keeps the scan to the last 8 entries, so very long histories are not walked in full.

## Origin

Ported from anthproxy ADR-0033 (`docs/adr/0033-routing-summary-trailing-message-walkback.md`), commit 2c31e9a and commits fdb18d2, 5676a43.
