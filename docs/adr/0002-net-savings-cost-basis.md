---
artifact-type: adr
lineage-rules: root
---

# ADR-0002: Net savings is a per-request cost delta, not a fixed baseline

## Context

`requests.net_savings_usd` and `requests.classifier_overhead_usd` have existed in the schema since the initial `anthrouter` schema, are threaded through `RequestDB.record_request()`, and are summed in the admin Routing summary — but no code ever computes and passes a value for either, so every row is `NULL` and the UI always shows "—". The columns were scaffolded ahead of the computation being wired in. `ModelRoutingDecision` already carries `classifier_input_tokens` and `classifier_output_tokens` (`model_router.py:412-413`), so no structural change to the return type is required — all inputs are available at `handlers.py:_record_db`.

## Decision

Compute `net_savings_usd` per request as `compute_cost(requested_model, stats_dict) - compute_cost(routed_model, stats_dict) - classifier_overhead_usd`. Compute `classifier_overhead_usd` as `compute_cost(routing.classifier_model, {"input_tokens": routing.classifier_input_tokens, "output_tokens": routing.classifier_output_tokens})` — both fields come from `ModelRoutingDecision` (`model_router.py:412-413`); a synthetic dict is required because `compute_cost()` takes a stats dict, not raw integers. Rejected alternative: fixed always-opus baseline — rejected because it overstates savings for requests the client would never have sent to opus.

**Scope boundary**: `classifier_overhead_usd` covers only the user-prompt classifier call. When `auto_model_routing_system_prompt_weight > 0`, the weighted blend also fires `_classify_system_prompt()` (`model_router.py:861`), whose return type `tuple[int, bool]` carries no token counts; those costs are not surfaced through the call chain and are excluded from this ADR. Extending `_apply_weighted_blend()` to return system-prompt classifier token counts is a separate structural change. **UI disclosure**: the UI label for `classifier_overhead_usd` is set unconditionally to "User-prompt classifier overhead" (a static label, not a conditional one — the browser bundle has no access to server-side config like `auto_model_routing_system_prompt_weight`, and no config-exposure endpoint exists to check it). This static label makes the scope explicit regardless of whether the weighted blend is active.

**Affirmation-inheritance path is out of scope**: The `affirmation_classified` path (`model_router.py:1899-1915`, reason_code `affirmation_classified`) calls `_apply_weighted_blend()` when in classifier mode (line 1866-1871) but returns a `ModelRoutingDecision` with no `classifier_model`, `classifier_input_tokens`, or `classifier_output_tokens` set. Real classifier cost is incurred on this path but is NOT surfaced by this ADR — `classifier_overhead_usd` is recorded as `0.0` for these rows today (via the "no classifier call" rule below), which undercounts actual spend. Populating those three fields on the `affirmation_classified` return path is a separate, follow-up structural change; this ADR only wires computation for decisions that already carry populated classifier fields.

**Failure policy** (for `net_savings_usd`):
1. `compute_cost()` returns `None` for unrecognized model tiers (`db.py:181-183`); when either the requested or routed model is unrecognized, `net_savings_usd` is recorded as `NULL`.
2. When stats carry no token counts (upstream errored before emitting usage), `net_savings_usd` is recorded as `NULL` — cost delta cannot be measured without usage data.
3. When routing applies and both models are recognized and stats are present, `net_savings_usd` is computed and recorded. It may be zero (classifier ran but routed to the same model) or negative (classifier ran but routed to a more expensive tier) — both are accurate and accepted.

**Failure policy** (for `classifier_overhead_usd`):
- When no classifier call was made (rules/tag/size-floor paths, where `routing.classifier_model is None`), `classifier_overhead_usd` is recorded as `0.0` — definitively zero overhead, not unknown.
- When a classifier ran (`routing.classifier_model is not None`), compute `classifier_overhead_usd` from `routing.classifier_input_tokens` and `routing.classifier_output_tokens` regardless of whether `applied=True` or `applied=False`, and regardless of whether main-request stats are present. Classifier overhead depends only on routing token counts, not on the main request's usage data.
- If `routing.classifier_model` itself is unrecognized by `compute_cost()` (the model passed to the classifier overhead calculation, not the routed/requested model from the main dispatch), `classifier_overhead_usd` is recorded as `NULL`.

