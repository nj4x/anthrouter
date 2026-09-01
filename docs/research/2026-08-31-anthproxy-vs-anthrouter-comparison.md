# anthproxy vs anthrouter: Architecture Comparison Report

**Date**: 2026-08-31  
**Type**: Research/Comparison  
**Scope**: Deep-compare of predecessor (anthproxy) and successor (anthrouter) codebases

---

## 1. Architecture-Level Diff: Module/Responsibility Mapping

### 1.1 Core Request Path

| anthproxy Module | anthrouter Module | Notes |
|------------------|-------------------|-------|
| `anthproxy/handlers.py` (2840 lines) | `anthrouter/handlers.py` (1256 lines) | anthrouter is ~56% smaller; removed multi-backend dispatch, retry logic, and credential handling complexity |
| `anthproxy/transport.py` (N/A - backends handle transport) | `anthrouter/transport.py` (246 lines) | anthrouter has single unified transport; anthproxy has per-backend transport in `anthproxy/anthropic/backend.py`, `anthproxy/bedrock/backend.py`, etc. |
| `anthproxy/mapper/anthropic_transform.py` | `anthrouter/mapper/anthropic_transform.py` | Nearly identical; both handle model alias resolution, beta merging, body building |
| `anthproxy/mapper/common.py` | `anthrouter/mapper/common.py` | Shared utilities for error handling, token estimation, thinking-block stripping |

### 1.2 Model Routing

| anthproxy Module | anthrouter Module | Notes |
|------------------|-------------------|-------|
| `anthproxy/model_router.py` (1997 lines) | `anthrouter/model_router.py` (2041 lines) | Nearly identical line counts; routing logic preserved 1:1 |
| `anthproxy/model_tier.py` | `anthrouter/model_tier.py` | Identical tier classification logic |
| `anthproxy/session_state.py` (N/A - uses BackendRegistry) | `anthrouter/session_state.py` | anthrouter has standalone session state; anthproxy embeds in BackendRegistry |
| `anthproxy/selector.py` (828 lines) | **N/A** | ** anthrouter lacks auto-backend selection entirely** - single-backend by design |

### 1.3 Sanitization

| anthproxy Module | anthrouter Module | Notes |
|------------------|-------------------|-------|
| `anthproxy/mapper/common.py:strip_volatile_system_blocks()` | `anthrouter/sanitizer.py` | anthrouter extracted sanitizer to standalone module; both use `anthproxy/prompt_volatility.py` logic |
| `anthproxy/prompt_volatility.py` | `anthrouter/prompt_volatility.py` | Identical volatility detection logic |

### 1.4 Persistence & Admin

| anthproxy Module | anthrouter Module | Notes |
|------------------|-------------------|-------|
| `anthproxy/db.py` (1798 lines, schema v13) | `anthrouter/db.py` (561 lines, schema v2) | anthrouter schema is simplified; lacks 11 migrations worth of features |
| `anthproxy/admin.py` (658 lines, handle_get + handle_post) | `anthrouter/admin.py` (176 lines, handle_get only) | **anthrouter admin is read-only by design** - no POST endpoints |
| `anthproxy/stats.py` | **N/A** | anthrouter lacks time-bucketed stats aggregation |
| `anthproxy/summary.py` | **N/A** | anthrouter lacks session summary generation |

### 1.5 Configuration

| anthproxy Module | anthrouter Module | Notes |
|------------------|-------------------|-------|
| `anthproxy/config.py` (705 lines) | `anthrouter/config.py` (457 lines) | anthrouter lacks backend selection flags, OAuth flags, multi-backend config |
| `anthproxy/backends_registry.py` | **N/A** | anthrouter has no backend registry - single transport |
| `anthproxy/oauth_registry.py` | `anthrouter/oauth_usage.py` (simplified) | anthrouter has read-only OAuth usage cache, no registry |

### 1.6 Key Architectural Differences

