# Research: Admin UI Config Modal and Auto-Model-Routing Settings

## Question

Does the anthrouter admin UI let a user change the `--auto-model-routing-*` classifier settings?

## Answer

**Yes, most of them.** The UI displays and allows live edit of 14 out of ~16 `auto_model_routing_*` routing flags. **Two are explicitly excluded** from the editable registry: `auto_model_routing_classification` and `auto_model_routing_task_tiers`.

---

## Full Set of Auto-Model-Routing Flags

All flags are defined in `anthrouter/config.py:328-347` as `Config` dataclass fields:

### Editable via UI (in EDITABLE_FIELDS registry):

**Live-editable (apply immediately, no restart needed):**

1. **`auto_model_routing`** (bool) — Toggle automatic routing on/off.
   - `FIELD_METADATA` line: `config.py:69`
   - UI renders as: checkbox
   
2. **`auto_model_routing_mode`** (str: `'classifier'|'rules'|'tag'`) — Which classification method to use.
   - `FIELD_METADATA` line: `config.py:98–102`
   - UI renders as: select enum
   - Validated in `validate_config()` line `config.py:380`

3. **`auto_model_routing_long`** (str) — Model forced by long-context floor (or `'off'` to disable).
   - `FIELD_METADATA` line: `config.py:86–90`
   - UI renders as: text input

4. **`auto_model_routing_long_context_threshold`** (int) — Token threshold for triggering long model.
   - `FIELD_METADATA` line: `config.py:77–83`
   - UI renders as: number input with min=0

5. **`auto_model_routing_affirmation_inherit`** (bool) — Bare "yes" inherits established tier.
   - `FIELD_METADATA` line: `config.py:127–131`
   - UI renders as: checkbox

6. **`auto_model_routing_confidence_bump`** (bool) — Enable confidence-score bumping.
   - `FIELD_METADATA` line: `config.py:118–123`
   - UI renders as: checkbox

7. **`auto_model_routing_min_confidence`** (float: 0.0–1.0) — Threshold for confidence-based bumping.
   - `FIELD_METADATA` line: `config.py:109–117`
   - UI renders as: number input with min=0.0, max=1.0
   - Validated in `validate_config()` line `config.py:389`

8. **`auto_model_routing_trivial_threshold`** (float) — Score cutoff for "trivial" tier.
   - `FIELD_METADATA` line: `config.py:91–96`
   - UI renders as: number input
   - Cross-field validation: must be strictly < `standard_threshold` (line `config.py:450`)

9. **`auto_model_routing_standard_threshold`** (float) — Score cutoff for "deep" tier (values between thresholds = "standard").
   - `FIELD_METADATA` line: `config.py:103–108`
   - UI renders as: number input
   - Cross-field validation: must be strictly > `trivial_threshold`

10. **`auto_model_routing_system_prompt_weight`** (float: 0.0–1.0) — Weight for system-prompt score in blend.
    - `FIELD_METADATA` line: `config.py:132–139`
    - UI renders as: number input with min=0.0, max=1.0
    - Cross-field validation: must sum to 1.0 with `user_prompt_weight` (line `config.py:459`)

11. **`auto_model_routing_user_prompt_weight`** (float: 0.0–1.0) — Weight for user-prompt score in blend.
    - `FIELD_METADATA` line: `config.py:140–147`
    - UI renders as: number input with min=0.0, max=1.0
    - Cross-field validation: must sum to 1.0 with `system_prompt_weight`

12. **`auto_model_routing_system_prompt_cache_size`** (int) — LRU cache size for system-prompt tier scores.
    - `FIELD_METADATA` line: `config.py:148–153`
    - UI renders as: number input with min=1
    - Validated in `validate_config()` line `config.py:421`

13. **`auto_model_routing_system_prompt_preview_limit`** (int) — Max characters of system prompt sent to classifier.
    - `FIELD_METADATA` line: `config.py:154–159`
    - UI renders as: number input with min=1
    - Validated in `validate_config()` line `config.py:426`

14. **`auto_model_routing_prior_response_summary_limit`** (int: 50–32000) — Max chars of prior response for affirmation enrichment.
    - `FIELD_METADATA` line: `config.py:160–166`
    - UI renders as: number input with min=50, max=32000
    - Validated in `validate_config()` line `config.py:414`

**File-editable (restart required):**

15. **`auto_model_routing_classifier_model`** (str) — Model alias for the internal classifier.
    - `FIELD_METADATA` line: `config.py:73–76`
    - UI renders as: text input, labeled "restart required"

### Excluded from UI (not in EDITABLE_FIELDS):

The comment at `config.py:17–21` explicitly lists:

- **`auto_model_routing_classification`** — A dict mapping tier labels to classifier models (e.g. `{"trivial":"haiku","standard":"sonnet"}`). Defined at line `config.py:332–334`. Not in `EDITABLE_FIELDS` by design.

- **`auto_model_routing_task_tiers`** — A JSON object mapping task names to model tiers (used when `--auto-model-routing-mode=tag`). Defined at line `config.py:337`. Not in `EDITABLE_FIELDS`.

Also excluded: `admin_token`, `model_aliases`, `anthrouter_home`, `enable_ui`, `request_history_size`.

---

## UI Implementation

**ConfigModal component** (`anthrouter/ui/src/components/ConfigModal.tsx`, lines 31–52):

- `GET /admin/config` (line 31) — fetches all current field values and metadata
- `POST /admin/config` (line 47) with `X-Admin-Token` header — saves edited values
- Uses metadata from response to render typed inputs (select for enums, checkbox for bool, number for int/float)

