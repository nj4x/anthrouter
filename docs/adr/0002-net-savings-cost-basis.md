---
artifact-type: adr
---

# ADR-0002: Net savings is a per-request cost delta, not a fixed baseline

## Context

`requests.net_savings_usd` and `requests.classifier_overhead_usd` have existed in the schema since the initial `anthrouter` schema, are threaded through `RequestDB.record_request()`, and are summed in the admin Routing summary — but no code ever computes and passes a value for either, so every row is `NULL` and the UI always shows "—". The columns were scaffolded ahead of the computation being wired in. `ModelRoutingDecision` already carries `classifier_input_tokens` and `classifier_output_tokens` (`model_router.py:412-413`), so no structural change to the return type is required — all inputs are available at `handlers.py:_record_db`.

## Decision

Compute `net_savings_usd` per request as `compute_cost(requested_model, stats_dict) - compute_cost(routed_model, stats_dict) - classifier_overhead_usd`. Compute `classifier_overhead_usd` as `compute_cost(routing.classifier_model, {"input_tokens": routing.classifier_input_tokens, "output_tokens": routing.classifier_output_tokens})` — both fields come from `ModelRoutingDecision` (`model_router.py:412-413`); a synthetic dict is required because `compute_cost()` takes a stats dict, not raw integers. Rejected alternative: fixed always-opus baseline — rejected because it overstates savings for requests the client would never have sent to opus.

**Scope boundary**: `classifier_overhead_usd` covers only the user-prompt classifier call. When `auto_model_routing_system_prompt_weight > 0`, the weighted blend also fires `_classify_system_prompt()` (`model_router.py:861`), whose return type `tuple[int, bool]` carries no token counts; those costs are not surfaced through the call chain and are excluded from this ADR. Extending `_apply_weighted_blend()` to return system-prompt classifier token counts is a separate structural change.

**Failure policy**:
- `compute_cost()` returns `None` for unrecognized model tiers (`db.py:181-183`); when either the requested or routed model is unrecognized, both `net_savings_usd` and `classifier_overhead_usd` are recorded as `NULL`.
- When no classifier call was made (rules/tag/size-floor paths, where `routing.classifier_model is None`), `classifier_overhead_usd` is recorded as `0.0` — definitively zero overhead, not unknown.
- When stats carry no token counts (upstream errored before emitting usage), `compute_cost()` returns `0` (not `None`); resulting in a `$0.00` savings figure. This is accepted: no usage means no routable cost delta was measured.

## Consequences

`handlers.py:_record_db` must compute and pass both values for `applied=True` rows. `applied=False` rows correctly suppress both fields to `NULL` today (`db.py:280-281` explicitly assigns `None` for that path); the fix is wiring the computation for the `applied=True` path only.