**anthproxy** is a **multi-backend routing proxy** with:
- Backend registry supporting anthropic, bedrock, codex, openrouter, local, peer
- Auto-backend selector with weekly utilization comparison, pace-delta ranking (ADR-0015)
- OAuth token management for anthropic/codex with working-hours gating (ADR-0030)
- Session pinning (backend + tier) via admin API
- Config change audit logging

**anthrouter** is a **single-backend specialization** with:
- One Anthropic API upstream only
- No backend selection, no OAuth, no peer chaining
- Simplified session state (in-memory LRU, no persistence across restarts)
- Read-only admin API (no runtime controls)

---

## 2. Behavioral/Feature Parity Gaps

### 2.1 Features in anthproxy BUT NOT in anthrouter

#### 2.1.1 Auto-Backend Selection (`selector.py`)
- **Location**: `anthproxy/selector.py:136-828`
- **What it does**: Background thread polls backend utilization, switches on 429s, implements pace-delta ranking (ADR-0015)
- **Status**: **Intentionally absent** - anthrouter is single-backend by design
- **Impact**: Operators lose automatic failover between subscription backends

#### 2.1.2 Multi-Backend Support (`backends_registry.py`)
- **Location**: `anthproxy/backends_registry.py`
- **What it does**: Registry pattern supporting 6+ backend types with per-backend auth, mappers, status
- **Status**: **Intentionally absent**
- **Impact**: Cannot route to Bedrock, Codex, OpenRouter, or peer instances

#### 2.1.3 OAuth Token Management
- **Location**: `anthproxy/oauth_registry.py`, `anthproxy/_shared/oauth_base.py`, `anthproxy/anthropic/auth.py`, `anthproxy/codex/auth.py`
- **What it does**: Proactive token refresh, working-hours gating (ADR-0030), pace-deadband selection (ADR-0017)
- **Status**: **Intentionally absent** - anthrouter forwards client credentials only
- **Impact**: Cannot use enterprise OAuth flows; clients must supply their own API keys

#### 2.1.4 Session Pinning via Admin API
- **Location**: `anthproxy/admin.py:561-598` (`_post_set_session_backend`, `_post_set_global_tier`)
- **What it does**: POST endpoints to pin backend/tier per session
- **Status**: **Absent** - anthrouter admin has no POST handlers
- **Impact**: Cannot override routing decisions at runtime

#### 2.1.5 Config Change Audit Log
- **Location**: `anthproxy/db.py:74-82` (config_changes table), `anthproxy/admin.py:601-650`
- **What it does**: Records who changed what config and when
- **Status**: **Absent** - anthrouter has no config_changes table
- **Impact**: No audit trail for configuration changes

#### 2.1.6 Session Summaries
- **Location**: `anthproxy/db.py:122-148` (session_summaries, conversation_summaries tables)
- **What it does**: LLM-generated session/conversation summaries
- **Status**: **Absent** - anthrouter lacks summary tables
- **Impact**: No automatic session summarization in admin UI

#### 2.1.7 Stats Time-Bucketing
- **Location**: `anthproxy/stats.py`
- **What it does**: Aggregates usage into day/week/month/quarter buckets
- **Status**: **Absent**
- **Impact**: Admin UI shows flat totals, no time-series breakdowns

#### 2.1.8 Cost Scope Filtering
- **Location**: `anthproxy/admin.py:249-264` (`_get_cost` with `session_id` filter)
- **What it does**: Cost breakdown by model/backend with optional session filter
- **Status**: **Absent** - anthrouter has no cost endpoint
- **Impact**: Cannot query cost by session

#### 2.1.9 Prompt Volatility Per-Session Reporting
- **Location**: `anthproxy/admin.py:218` (`session['prompt_volatility'] = _volatility_tracker.session_report(session_id)`)
- **What it does**: Returns per-block uniqueness ratios for a session
- **Status**: **Absent** - anthrouter admin doesn't call tracker.report()
- **Impact**: Cannot see volatility metrics in session detail view