**Critical difference**: a routing decision where the classifier ran and confirmed the originally-requested model (e.g., user requests haiku, classifier scores standard→haiku, routed==requested, `applied=False`) must still record the real classifier cost in `classifier_overhead_usd` — this ensures the routing summary does not silently omit classifier spend when no model change occurs.

**Implementation contract**: `compute_cost()` signature is `compute_cost(model_str, stats_dict) -> float | None`. For classifier overhead, a synthetic dict is constructed: `{"input_tokens": routing.classifier_input_tokens, "output_tokens": routing.classifier_output_tokens}`. This must match the existing call-site behavior at `db.py:cost_estimate()` where a real-request stats dict is passed. The synthetic dict is valid because `compute_cost()` extracts only token counts from the dict via `.get()` calls (e.g., `.get('input_tokens', 0)`); cache token keys default to 0, which is semantically correct for classifier calls (no cache activity). **Maintenance invariant**: if `compute_cost()` is extended with a new token-type field that requires `.get()` with a non-zero default or a direct access (not `.get()`), the synthetic dict must be updated to include that field or explicitly set it to 0. A missing field would silently miscalculate classifier overhead.

## Consequences

**Separate handling for the two fields**: `handlers.py:_record_db` must compute both values **independently**, not as a paired unit.
- `net_savings_usd`: suppress to `NULL` for `applied=False` rows (no model change = no savings).
- `classifier_overhead_usd`: compute **unconditionally** whenever a classifier ran (`routing.classifier_model is not None`), regardless of `applied`. When the classifier confirms the originally-requested model (e.g., user requests haiku, classifier scores standard→haiku, routed==requested), `applied=False` but `classifier_overhead_usd` is real and non-zero; the routing summary must include it. Follow the Failure policy above: if a classifier ran, compute cost from `routing.classifier_input_tokens` and `routing.classifier_output_tokens`, even when `applied=False`.

**UI render scope**: `anthrouter/ui/src/views/Routing.tsx:42` currently renders only a "Net savings" `Metric`. This ADR adds a second `Metric` for `classifier_overhead_usd` (labeled "User-prompt classifier overhead", per the UI disclosure note above) to the same grid in `Routing.tsx`. `anthrouter/ui/src/api.ts:18-19` and `:97-98` already declare both fields in the TypeScript types — no type change is needed, only the missing render call.

**Ordering with ADR-0003**: When `auto_model_routing_system_prompt_weight > 0`, the weighted blend (ADR-0003) executes first and may change the routed model. The savings computation always uses the **final post-blend `routed_model`** from the `ModelRoutingDecision` — the blend decision is finalized before `handlers.py:_record_db()` is called, so no recomputation or retroactive adjustment is required.

**Note on applied=False**: When routing does not apply (`applied=False`), the condition `applied=(routed != requested)` at `model_router.py:2024` guarantees that no routing decision changed the model. However, `net_savings_usd` and `classifier_overhead_usd` must be handled separately for this path — this is a **required code change**:
- `net_savings_usd`: suppress to `NULL` (no model change = no savings). The existing logic at `db.py:280` correctly assigns `None` for this field.
- `classifier_overhead_usd`: do NOT suppress to `NULL` when a classifier ran (`routing.classifier_model is not None`). When a classifier ran and confirmed the originally-requested model, `applied=False` but classifier_overhead_usd is real and non-zero; the routing summary must include it. The current code at `db.py:281` incorrectly suppresses this field to `None` unconditionally for all `applied=False` rows — this must be changed to set only `net_savings_usd = None` in that branch, leaving `classifier_overhead_usd` to be computed in `handlers.py:_record_db` as specified in the Failure policy.
