---
artifact-type: adr
lineage-rules: root
---

# Volatility tracker observes the post-strip system prompt

The volatility tracker (`prompt_volatility.py`) observes system blocks and flags those whose values change on every request within a session, indicating cache-hostile variation. When the sanitizer is enabled and strips allowlisted blocks from `payload['system']`, the tracker must observe the *post-strip* system to avoid false positives.

If the tracker observed the pre-strip system, every allowlisted block (e.g. `x-anthropic-billing-header` with a unique per-request value) was flagged on every request even after the strip dropped it. In strip mode, flagged-but-stripped blocks reported as "Not stripped: no allowlist match" — a contradiction, because the allowlist had covered them and the strip had just removed them. This produced tens of thousands of false warnings per deployment day and false volatility flags in the admin UI.

## Decision

When `sanitize_system_prompt` runs in `strip` mode, observe the system *after* `strip_volatile_system_blocks` has stripped it; when running in `warn` mode, observe the original unmodified system (nothing is stripped in warn mode). One `observe(session_id, payload['system'])` call after the strip branch serves both modes; in warn mode the value is unchanged. The `observe` signature does not change.

When constructing flagged-block warning messages, check whether the block at that index is a volatile block (using the same predicate the sanitizer used to strip). If it is, the message text is "Not stripped: mode is warn"; if it is not volatile, the text is "Not stripped: no allowlist match" (matching the strip mode case where the block was not allowlisted and was therefore not stripped).

- Only blocks that are actually dispatched to the upstream API are flagged as volatile. Allowlisted blocks stripped by the sanitizer are no longer reported as unrecognized.
- False positives for every-request volatility are eliminated. A session with a per-request `cc_prompt_id` inside an allowlisted `x-anthropic-billing-header` block will no longer flag it as volatile on every request; the sanitizer strips it and the tracker never sees it.
- In `warn` mode, allowlisted blocks are reported as "Not stripped: mode is warn" rather than "No allowlist match", accurately reflecting the reason they remain in the dispatched system.
- Flagged-block warning indices now refer to the post-strip array (in strip mode) or the original array (in warn mode), matching what is actually dispatched and what the operator needs to debug.

## Origin

Ported from anthproxy commit 918b28a (`fix: observe post-strip system in volatility tracker`).