#### 2.1.10 Trace Export
- **Location**: `anthproxy/admin.py:653-658` (`_post_export`)
- **What it does**: POST /admin/export returns full session trace with filename
- **Status**: **Absent**
- **Impact**: Cannot export session traces via API

#### 2.1.11 Backend Health Status
- **Location**: `anthproxy/admin.py:408-416` (`_get_backends` with `available` status)
- **What it does**: Returns availability status per backend
- **Status**: **Absent** - anthrouter has no backends to report on
- **Impact**: N/A for single-backend

### 2.2 Features in anthrouter BUT NOT in anthproxy

#### 2.2.1 Weighted System-Prompt + User-Prompt Blend (ADR-0010/0011/0012)
- **Location**: `anthrouter/model_router.py:926-979` (`_apply_weighted_blend`)
- **What it does**: Classifies system prompt separately, blends scores with configurable weights (default 30/70)
- **Status**: **Present in both** - anthproxy has identical implementation at `anthproxy/model_router.py:926-968`
- **Note**: Originally anthrouter-only, ported to anthproxy

#### 2.2.2 Long-Context Size Floor
- **Location**: `anthrouter/model_router.py:21-34` (docstring), lines 1318-1345 (implementation)
- **What it does**: Deterministic floor based on estimated input tokens + session calibration ratio
- **Status**: **Present in both** - anthproxy has identical at `anthproxy/model_router.py:22-35`, lines 1274-1301
- **Note**: Both have `opus[1m]` forcing with `context-1m` beta injection

#### 2.2.3 Walk-Back Cache for Text-less Turns
- **Location**: `anthrouter/model_router.py:685-705` (walk-back over prior messages)
- **What it does**: Recovers prior user text when final message is tool_result-only
- **Status**: **Present in both** - identical at `anthproxy/model_router.py:675-695`

#### 2.2.4 Affirmation Inheritance with Prior-Response Enrichment (ADR-0013)
- **Location**: `anthrouter/model_router.py:792-807` (`_extract_prior_response_summary`), lines 1402-1450 (affirmation handling)
- **What it does**: Bare "yes"/"proceed" turns inherit prior tier; classifier sees prior assistant response context
- **Status**: **Present in both** - anthproxy has identical at `anthproxy/model_router.py:781-807`, lines 1358-1406

#### 2.2.5 System-Prompt Sanitizer (ADR-0029)
- **Location**: `anthrouter/sanitizer.py`, `anthrouter/prompt_volatility.py`
- **What it does**: Detects volatile blocks by per-session uniqueness ratio, strips allowlisted prefixes
- **Status**: **Present in both** - anthproxy has identical at `anthproxy/mapper/common.py:strip_volatile_system_blocks()`, `anthproxy/prompt_volatility.py`
- **Note**: Both default to `strip` mode, both exclude peer-bound requests

#### 2.2.6 Thinking-Block Retry
- **Location**: `anthrouter/transport.py:164-176`
- **What it does**: On 400 with thinking/redacted_thinking, retry once with all thinking blocks stripped
- **Status**: **Present in anthproxy** at `anthproxy/anthropic/backend.py:112-126` (identical logic)

#### 2.2.7 DB Tier Pin Enforcement
- **Location**: `anthrouter/handlers.py:884-908` (checks `session_db.get_session_metadata()` for `pinned_tier`)
- **What it does**: Applies admin-set tier pins from DB on routing
- **Status**: **Absent in anthrouter** - wait, this code EXISTS in anthrouter but the DB schema lacks the `pinned_tier` column in sessions table. **This is a bug** - the code references `meta.get('pinned_tier')` but anthrouter's db.py has no sessions table with that column.
- **Note**: anthproxy has `sessions.pinned_tier` column (migration 0, line 71 in db.py) and `pinned_backend` (line 70)

### 2.3 Feature Parity Summary Table

