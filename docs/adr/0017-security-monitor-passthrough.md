---
artifact-type: adr
lineage-rules: root
---

**Status**: Approved

# Security-monitor requests bypass the lock and routing

Claude Code's auto-mode security monitor sends a request whose system prompt starts with `You are a security monitor for autonomous AI coding agents.` It is not a conversation turn. Request #13224 (2026-10-23 18:44 UTC) shows the failure. `--lock-requested-model sonnet` rewrote the client's `claude-sonnet-5` to `claude-sonnet-5-5`, and the Classifier then ran on it. Upstream answered `429 rate_limit_error` with message `Error` and no rate-limit headers. Claude Code retried 4 times, and each retry paid for a new Classifier call (`docs/research/2026-10-23-rate-limit-errors.md`).

Commit `b80986f` made this worse. It set `HAPPY_NEW_YEAR_PREFIX = '64539a0e'`, which no request matches, and kept the real prefix in `_HAPPY_NEW_YEAR_PREFIX` (`anthrouter/handlers.py:70-71`). The local `<block>no</block>` reply stopped, so monitor requests went upstream through the lock.

ADR-0016 says the lock substitutes the client's model before any routing path runs. This ADR narrows that rule for security-monitor requests.

## Decision

1. **Detection.** `_has_happy_new_year_system_prompt()` in `handlers.py` matches any system text block that starts with `HAPPY_NEW_YEAR_PREFIX` (the security-monitor text). It does not require the first block.
2. **Passthrough.** In `_prepare` in `handlers.py`, a matched request skips `_route`, and so skips:
   - the lock substitution and its log line;
   - `route_model()`;
   - the session tier-cache read and write (`_apply_tier_cache`);
   - the session context update.

   The request goes upstream with the client's model. `routing` is `ModelRoutingDecision(requested_model=X, routed_model=X, applied=False, reason_code='security_monitor_passthrough')`. `ReasonCode` in `model_router.py` gains `security_monitor_passthrough`.
3. **Sanitization stays on.** The system prompt is about 142k characters. Dropping the sanitizer would lose prompt caching.
4. **Local reply behind a flag.** The `'64539a0e'` sentinel is removed. A new flag, `--security-monitor-local-reply` (default off), with env var `ANTHROUTER_SECURITY_MONITOR_LOCAL_REPLY`, turns on the local `<block>no</block>` reply. It follows the pattern of `activity_label_local_reply` (ADR-0015): CLI/env only, not in `EDITABLE_FIELDS`. It is an independent boolean, so it needs no cross-field validation.

The rejected alternative was to pass through every request without the Claude Code identity block (option 1 in the research note). It would also cover quota probes and plain SDK scripts. But it would drop routing and the lock for any client that does not send the identity, such as API-key traffic, where the identity gate was never observed. The monitor is the only identity-less request with a known, stable prefix and a known failure.

## Consequences

- Monitor requests use the model Claude Code chose. No Classifier call and no lock rewrite happen for them.
- If Sonnet 5.5 rejects this request shape but Sonnet 5 accepts it, the 429 stops (reading b). If every identity-less request is rejected, the 429 continues but the wasted Classifier calls stop (reading a). One controlled call on `claude-sonnet-5` will decide between them. It has not been made yet.
- Monitor requests are no longer priced or tiered by routing. Their cost shows as the requested model.
- Narrows ADR-0016 for this request class only. The lock still applies to all other requests.