**Server endpoints** (`anthrouter/admin.py`):

- `handle_get()` (line 242) — read-only; returns field values + metadata
- `handle_post_config()` (line 157) — NEW (ADR-0005); validates and persists the full config map atomically
- Called from `handlers.py:487–488` for `POST /admin/config`

**Field metadata source** (`config.py:54–166`):

- `FIELD_METADATA` dict is the single authoritative source
- Each entry has: `description`, `type` (`'str'|'bool'|'int'|'float'`), optional `enum`, optional `min`/`max`, `group`
- `GET /admin/config` serves this metadata directly to the modal
- `validate_config()` enforces all advertised constraints server-side

---

## Live vs. Restart-Required Changes

**Live-editable** (14 routing flags) apply immediately in-memory without restart:
- Changes reflected in the next request
- Routed via `dataclasses.replace()` + atomic reference swap at `admin.py:209`

**File-editable** (1: `auto_model_routing_classifier_model`) written to `config.env` but require restart:
- Value persisted to disk for next process start
- Modal shows "restart required" badge (ADR-0005, section "File-editable fields")
- Classifier client is constructed once at startup, so live replacement would leave in-flight classifier calls talking to old target

---

## How Users Change These Settings

1. **Via Admin UI** (new, ADR-0005, ADR-0007):
   - Click "Edit configuration" in the admin UI Configuration tab
   - Token-gated: admin must set `--admin-token` / `ANTHROUTER_ADMIN_TOKEN` at startup to enable writes
   - Modal shows every field with typed inputs, enum selects, bounds validation
   - Save persists to both in-memory `Config` (live fields) and `~/.anthrouter/config.env` (all fields)

2. **Via CLI flags or env vars** (existing):
   - Start proxy: `python -m anthrouter --auto-model-routing --auto-model-routing-mode=rules ...`
   - Or set env vars: `export ANTHROUTER_AUTO_MODEL_ROUTING=true ANTHROUTER_AUTO_MODEL_ROUTING_MODE=rules`
   - Requires restart to take effect

3. **Via config.env file** (existing, also touched by UI):
   - Edit `~/.anthrouter/config.env` directly and restart
   - Or via UI, which reads/writes this file as the merge point for file-editable fields

---

## ADR References

**ADR-0005: Runtime configuration editor**
- Lines 8, 24–32 (decision): defines scope, split into live-editable vs file-editable, auth mechanism, concurrency
- Line 28: "Field-editable fields are written to the file ... but the running process keeps its old value and the UI shows a 'restart to apply' notice"
- Line 27: validation must reject bad enums; `validate_config()` is the prerequisite refactor
- Explicitly allows `POST /admin/config` as "the sole, deliberate exception" to read-only admin design (line 8)

**ADR-0007: Config modal field metadata**
- Lines 20–26 (decision): backend is single source of field metadata; enum values, type hints, min/max in `FIELD_METADATA`
- Line 24: server-side enforcement: every constraint advertised in metadata must be enforced by `validate_config()`
- Line 16–17 (context): identified gap — enum enforcement was missing at POST time; this ADR adds it

---

## Validation Enforcement

All constraints displayed in the modal are enforced server-side in `validate_config()` (`config.py:365–462`):

- Enums: `log_level`, `auto_model_routing_mode`, `sanitize_system_prompt` (lines 380–388)
- Numeric bounds: confidence [0, 1], thresholds, cache/preview sizes, prior limit [50, 32000] (lines 389–431)
- Cross-field: weights sum to 1.0, trivial < standard (lines 441–460)
- Non-empty fields: `upstream_base_url`, `auto_model_routing_classifier_model` (lines 432–439)

Invalid submissions are rejected at 400 with field-level error list; no partial writes.

---

## Summary Table

| Flag | Type | Editable | Restart | Control Type | Enum | Bounds |
|------|------|----------|---------|--------------|------|--------|
| `auto_model_routing` | bool | yes | no | checkbox | — | — |
| `auto_model_routing_mode` | str | yes | no | select | `classifier`, `rules`, `tag` | — |
| `auto_model_routing_classifier_model` | str | yes | **yes** | text | — | non-empty |
| `auto_model_routing_long` | str | yes | no | text | — | — |
| `auto_model_routing_long_context_threshold` | int | yes | no | number | — | ≥ 0 |
| `auto_model_routing_affirmation_inherit` | bool | yes | no | checkbox | — | — |
| `auto_model_routing_long_context_threshold` | int | yes | no | number | — | ≥ 0 |
| `auto_model_routing_confidence_bump` | bool | yes | no | checkbox | — | — |
| `auto_model_routing_min_confidence` | float | yes | no | number | — | [0.0, 1.0] |
| `auto_model_routing_trivial_threshold` | float | yes | no | number | — | < standard |
| `auto_model_routing_standard_threshold` | float | yes | no | number | — | > trivial |
| `auto_model_routing_system_prompt_weight` | float | yes | no | number | — | [0.0, 1.0], sum to 1.0 |
| `auto_model_routing_user_prompt_weight` | float | yes | no | number | — | [0.0, 1.0], sum to 1.0 |
| `auto_model_routing_system_prompt_cache_size` | int | yes | no | number | — | ≥ 1 |
| `auto_model_routing_system_prompt_preview_limit` | int | yes | no | number | — | ≥ 1 |
| `auto_model_routing_prior_response_summary_limit` | int | yes | no | number | — | [50, 32000] |
| `auto_model_routing_classification` | dict | **no** | n/a | — | — | — |
| `auto_model_routing_task_tiers` | dict | **no** | n/a | — | — | — |