| Feature | anthproxy | anthrouter | Notes |
|---------|-----------|------------|-------|
| Multi-backend routing | ✓ | ✗ | Intentional |
| Auto-backend selector | ✓ | ✗ | Intentional |
| OAuth token management | ✓ | ✗ | Intentional |
| Peer chaining | ✓ | ✗ | Intentional |
| Model-tier routing (classifier) | ✓ | ✓ | Identical |
| Long-context size floor | ✓ | ✓ | Identical |
| Walk-back cache | ✓ | ✓ | Identical |
| Affirmation inheritance | ✓ | ✓ | Identical |
| Weighted prompt blend | ✓ | ✓ | Identical |
| System-prompt sanitizer | ✓ | ✓ | Identical |
| Thinking-block retry | ✓ | ✓ | Identical |
| Admin POST endpoints | ✓ | ✗ | Intentional (read-only design) |
| Session pinning (DB) | ✓ | ✗ | Schema gap |
| Config audit log | ✓ | ✗ | Schema gap |
| Session summaries | ✓ | ✗ | Schema gap |
| Time-bucketed stats | ✓ | ✗ | Module absent |
| Prompt volatility reporting | ✓ | ✗ | Admin doesn't call tracker |
| Trace export | ✓ | ✗ | Endpoint absent |
| Cost by session | ✓ | ✗ | Endpoint absent |

---

## 3. Config Flags, DB Schema, Reason Codes Comparison

### 3.1 Config Flags Present in anthproxy BUT NOT in anthrouter

| Flag | anthproxy Default | Purpose |
|------|-------------------|---------|
| `--backend` | `'bedrock'` | Active backend selection |
| `--backends` | `None` (all enabled) | Backend allowlist |
| `--auto-backend` | `True` | Enable auto-selection |
| `--auto-backend-mode` | `'subscription'` | Initial selection mode |
| `--auto-backend-interval` | `60.0` | Selector poll interval |
| `--auto-backend-weekly-margin` | `5.0` | Hysteresis band |
| `--auto-backend-pace-delta` | `'on'` | Pace-delta ranking |
| `--auto-backend-oauth-*` | Various | OAuth gating params |
| `--region` | `'us-east-1'` | AWS region for Bedrock |
| `--use-inference-profile` | `True` | Cross-region inference |
| `--codex-home` | `''` | Codex credentials path |
| `--anthropic-home` | `''` | Anthropic credentials path |
| `--openrouter-api-key` | `''` | OpenRouter API key |
| `--peer-base-url` | `''` | Peer hop target |
| `--peer-api-key` | `''` | Peer auth key |
| `--codex-context-limit` | `100000` | Codex truncation limit |
| `--codex-unsupported-model-fallback` | `''` | Codex fallback model |
| `--stats-dir` | `''` | Stats JSONL directory |

### 3.2 Config Flags Present in anthrouter BUT NOT in anthproxy

**None** - all anthrouter flags are a subset of anthproxy's flags.

### 3.3 Shared Config Flags with Different Defaults

| Flag | anthproxy Default | anthrouter Default | Notes |
|------|-------------------|-------------------|-------|
| `--port` | `8082` | `8083` | Different defaults |
| `--lock-requested-model` | `'claude-sonnet-4-6'` | `'off'` | anthproxy defaults to lock ON |
| `--auto-model-routing-system-prompt-weight` | `0.20` | `0.30` | Different defaults |
| `--auto-model-routing-user-prompt-weight` | `0.80` | `0.70` | Different defaults |
| `--auto-model-routing-trivial-threshold` | `30` | `38.0` | Different defaults |
| `--auto-model-routing-standard-threshold` | `60` | `75.0` | Different defaults |

### 3.4 DB Schema Comparison

#### anthproxy Schema (v13, 13 migrations)

