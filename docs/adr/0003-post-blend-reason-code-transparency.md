---
artifact-type: adr
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

## Consequences

Audit of call sites that pattern-match on `classifier_trivial`/`classifier_standard`/`classifier_deep` outside logging: `db.py:443-445` (`get_routing_summary`) matches on the `classification` column, not `reason_code`, so it is unaffected. No other non-logging, non-test call site pattern-matches bare `classifier_{tier}` reason codes. No additional call-site changes are required by this ADR.
