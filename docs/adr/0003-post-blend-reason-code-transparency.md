---
artifact-type: adr
lineage-rules: root
---

# ADR-0003: Reason code reflects the post-blend routing outcome, not the raw classifier score

## Context

Under the ADR-0010 weighted system-prompt/user-prompt blend, `dispatch_reason` is set from the raw user-prompt score (`model_router.py:1989`, e.g. `classifier_deep`) before the blend runs, and is never updated when `_apply_weighted_blend()` (`model_router.py:937-979`) produces a different final tier that actually selects the routed model (`model_router.py:2002,2016`). A user-prompt score of 78 ("deep") blended against a low system-prompt score can land the final tier at "standard", routing to sonnet — while the DB/UI still shows `classification=deep, reason_code=classifier_deep`, misrepresenting what was actually decided.

## Decision

Extend the `ReasonCode` literal with blended variants emitted whenever the post-blend final label differs from the raw user-prompt tier. The blend is bidirectional — `_apply_weighted_blend()` computes `weighted = round(sys_weight × sys_score + user_weight × user_score)`, so a high system-prompt score can upgrade a trivial user score (e.g. user=20, sys=90, weights 0.30/0.70 → weighted=41 → standard) just as a low system-prompt score can downgrade a deep one. All six mismatch pairs must be covered:

- `classifier_trivial_blended_standard` — user trivial, blend → standard
- `classifier_trivial_blended_deep` — user trivial, blend → deep
- `classifier_standard_blended_trivial` — user standard, blend → trivial
- `classifier_standard_blended_deep` — user standard, blend → deep
- `classifier_deep_blended_standard` — user deep, blend → standard
- `classifier_deep_blended_trivial` — user deep, blend → trivial

The existing `classifier_{tier}` codes are emitted unchanged when blend agrees with the raw score. Rejected alternative: a separate `blend_final_tier` column — rejected because `reason_code` is already the field both the INFO log line and every existing DB/UI consumer key off of, per its "stable reason_code" contract in `route_model()`. The two-field approach would also require DB migration for stored history.

The `classification` DB column is intentionally retained as the raw user-prompt score tier (pre-blend) and is NOT updated by this ADR. `reason_code` becomes the sole field conveying the actual post-blend routing outcome; `classification` is preserved for raw-score auditability — an operator can compare `classification` (what the user-prompt alone implied) against the `reason_code` (what the blend decided) to understand the system-prompt classifier's influence.

**Enforcement point:** In `route_model()`, after calling `_apply_weighted_blend()` at line 1995–1998 and receiving `label_or_tier` (the final blended tier), add a comparison block immediately after line 2002 (`score_or_label_or_tier = label_or_tier`):

```python
# ADR-0003: Emit blended reason codes when post-blend tier differs from raw user-prompt tier
# (only in classifier mode, checked by enclosing if-block not shown here)
if effective_mode == 'classifier' and user_prompt_tier is not None:
    if label_or_tier != user_prompt_tier:
        dispatch_reason_str = f'classifier_{user_prompt_tier}_blended_{label_or_tier}'
```

A format string (rather than a lookup dict) is used deliberately: it produces the identical six values with no dict to keep in sync against the `ReasonCode` literal as tiers are added or renamed. The `ReasonCode` literal itself remains the source of truth and is validated at type-check time; the runtime string is never checked against it, matching how `dispatch_reason_str = f'classifier_{user_prompt_tier}'` already works unchecked at line 1989.

This block updates `dispatch_reason_str` immediately after the blend runs, ensuring the final `reason_code` field in the returned `ModelRoutingDecision` reflects the post-blend outcome. When tiers match, `dispatch_reason_str` retains the original `classifier_{tier}` code set at line 1989.

**Test coverage:** At minimum, one test case must verify each of the six mismatch pairs produces the correct reason code (e.g., mock a user score of 20/trivial and system score of 90/deep, weights 0.30/0.70, weighted=41/standard, assert reason_code=`classifier_trivial_blended_standard`).

## Consequences

**Production code**: Audit of call sites that pattern-match on `classifier_trivial`/`classifier_standard`/`classifier_deep` outside logging: `db.py:443-445` (`get_routing_summary`) matches on the `classification` column, not `reason_code`, so it is unaffected. No other non-logging, non-test call site pattern-matches bare `classifier_{tier}` reason codes at runtime.

**Test code**: Any existing test that asserts a specific `reason_code` value for blended scenarios (e.g., when system-prompt weight > 0 and blend changes the tier) must be updated. Tests should cover:
- Blend agreement cases: verify reason_code is `classifier_{tier}` (unchanged from raw score).
- Each of the six mismatch pairs: verify the corresponding blended reason code is emitted.

The mapping is applied only in classifier mode with an active blend; all other routing paths — rules, tag, size-floor override, and walk-back cache replay — are unaffected because no `user_prompt_tier` comparison is produced for those paths.

**Affirmation-inheritance path is out of scope**: The `affirmation_classified` path (`model_router.py:1899-1915`) also calls `_apply_weighted_blend()` when in classifier mode (line 1866-1871) and can produce a `blend_label` that differs from the raw `aff_user_tier`, but its `ModelRoutingDecision` always sets `reason_code='affirmation_classified'` (line 1905) unconditionally — it never emits a blended variant. This ADR does not extend the blended reason codes to the affirmation path; doing so is a separate follow-up that would need its own reason-code scheme (`affirmation_classified` has no raw pre-blend `classifier_{tier}` code to diverge from in the DB/UI today).