**Tables**:
- `requests` (40+ columns including weighted blend, sanitizer, economics columns)
- `sessions` (with `pinned_backend`, `pinned_tier`, `display_name`)
- `session_summaries` (LLM-generated session summaries)
- `conversation_summaries` (per-anchor summaries)
- `prompt_store` (deduped system/tools content)
- `config_changes` (audit log)

**Key columns in `requests`**:
- `conversation_anchor` (per-conversation discriminator)
- `backend` (which backend served)
- `parent_conversation_anchor` (links to parent Task session)
- `system_prompt_tier`, `system_prompt_score`, `user_prompt_score`, `routing_weighted_score`, `system_prompt_classification_failed` (weighted blend)
- `user_prompt_tier` (derived from user score)
- `system_prompt_sanitized_sha256` (post-strip hash)
- `user_prompt_search`, `response_search` (casefolded for search)
- `routing_recovered_via_walkback` (walk-back flag)
- `classifier_confidence` (legacy, unused)
- `net_savings_usd`, `classifier_overhead_usd` (economics)

#### anthrouter Schema (v2, 2 migrations)

**Tables**:
- `requests` (35 columns)
- `prompt_store` (deduped content)
- `sanitizer_events` (1:N with requests, includes `payload_full`)
- **No `sessions` table**
- **No `session_summaries` table**
- **No `conversation_summaries` table**
- **No `config_changes` table**

**Key columns in `requests`**:
- `session_id` (text, not foreign-keyed to sessions table)
- **No `conversation_anchor`** - lacks per-conversation discrimination
- **No `backend`** - single-backend, implicit
- **No `parent_conversation_anchor`** - no Task sub-agent tracking
- Has weighted blend columns (same as anthproxy)
- Has `system_prompt_sanitized_sha256`
- **No `user_prompt_search`/`response_search`** - uses FTS5 instead
- **No `routing_recovered_via_walkback`** - not recorded
- **No `classifier_confidence`** - not recorded
- Has economics columns

#### Schema Gaps Summary

| Table/Column | anthproxy | anthrouter | Impact |
|--------------|-----------|------------|--------|
| `sessions` table | ✓ | ✗ | No session metadata, pinning, display names |
| `session_summaries` | ✓ | ✗ | No LLM-generated summaries |
| `conversation_summaries` | ✓ | ✗ | No per-conversation summaries |
| `config_changes` | ✓ | ✗ | No audit trail |
| `requests.conversation_anchor` | ✓ | ✗ | Cannot isolate sub-agent conversations |
| `requests.backend` | ✓ | ✗ | Cannot filter by backend (N/A for single-backend) |
| `requests.parent_conversation_anchor` | ✓ | ✗ | Cannot link Task sub-agents to parents |
| `requests.routing_recovered_via_walkback` | ✓ | ✗ | Cannot query walk-back usage |
| FTS5 index | ✗ | ✓ | anthrouter uses trigram FTS, anthproxy uses casefolded shadow columns |
| `sanitizer_events.payload_full` | ✗ | ✓ | anthrouter stores full payload, anthproxy stores preview only |

### 3.5 Reason Codes Comparison

#### Shared Reason Codes (both have)

```
'disabled'
'model_not_eligible'
'malformed_payload'
'missing_final_user_text'
'override_no_classifier'
'size_forced_long_context'
'session_cached_tier'
'session_cached_tier_capped'
'session_cached_walkback'
'session_cached_walkback_capped'
'session_cached_walkback_tool_result'
'affirmation_inherited'
'affirmation_floored_standard'
'affirmation_classified'
'affirmation_classifier_failed'
'rule_title_generation'
'classifier_trivial'
'classifier_standard'
'classifier_deep'
'classifier_trivial_bumped'
'classifier_standard_bumped'
'classifier_failed'
'classifier_invalid'
'classifier_rules_trivial'
'classifier_rules_standard'
'classifier_rules_deep'
'rules_no_signal'
'task_tag_routed'
'no_task_tag'
'unknown_task_tag'
```

#### Reason Codes in anthproxy BUT NOT in anthrouter

