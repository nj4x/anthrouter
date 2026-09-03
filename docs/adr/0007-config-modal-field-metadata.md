---
artifact-type: adr
lineage-rules: root
---

# ADR-0007: Config modal field metadata (hints, typed inputs, server-side enum enforcement)

## Status

Accepted

## Context

The runtime config editor (ADR-0005) renders every one of the 20 `EDITABLE_FIELDS` as a plain text input labeled with the raw field name — no description, no allowed-values hint, no type-aware control. `GET /admin/config` returns only `{restart_required, value}` per field. All human-readable documentation of these fields lives exclusively in argparse `help=` strings in `config.py`, which never reach the frontend.

A related gap was found while surveying: `validate_config()` does not enforce the enum choices for `log_level` (`DEBUG|INFO|WARNING|ERROR`), `auto_model_routing_mode` (`classifier|rules|tag`), or `sanitize_system_prompt` (`off|warn|strip`). Those choices are checked only by argparse at startup, so `POST /admin/config` can persist an invalid enum value to both memory and `config.env`.

## Decision

- **Backend is the single source of field metadata.** The per-field metadata (description, type, enum values, numeric min/max) is defined alongside `EDITABLE_FIELDS` in `config.py` and returned by `GET /admin/config`, extending each field's entry beyond `{restart_required, value}`. Rejected alternative: a hardcoded metadata map in the TypeScript frontend keyed by field name — that creates a second field registry that drifts from the Python one, and ADR-0005 already established the registry-in-`config.py` pattern precisely to avoid three-way drift.
- **One canonical description string per field; argparse help derives from it.** Each field's description lives once, in the metadata entry, written audience-neutral (no flag spellings, no CLI-only phrasing). The `argparse` `help=` for the corresponding flag is built from that same string — verbatim, or with mechanical additions argparse already handles (default value, env var name). Rejected alternative: separate hand-written web and CLI copies "kept adjacent so a change to one prompts a change to the other" — that is the same drift risk this ADR cites to reject the TypeScript map, and adjacency is a convention, not a constraint; prose drift between two free strings is not mechanically checkable. Same-file proximity does reduce drift compared to a cross-language, separately-built registry (a TS map also requires a `dist/` rebuild to stay current), but the only arrangement under which the "single source" claim actually holds end-to-end is one string with one derivation, so that is the decision.
- **Cross-field constraints name the paired field explicitly.** The hints for `auto_model_routing_system_prompt_weight` / `auto_model_routing_user_prompt_weight` (must sum to 1.0) and `auto_model_routing_trivial_threshold` / `auto_model_routing_standard_threshold` (trivial strictly below standard) each state the constraint and spell out the exact name of the paired field, so the user can locate it in the modal without guessing.
- **Typed inputs replace bare text where the type allows.** Enum fields render as `<select>` restricted to the allowed values; boolean fields render as checkboxes; numeric fields render as `<input type="number">` carrying `min`/`max` attributes where a bound exists, in addition to stating the bound in the hint text (the hint also covers semantics the attribute cannot express, such as "0 disables"). Free-form string fields stay as text inputs. The control type is derived from the served metadata, not hardcoded per field in the UI.
- **Server-side enforcement is closed for every served constraint in the same change.** The principle: no bound or allowed-values list appears in the metadata unless `validate_config()` enforces it — a hint the POST path does not back would be a lie. Numeric bounds are already almost entirely covered: `validate_config()` today enforces `long_context_threshold >= 0`, `prior_response_summary_limit` in [50, 32000], `system_prompt_cache_size >= 1`, `system_prompt_preview_limit >= 1`, `sse_keepalive_interval >= 0`, `db_retention_days >= 0`, the weight pair summing to 1.0, and `trivial < standard` threshold ordering; the served min/max values mirror those existing checks rather than introducing new ones. The residual gaps — the only constraints living solely in the argparse path — are the three enum fields above plus `min_confidence`'s [0, 1] range. This change moves all four into `validate_config()`, the enum checks sourced from the same metadata structure that feeds the modal so the advertised and enforced values cannot diverge. After this change, every constraint the modal displays is enforced server-side; nothing is deferred to the frontend.
- **Hint placement: inline caption, not tooltip.** Each field shows a one-sentence description directly below its label, always visible. Tooltips and expandable help icons were rejected: they hide exactly the information this change exists to surface, for no meaningful space saving in a scrollable modal.
- **Field ordering: grouped by subsystem, replacing alphabetical.** Fields are grouped Logging, Upstream, Model Routing (core toggle, then mode and classifier, then tiers and thresholds, then weights and confidence), Prompt Sanitization, Database, Server. Alphabetical order scatters paired fields whose hints reference each other; grouping puts them adjacent. Group membership and order are part of the served metadata.

## Consequences

- `GET /admin/config`'s response shape grows per-field keys. The modal derives everything — control type, options, bounds, hint, grouping — from the response, so registering a new field with metadata in `config.py` is still the single point of change; no UI edit or `dist/` rebuild is needed for a new field to render correctly.
- Enum values previously accepted by the POST path (any string) are now rejected with a 400 field-level error. Any `config.env` already holding an invalid enum value from the pre-fix window will fail argparse at next restart exactly as a hand-edited bad value always did; this change does not attempt migration.
- The metadata structure becomes load-bearing for validation, not just display: enum checks in `validate_config()` read from it. Adding an enum value means editing one place, and the UI, the validator, and the hint text all follow.
