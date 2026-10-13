---
artifact-type: adr
lineage-rules: root
---

**Status**: Approved

# Locked baseline governs fail-closed routing decisions

`--lock-requested-model` substitutes the client's model with the operator's baseline at the top of `route_model()`, before any routing path runs: `payload['model'] = baseline_model` (`model_router.py:1639-1640`). The substitution is deliberate — the classifier, the routing summary and the tier cache all need a consistent input, and the client's original model is preserved separately in `requested` for the response echo.

Three fail-closed returns run after that substitution and report the client's model instead of the baseline that the substitution already wrote into the payload:

1. `disabled` (`model_router.py:1642-1649`) — reports `raw_model`.
2. `model_not_eligible` (`model_router.py:1652-1659`) — reports `raw_model`.
3. The classifier fail-closed return (`model_router.py:2019-2023`) — reports `routed_model=requested, applied=False`.

A fourth return, `missing_final_user_text` (`model_router.py:1718-1729`), does the opposite: it re-sets `payload['model'] = routing_baseline if baseline_model else requested` and reports that same value, so its record matches its wire.

The disagreement is observable. During the 2026-10-13 classifier rate-limit storm, one request produced `Model routing: requested=claude-haiku-4-5-20251001 routed=claude-haiku-4-5-20251001 applied=False reason=classifier_failed` followed by `Routing request: … model=sonnet` (`docs/research/2026-10-13-classifier-429-triage-and-fix-plan.md`, issue E). Sonnet served the request; the database row priced it as Haiku. Because `applied` is false, `db.py:303-305` also forces `net_savings_usd` to NULL, so the row carries neither a correct cost nor a savings figure.

Two readings of `CLAUDE.md`'s "routing failures always fail closed to the originally-requested model — never fail open to a more expensive or more permissive tier" are defensible. Read as a statement about the wire, the current code violates it: Haiku was requested and Sonnet was dispatched. Read as a statement about the routing decision, the code holds: the decision object never names a pricier tier. The flag's own help text (`config.py:688-694`) supports a third reading — the operator asked for everything to be served at the baseline, so dispatching the baseline is the instruction being followed, and only the record is wrong.

## Decision

**The model a fail-closed path dispatches is the model it records.** Every fail-closed return that follows the lock substitution reports the baseline, matching the existing `missing_final_user_text` precedent rather than contradicting it:

- `routed_model = routing_baseline` (which equals `requested` when no lock is active).
- `applied = (routing_baseline != requested)`.
- `payload['model']` is left at — or explicitly re-set to — `routing_baseline`.

This applies to the `disabled`, `model_not_eligible` and classifier fail-closed returns. `requested_model` continues to carry the client's original model, and the response still echoes the client's model name, unchanged by this decision.

The rejected alternative was to restore `payload['model'] = requested` on every fail-closed return, so that a routing failure means "client's model, untouched". It is the stronger literal reading of the fail-closed invariant, but it makes `--lock-requested-model` apply only to successful classifier decisions — the operator's baseline would silently lapse on every classifier timeout, 429 or malformed-summary request — and it would require changing the `missing_final_user_text` precedent to match.

The fail-closed invariant is therefore restated in terms of the *routing decision*, not the wire: a failure never selects a tier the configured baseline would not already have served. With a lock active the baseline is the operator's choice and may be pricier than the client's model; without a lock, `routing_baseline == requested` and the wire-level and decision-level readings coincide.

## Consequences

- Cost attribution becomes truthful for locked fail-closed requests, including the direction that previously hid a cost increase. A Haiku request served by a Sonnet baseline is recorded, priced and tiered as Sonnet.
- `applied` becomes true for locked fail-closed rows where the baseline differs from the client's model, so `net_savings_usd` is computed instead of forced to NULL. Where the baseline is pricier, that figure is negative — which is the fact, not a bug.
- Historical rows and new rows will disagree about identical situations. Existing rows are not backfilled: the model actually put on the wire was never recorded, so it cannot be recovered from the database (only from the log, by pairing `Model lock: forcing routing baseline` with the following `Routing request:` line).
- The four fail-closed paths stop contradicting each other, so `reason_code` no longer predicts whether `routed_model` can be trusted.
- No new configuration flag, no new reason code and no schema change. The `ReasonCode` literal is untouched.
- Unlocked behaviour is unchanged: with `baseline_model` unset, `routing_baseline` is `requested`, every assignment above is a no-op, and `applied` stays false.

## Origin

Issue E of `docs/research/2026-10-13-classifier-429-triage-and-fix-plan.md`, found while triaging the 2026-10-13 classifier rate-limit storm. The defect predates that incident — the storm only made it visible by driving 69 requests down a fail-closed path in 45 minutes.