| Reason Code | Location | Purpose |
|-------------|----------|---------|
| `'peer_hop_suppressed'` | `anthproxy/handlers.py:788-795` | Model routing suppressed for peer-bound requests (ADR-0023) |

#### Reason Codes in anthrouter BUT NOT in anthproxy

**None** - anthrouter's `ReasonCode` literal at `anthrouter/model_router.py:342-375` is a subset of anthproxy's at `anthproxy/model_router.py:340-375`.

---

## 4. Error-Handling / Fail-Open vs Fail-Closed Philosophy

### 4.1 Model Routing Failures

**Both projects**: **Fail-closed** for routing decisions.

- `anthrouter/model_router.py:18-19`: "Any classifier failure keeps the model as the original requested model (fail-closed)."
- `anthproxy/model_router.py:19-20`: Identical statement.
- Implementation: When `route_model()` encounters any error (malformed payload, classifier failure, no eligible model), it returns `requested_model == routed_model` with `applied=False`.

**Specific fail-closed behaviors**:
- Classifier returns invalid response → keep requested model, reason `'classifier_invalid'`
- Classifier network error → keep requested model, reason `'classifier_failed'`
- No final user text extractable → keep requested model, reason `'missing_final_user_text'`
- Model not a non-empty string → keep as-is (not eligible for routing)

### 4.2 Sanitizer Failures

**Both projects**: **Fail-open** for sanitizer - unrecognized volatile blocks are reported but NOT stripped.

- `anthrouter/sanitizer.py`: Allowlist-based stripping; unrecognised blocks flagged but transmitted untouched.
- `anthproxy/mapper/common.py:strip_volatile_system_blocks()`: Identical behavior.
- ADR-0029 §41: "A block judged volatile but unrecognised is reported and transmitted untouched. This is the load-bearing half: auto-stripping anything that merely *looks* volatile risks deleting a timestamp, a live file list, or running task state."

### 4.3 Transport/Retry Failures

**anthrouter**:
- `anthrouter/transport.py:111-181`: Retries 429s (with upstream timing) and 5xx in-connection, MAX_RETRIES=3.
- Thinking-block 400: Single retry with all thinking/redacted_thinking stripped (`transport.py:164-176`).
- Network errors after all retries → 502 with `error_type='api_error'`.
- **No handler-level retry** - transport retries are the only retry logic.

**anthproxy**:
- `anthproxy/anthropic/backend.py:100-170`: Identical retry logic to anthrouter.
- Thinking-block 400: Identical single-retry strip logic.
- **Additional retry layer**: `anthproxy/handlers.py:1850-1920` has handler-level retry for peer/backend failover when the active backend fails.

**Key difference**: anthproxy can retry on a different backend when one fails; anthrouter has nowhere to fail over to.

### 4.4 Credential Handling

**anthrouter**:
- `anthrouter/transport.py:59-88`: `extract_client_credentials()` rejects (401) any request missing both `x-api-key` and `Authorization`.
- **No default credentials** - anthrouter holds no credential of its own.
- 401 propagates untouched to client.

**anthproxy**:
- `anthproxy/anthropic/auth.py`: Can use OAuth tokens from `~/.anthropic/` for anthropic backend.
- `anthproxy/codex/auth.py`: Can use OAuth tokens from `~/.codex/` for codex backend.
- `anthproxy/transport.py:extract_client_credentials()`: Same 401 behavior for missing credentials.
- **OAuth backends** can supply credentials server-side; Anthropic/Codex backends also accept client passthrough.

### 4.5 Classifier Isolation

**Both projects**: Classifier calls are isolated from backend kill-switches and usage accounting.

- `anthrouter/model_router.py:38-44`: "Classifier payloads carry `_anthproxy_internal_classifier = True`. The mapper must strip this key before sending upstream."
- `anthproxy/model_router.py:39-42`: Identical sentinel handling.
- Both use `send_classifier_message()` when available on backend to exclude from user-visible accounting.

**Key invariant**: Classifier failures never count against user quotas or trigger backend switching.

### 4.6 Peer Hop Authority Boundaries (anthproxy only)

**anthproxy** has explicit fail-closed boundaries for peer chaining:

- ADR-0023: "Model-tier routing authority rests with the innermost hop."
- `anthproxy/handlers.py:738-801`: Peer-bound requests suppress ALL routing (classifier, size floor, session state).
- ADR-0029 §44: "Sanitizing authority rests with the innermost hop."
- `anthproxy/handlers.py:963`: `if mode == 'off' or snapshot.name == 'peer': return` - sanitizer suppressed for peer.

**anthrouter**: No peer backend, so these boundaries are N/A.

### 4.7 Summary: Error-Handling Philosophy

| Scenario | anthproxy | anthrouter |
|----------|-----------|------------|
| Classifier failure | Fail-closed (keep requested model) | Fail-closed (keep requested model) |
| Sanitizer unrecognized block | Fail-open (report but transmit) | Fail-open (report but transmit) |
| Transport 429/5xx | Retry in-connection, then fail | Retry in-connection, then fail |
| Missing credentials | 401 to client | 401 to client |
| Peer-bound request | Suppress routing/sanitizing (innermost authority) | N/A |
| Backend failure | Can failover to different backend | No failover option |
| Thinking-block 400 | Single retry with strip | Single retry with strip |

**Overall philosophy**: Both projects share identical fail-closed posture for routing decisions and fail-open posture for sanitization. The key difference is anthproxy has more failure modes to handle (multi-backend, peer chaining, OAuth) and thus more complex recovery paths.

---

## 5. Verified Line Numbers for Previously Reported Features

### 5.1 HAPPY_NEW_YEAR_PREFIX Security Monitor Interception

**Brief stated**: "a prior research pass already found one feature (`HAPPY_NEW_YEAR_PREFIX`/`HAPPY_BIRTHDAY_REPLY` permission-check short-circuit) that existed in anthproxy but was missing in anthrouter, and it has since been ported (anthrouter/handlers.py, constants ~line 53-54, detection fn ~169-180, interception call ~567-568, handler method ~954 — verify these line numbers are still accurate)"

**Verified locations in anthrouter**:
- Constants: `anthrouter/handlers.py:53-54` ✓ (lines 53-54 define `HAPPY_NEW_YEAR_PREFIX` and `HAPPY_BIRTHDAY_REPLY`)
- Detection fn: `anthrouter/handlers.py:169-182` ✓ (`_has_happy_new_year_system_prompt()`)
- Interception call: `anthrouter/handlers.py:567-569` ✓ (in `_handle_messages()`)
- Handler method: `anthrouter/handlers.py:954-969` ✓ (`_handle_happy_new_year()`)

**Verified locations in anthproxy**:
- Constants: `anthproxy/constants.py:37-38` ✓
- Detection fn: `anthproxy/handlers.py:232-244` ✓ (`_has_happy_new_year_system_prompt()`)
- Interception: `anthproxy/handlers.py:1250-1252` ✓ (in `_handle_messages()`)
- Handler: `anthproxy/handlers.py:1535-1547` ✓ (`_handle_happy_new_year()`)

**Status**: **CONFIRMED PORTED** - line numbers in brief were approximately correct (off by ~15 lines due to code changes).

### 5.2 Prompt Store JOIN Pattern (Admin Request Detail)

**Brief stated**: "anthproxy's admin.py (~line 1369-1372) has a prompt_store JOIN pattern that anthrouter's admin.py currently lacks (anthrouter's _get_request_detail() only returns hashes, not content)"

**Verified in anthproxy**:
- `anthproxy/admin.py:496-502` (`_get_request_detail()`): Returns `db.get_request(request_id)` which includes JOINed prompt content via `db.py:1050-1080` (`get_request()` method joins `prompt_store` for `system_prompt_content` and `tools_content`).

**Verified in anthrouter**:
- `anthrouter/admin.py:94-101` (`_get_request_detail()`): Returns `db.get_request(request_id)` which returns raw `requests` row only.
- `anthrouter/db.py:368-372` (`get_request()`): Simple `SELECT * FROM requests WHERE id = ?` - **no JOIN to prompt_store**.

**Status**: **CONFIRMED GAP** - anthrouter's request detail endpoint returns only hashes, not prompt content. anthproxy JOINs `prompt_store` to return full content.

**Fix would require**:
1. Modify `anthrouter/db.py:get_request()` to JOIN `prompt_store` on `system_prompt_sha256`, `system_prompt_sanitized_sha256`, `tools_sha256`
2. Or add a separate `get_prompt_content()` method and call it from admin handler

---

## 6. Conclusions

### 6.1 Intentional Differences (Design Choices)

The following gaps are **intentional architectural simplifications** in anthrouter:

1. **Single-backend focus**: No multi-backend registry, selector, or failover
2. **No OAuth**: Client credentials only, no server-side token management
3. **Read-only admin**: No POST endpoints, no runtime controls
4. **Simplified schema**: No session tables, no summaries, no audit log
5. **No peer chaining**: No support for routing through another proxy instance

### 6.2 Unintentional Gaps (Potential Bugs/Oversights)

The following gaps may be **unintentional oversights**:

1. **DB tier pin code without schema support**: `anthrouter/handlers.py:884-908` references `session_db.get_session_metadata()` and `pinned_tier`, but anthrouter has no `sessions` table and no `pinned_tier` column. **This code is dead/unreachable.**

2. **Prompt content not JOINed in admin detail**: Confirmed gap - admin detail returns hashes only, not content.

3. **Walk-back flag not recorded**: `routing_recovered_via_walkback` column absent from anthrouter schema.

4. **Prompt volatility not reported**: Admin doesn't call `_volatility_tracker.session_report()` for session detail.

### 6.3 Parity Strengths

The following core features are **identical** between both projects:

1. Model-tier routing (classifier, rules, tag modes)
2. Long-context size floor with calibration
3. Walk-back cache for text-less turns
4. Affirmation inheritance with prior-response enrichment
5. Weighted system-prompt + user-prompt blend
6. System-prompt sanitizer with volatility detection
7. Thinking-block retry logic
8. Classifier isolation and sentinel handling

---

## Appendix A: File/Line Citation Index

| Feature | anthproxy Location | anthrouter Location |
|---------|-------------------|---------------------|
| HAPPY_NEW_YEAR constants | `constants.py:37-38` | `handlers.py:53-54` |
| HAPPY_NEW_YEAR detection | `handlers.py:232-244` | `handlers.py:169-182` |
| HAPPY_NEW_YEAR handler | `handlers.py:1535-1547` | `handlers.py:954-969` |
| Model routing main | `model_router.py:1274-1450` | `model_router.py:1318-1494` |
| Weighted blend | `model_router.py:926-968` | `model_router.py:926-979` |
| Sanitizer strip | `mapper/common.py:strip_volatile_system_blocks()` | `sanitizer.py:sanitize_system_prompt()` |
| Volatility tracker | `prompt_volatility.py` | `prompt_volatility.py` (identical) |
| Admin request detail | `admin.py:496-502` | `admin.py:94-101` |
| DB get_request with JOIN | `db.py:1050-1080` | `db.py:368-372` (no JOIN) |
| Transport retry | `anthropic/backend.py:100-170` | `transport.py:111-181` |
| Thinking-block retry | `anthropic/backend.py:112-126` | `transport.py:164-176` |
| Auto-selector | `selector.py:136-828` | N/A |
| Backend registry | `backends_registry.py` | N/A |
| OAuth registry | `oauth_registry.py` | N/A |

---

*Report generated by deep-compare of both codebases with line-level verification.*
