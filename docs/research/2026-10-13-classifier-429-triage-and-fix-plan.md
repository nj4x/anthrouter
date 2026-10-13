# Classifier 429 storm: triage and fix plan

**Date**: 2026-10-13
**Status**: Triage complete; fixes proposed, nothing implemented
**Symptom**: 69 of 70 requests recorded on 2026-10-13 carry `reason_code='classifier_failed'` with `classifier_raw_response='HTTP 429 AnthropicRequestError: Error'`; 3 of them surfaced HTTP 429 to the client; every affected request paid ~17.5 s of pure classifier-retry latency. In parallel, the OAuth usage poller hammered `/api/oauth/usage` about 7 times per second.
**Scope**: Read-only investigation. No source, test or database change was made. The database was opened with `sqlite3 -readonly`; no prompt text, credential or token value was read out.

Related: [2026-09-29 OAuth usage endpoint 429 retry loop](2026-09-29-oauth-usage-429-retry-loop.md) — issue A below is that same unfixed defect, re-observed; this note adds the new evidence and does not restate its root-cause analysis.

---

## 1. Issue register

Severity scale: **high** = user-visible failure or upstream abuse; **medium** = wrong data or lost diagnostics; **low** = noise or cosmetics.

| ID | Issue | Severity | Confidence |
| -- | ----- | -------- | ---------- |
| A | OAuth usage poller busy-loops on 429 with `Retry-After: 0` | high | verified |
| B | Classifier retries a non-retryable 429 four times, adding ~15 s per request | high | verified |
| C | A 429 that reaches the client is delayed ~18 s by issue B, and the proxy invents `Retry-After: 1` | medium | verified |
| D | `anthropic-ratelimit-*` headers and `request-id` are discarded on every non-200 response | medium | verified |
| E | Under `--lock-requested-model`, a fail-closed classifier decision dispatches the lock baseline but records the client's model | high | verified |
| F | Classifier retry budget is hard-coded; no jitter, no circuit breaker; transport retries nest inside it | medium | verified |
| G | `requests.attempt` is always 1 — retries are invisible in the database | low | verified |
| H | Log is 87% OAuth-poller noise, which buries the real failures | medium | verified |

### A. OAuth usage poller busy-loops on `Retry-After: 0`

**Root cause** (unchanged since the 2026-09-29 note): [`anthrouter/oauth_usage.py:169-178`](../../anthrouter/oauth_usage.py) *replaces* the exponential backoff multiplier with `retry_after / self._base_interval`. With `Retry-After: 0` the multiplier becomes `0.0`, so the loop's sleep at [`oauth_usage.py:117`](../../anthrouter/oauth_usage.py) computes `min(60 * 0.0, 300) = 0.0` and `self._stop_event.wait(0.0)` returns at once. The error path at [`oauth_usage.py:138-144`](../../anthrouter/oauth_usage.py) doubles the multiplier, but `0 * 2 = 0`, so the doubling never escapes zero.

**Verified new evidence (2026-10-13)**

- `grep -c "OAuth usage 429" ~/.anthrouter/anthrouter.log` → 269,097; `grep -c "OAuth usage fetch failed" …` → 283,577.
- Per-minute rate on 2026-10-13: 416 (11:56), 406 (11:57), 440 (11:58), 438 (11:59), 428 (12:01), 430 (12:03), 440 (12:05) — about 7 requests per second, matching the ~130 ms cadence described in the earlier note.
- The log line only exists inside `if status == 429 and retry_after_str:` ([`oauth_usage.py:170`](../../anthrouter/oauth_usage.py)), so the header **was present** and parsed to a value that formats as `0.0` with `%.1f`. This is not an absent header and not a parse failure. The usage endpoint really answers `Retry-After: 0` (or a value below 0.05 s).

**Impact**: sustained ~7 requests/second against an Anthropic endpoint with the user's OAuth bearer token, for the whole life of the process; the log flood of issue H; and a plausible (unproven) contribution to account-level throttling.

**Triggers** (all three still present): per-request on bearer auth ([`handlers.py:669-674`](../../anthrouter/handlers.py)), per admin poll that passes `?token=` ([`admin.py:432-435`](../../anthrouter/admin.py)), and the single daemon loop ([`oauth_usage.py:108-144`](../../anthrouter/oauth_usage.py)). The 2026-10-13 run shows 377 `GET /admin/oauth-usage` calls, consistent with the admin UI being open; those calls without a `token` query parameter only read the cache, so they do not themselves schedule fetches.

### B. Classifier retries a non-retryable 429 four times

**Root cause**: [`anthrouter/model_router.py:1491-1555`](../../anthrouter/model_router.py) wraps the classifier call in a fixed ladder `backoff_delays = [1.0, 2.0, 4.0, 8.0]` and retries on **any** exception, with no look at the status code and no jitter. Five attempts therefore always run before `classifier_failed` is returned, costing `1+2+4+8 = 15 s` of sleep plus five upstream round-trips.

The same 429 is treated as non-retryable one layer down: [`http_util.py:168-179`](../../anthrouter/http_util.py) `should_retry()` returns `False` for a 429 that carries no retry timing, precisely because "a short backoff won't clear the limit". The classifier layer contradicts that policy.

**Verified evidence**

- Log, one request, 2026-10-13 11:23:22.379 → 11:23:40.141: `classifying` then five `Upstream error HTTP 429` / `classifier call failed (HTTP 429, rate_limit_error)` pairs at +0.8 s, +2.3 s, +4.8 s, +9.2 s, +17.8 s, with `retrying classifier in 1.0s … 2.0s … 4.0s … 8.0s (attempt 4/4)` in between, ending in `Model routing: … reason=classifier_failed` at `+17.77s`.
- Attempt accounting: the log holds 351 `Upstream error HTTP 429` lines, 348 `classifier call failed` lines and 3 main-dispatch 429s — 348 + 3 = 351. **Zero** transport-level retries happened, which proves `parse_retry_after()` found neither `retry-after` nor `retry-after-ms` on these 429s.
- Durations: `success`/`classifier_failed` rows average 6.5 s (min 1.6 s, max 42.0 s); the three `rate_limited` rows are 18.0 s, 18.4 s and 19.0 s — the 15 s ladder plus five round-trips.

**Impact**: every request during the storm paid up to 17.5 s before its real upstream call even started, and spent five requests of the account's own per-model request budget on a classifier that could not succeed. Amplification factor 5 against an endpoint that is already saying "too many requests".

**Fail-closed invariant**: holds in the classifier path. `_dispatch_classifier_mode` returns `None` as the first tuple element ([`model_router.py:1555`](../../anthrouter/model_router.py)), and `route_model()` then returns `routed_model=requested, applied=False` ([`model_router.py:2008-2026`](../../anthrouter/model_router.py)). No upgrade to a pricier tier happens *through the decision object*. See issue E for the separate payload-level violation under `--lock-requested-model`.

### C. 429 reaching the client, and an invented `Retry-After`

**Surface-versus-retry decision path**: `AnthropicTransport._send_with_retries` → `should_retry(429, resp)` → `False` (no retry timing) → `handle_error_response()` raises `AnthropicRequestError(status_code=429, error_type='rate_limit_error')` ([`transport.py:150-205`](../../anthrouter/transport.py), [`http_util.py:177-209`](../../anthrouter/http_util.py)). `_handle_messages` catches it and records `status='rate_limited'` when `exc.status_code == 429` ([`handlers.py:677-684`](../../anthrouter/handlers.py)), then `_send_error` returns the envelope to the client.

This is the documented-correct behaviour: a 429 without `retry-after` is the shape Anthropic uses for a spend-cap stop, which "keeps failing until access resumes" (see section 3). **Not a bug.**

Two real problems sit around it:

1. The client waited ~18 s for an answer that was knowable in ~0.5 s, because the classifier ladder ran first (issue B). SQL confirms the three rows: ids 7945 (18:36:08Z), 7951 (18:39:54Z), 7959 (18:46:50Z), each `error='429 — rate_limit_error'`, each `duration_ms` 18.0–19.0 s.
2. [`handlers.py:1266-1269`](../../anthrouter/handlers.py) hard-codes `self.send_header('Retry-After', '1')` on **every** 429 the proxy emits, including one the upstream sent with no timing at all. The proxy is inventing guidance that contradicts both the upstream and the docs, and 1 s invites the client to hammer. Note that `AnthropicRequestError` already carries a `retry_after` field ([`mapper/common.py:30-37`](../../anthrouter/mapper/common.py)) that **nothing ever sets and nothing ever reads** — `handle_error_response()` does not pass it. The plumbing for a truthful header exists and is dead.

**Why only 3 of 69**: the three are the requests whose *main* dispatch also drew a 429. The other 66 classifier failures were followed by a successful main dispatch. All three failures were requests the client sent as `claude-haiku-4-5-20251001`; the 58 sonnet-requested rows all succeeded. That is consistent with the documented per-model rate limits (section 3): the Haiku bucket was exhausted — by, among other things, the classifier itself, which runs on `haiku` → `claude-haiku-5-5` ([`model_config.py:11-16`](../../anthrouter/model_config.py)) — while the Sonnet bucket still had headroom. *Hypothesis*, see section 2.

### D. Rate-limit headers and request ID discarded on error responses

**Root cause**: `parse_ratelimit_headers(resp)` is called **only** on the 200 branch, [`transport.py:145-148`](../../anthrouter/transport.py). Every non-200 response — including the 429 that is exactly when the window matters most — goes to `handle_error_response()` without its headers being read. `request-id` is never read on any path, so a 429 cannot be reported to Anthropic support.

**Verified**: all three `rate_limited` rows have NULL in all five `ratelimit_*` columns. Separately, across the **entire** database (4,952 rows, 2026-09-14 → 2026-10-13) **zero** rows carry any `ratelimit_*` value, although the write path maps them correctly ([`db.py:306`, `db.py:368-372`](../../anthrouter/db.py)). The in-memory window used by `proxy-status` ([`handlers.py:1157`](../../anthrouter/handlers.py), [`session_state.py:100-108`](../../anthrouter/session_state.py)) is therefore always empty too.

The 429 body is also non-standard: `handle_error_response` logged `Upstream error HTTP 429: Error`, and the stored preview is `HTTP 429 AnthropicRequestError: Error`. A documented error body would have produced a sentence (section 3). A bare `Error` means `json.loads` failed and the fallback at [`http_util.py:193-195`](../../anthrouter/http_util.py) used the raw body text — so the upstream returned a plain-text `Error` body, which is **not** a documented Anthropic error shape and carries none of `error.type`, `error.details.error_code` or `request_id`.

### E. Fail-closed under `--lock-requested-model` dispatches the baseline but records the client model

**Root cause**: [`model_router.py:1636-1640`](../../anthrouter/model_router.py) overwrites `payload['model']` with the lock baseline up front. The `missing_final_user_text` fail-closed path then deliberately re-sets `payload['model']` and *reports the same value* (`fallback = routing_baseline if baseline_model else requested`, [`model_router.py:1715-1726`](../../anthrouter/model_router.py)). The classifier fail-closed return at [`model_router.py:2008-2026`](../../anthrouter/model_router.py) does neither: it leaves `payload['model']` at the baseline and reports `routed_model=requested, applied=False`. The `disabled` and `model_not_eligible` early returns at [`model_router.py:1641-1659`](../../anthrouter/model_router.py) have the same shape — both run *after* line 1640.

**Verified evidence** (same request, 2026-10-13 11:35:50 → 11:36:08, DB id 7945):

```
Model lock: forcing routing baseline from claude-haiku-4-5-20251001 to sonnet
Model routing: requested=claude-haiku-4-5-20251001 routed=claude-haiku-4-5-20251001 applied=False reason=classifier_fail…
Routing request: operation=messages stream=False model=sonnet
```

The decision says "kept Haiku"; the wire got `sonnet`, which `resolve_model()` maps to `claude-sonnet-5-5`. The DB row stores `requested_model` and `routed_model` both as `claude-haiku-4-5-20251001`, so `cost_estimate`, `model_tier` and the savings columns are all computed against a model that never served the request. `net_savings_usd` is additionally forced to NULL because `applied` is false ([`db.py:303-305`](../../anthrouter/db.py)).

**Impact**: cost attribution is wrong in the direction that hides a cost *increase* (Haiku priced, Sonnet served). Read literally, `CLAUDE.md`'s "never fail open to a more expensive or more permissive tier" is violated at the payload level. The counter-reading — that `--lock-requested-model` is an operator instruction to serve everything at the baseline, so dispatching `sonnet` is intended — is defensible from the flag's help text ([`config.py:688-694`](../../anthrouter/config.py)). Either way the **record** is wrong, and the two fail-closed paths disagree with each other.

### F. Classifier retry budget is hard-coded, and transport retries nest inside it

- No config flag governs the ladder; `backoff_delays` is a literal ([`model_router.py:1491`](../../anthrouter/model_router.py)).
- No jitter, so concurrent requests retry in lockstep.
- No breaker: each inbound request repeats the full ladder, so a storm scales linearly with traffic (348 classifier 429s over ~45 minutes).
- Nesting: each classifier attempt calls `send_message` → `_send_with_retries`, which may itself retry up to `MAX_RETRIES = 3` ([`http_util.py:21`](../../anthrouter/http_util.py), [`transport.py:120-157`](../../anthrouter/transport.py)). When the upstream *does* send `retry-after`, worst case is 5 × 4 = 20 upstream calls for one inbound request.
- The system-prompt classifier takes the opposite stance and fails **open** to the midpoint score ([`model_router.py:959-965`](../../anthrouter/model_router.py)) with no retry at all; the affirmation classifier at [`model_router.py:1886-1900`](../../anthrouter/model_router.py) also has no retry. Only the main classifier retries. During this incident neither was reached, because the main classifier failed first.

### G. `requests.attempt` is always 1

The column exists with `NOT NULL DEFAULT 1` ([`db.py:71`](../../anthrouter/db.py)) and `record_request(attempt: int = 1)` ([`db.py:275`](../../anthrouter/db.py)), but no caller ever passes it — `handlers._record_db` does not ([`handlers.py:1001-1022`](../../anthrouter/handlers.py)). `SELECT max(attempt)` over the incident window returns 1. Transport retries and classifier retries are both invisible to the admin UI.

### H. Log noise

269,097 + 283,577 = 552,674 of 635,120 lines (87%) in `~/.anthrouter/anthrouter.log` are the two OAuth-poller lines. The 429 line is logged at INFO on every single attempt ([`oauth_usage.py:176`](../../anthrouter/oauth_usage.py)) and the failure at WARNING on every single attempt ([`oauth_usage.py:139`](../../anthrouter/oauth_usage.py)). Finding the 348 classifier failures required filtering them out.

---

## 2. Do A and B share a cause?

**Verified facts**

- The classifier call uses **the client's own credentials**. `extract_client_credentials(self.headers)` → `_prepare` → `route_model(payload, target, credentials, …)` ([`handlers.py:666`, `715-727`](../../anthrouter/handlers.py)) → `send_fn(classifier_payload, credentials, config)` ([`model_router.py:1497`](../../anthrouter/model_router.py)). So classifier traffic draws on the same account, and on the Haiku 5.5 per-model bucket.
- The OAuth poller uses the same account's OAuth bearer token, against a different endpoint and different HTTP client ([`oauth_usage.py:146-159`](../../anthrouter/oauth_usage.py)).
- Ordering rules out one direction: classifier 429s start at 11:23 (first `classifier call failed` line) and the first OAuth-poller 429 of that process is at 11:56. **The poller did not start the classifier storm.**
- The two 429s differ in shape: the usage endpoint sends `Retry-After: 0`; the Messages endpoint sent **no** retry timing at all (proved by zero transport retries).

**Hypothesis (medium confidence)**: one account-level condition produced both — the account hit a usage ceiling, and both endpoints answered 429 for the same underlying reason. Under that reading neither A nor B *causes* the other; each is an amplifier that makes a bad account state worse and much noisier. The plain-text `Error` body (issue D) and the missing `retry-after` both point away from an ordinary per-minute bucket 429 and toward a ceiling-reached 429 (section 3).

**Hypothesis (low confidence)**: 7 requests/second from the poller tripped an acceleration or abuse limit that then applied to Messages traffic as well. The documentation does mention acceleration limits for sharp usage increases (section 3), but gives no cross-endpoint rule, and the timeline shows the Messages 429s first. Do not act on this one.

**Hypothesis (medium confidence)**: the classifier's own five-attempt ladder on `claude-haiku-5-5` is what exhausted the Haiku bucket that then failed the three client Haiku requests. Rate limits are documented as per-model, the three failures are exactly the Haiku-requested ones, and all 58 Sonnet-requested ones succeeded. Confirmable only with the response headers that issue D throws away.

---

## 3. Against the official documentation

From [Rate limits](https://platform.claude.com/docs/en/api/rate-limits) and [Claude API errors](https://platform.claude.com/docs/en/api/errors), both read 2026-10-13:

| Claim | Documented |
| ----- | ---------- |
| Exceeding RPM/ITPM/OTPM returns 429 "along with a `retry-after` header indicating how long to wait" | yes |
| Rate limits "are applied separately for each model" | yes |
| Spend-cap 429 "has no `retry-after` header and keeps failing until access resumes"; `error.details.error_code` is `enforced_spend_limit_reached` | yes |
| Claude Code workspace limits are checked separately and "can instead receive a 429 that carries a `retry-after` header" | yes |
| `retry-after`: "The number of seconds to wait until you can retry the request. Earlier retries will fail." | yes |
| Full header set: `retry-after`, `anthropic-ratelimit-{requests,tokens,input-tokens,output-tokens}-{limit,remaining,reset}`, plus `anthropic-priority-*` on Priority Tier | yes |
| Every response carries a `request-id` header, to be quoted in support tickets | yes |
| Official SDKs retry transient failures, including rate limits, "honoring the `retry-after` header when present" | yes |
| 429 error body shape: `{"type":"error","error":{"type":"rate_limit_error","message":…},"request_id":…}` | yes |
| `/api/oauth/usage` request or response contract, its rate limit, or its `Retry-After` semantics | **undocumented** |
| Any rule that one endpoint's 429 implies another endpoint's 429 | **undocumented** |
| `Retry-After: 0` semantics | **not in Anthropic docs**; RFC 9110 §10.2.3 defines the value as a minimum delay, and 0 is a valid value |

Three gaps worth stating plainly:

1. **`/api/oauth/usage` is undocumented.** It appears only in third-party reports ([Claude-Code-Usage-Monitor #202](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor/issues/202), [claude-hud #287](https://github.com/jarrodwatts/claude-hud/issues/287)), which also warn that using a subscription OAuth token outside Claude Code and Claude.ai may breach the Consumer Terms. Nothing about its 429 behaviour can be treated as contract; `Retry-After: 0` from it must be handled defensively.
2. **The observed 429 does not match the documented body.** `Error` as the whole body means no `error.type`, no `details.error_code` and no `request_id` were available, so this proxy cannot currently tell a spend-cap stop from a per-minute bucket stop. Issue D is what keeps that question open.
3. **The docs support the current transport policy** (`should_retry` refusing a 429 without timing) and contradict the classifier ladder, which retries a 429 the upstream explicitly declined to schedule.

---

## 4. Fix plan

Ordering is by (upstream harm × ease). Each step is independently shippable.

### Priority 1 — A: stop the poller busy-loop

`anthrouter/oauth_usage.py` only.

1. Treat `Retry-After` as a floor, never an override: `cooldown = min(max(retry_after_seconds, base_interval * multiplier, MIN_COOLDOWN_SECONDS), MAX_BACKOFF_SECONDS)` with `MIN_COOLDOWN_SECONDS = 5`. Keep the doubling on failure so repeated 429s still walk out to the 300 s cap.
2. Add a module constant floor rather than a config flag — there is no operator decision here, and `--oauth-usage-poll-interval` already exists for the cadence ([`config.py:380`, `749`](../../anthrouter/config.py)). **No new flag.**
3. Log the 429 once per cooldown window instead of once per attempt, and demote the per-attempt failure line to DEBUG once a cooldown is already in force. Fixes issue H at the same time.
4. Optional, only if (1) proves insufficient: the shared `_fetch_cooldown_until` gate from the 2026-09-29 note, so per-request `get()` calls cannot re-arm the loop early.

**ADR**: not needed. This is a bug fix to an invariant the code already intended ("exponential backoff on 429").

**Tests** — new file `tests/test_oauth_usage.py` (none exists today):

- `test_retry_after_zero_does_not_zero_the_backoff` — after a 429 with `Retry-After: 0`, the next sleep is >= `MIN_COOLDOWN_SECONDS`.
- `test_retry_after_is_a_floor_not_an_override` — `Retry-After: 30` with multiplier 4 and base 60 yields 240 s, not 30 s.
- `test_retry_after_above_cap_is_clamped` — `Retry-After: 9999` yields `MAX_BACKOFF_SECONDS`.
- `test_invalid_retry_after_keeps_exponential_backoff` — `Retry-After: "soon"` doubles the multiplier and logs a warning.
- `test_consecutive_429s_walk_out_to_the_cap` — four failures reach 300 s.
- `test_429_logged_once_per_cooldown_window` — one INFO line per window, not per attempt.

### Priority 2 — B and F: do not retry a 429 the upstream declined to schedule

1. `http_util.handle_error_response()` gains the response object (or just the parsed value) so it can populate the already-existing `AnthropicRequestError.retry_after` ([`mapper/common.py:36`](../../anthrouter/mapper/common.py)). Call `parse_retry_after(resp)` once and pass it for 429 and 5xx. No new field, no schema change.
2. In `_dispatch_classifier_mode` ([`model_router.py:1489-1556`](../../anthrouter/model_router.py)), retry only when the exception is plausibly transient: `connection_error` is true, or `status_code` is None or >= 500, **or** `status_code == 429 and retry_after is not None`. A 429 with no timing returns immediately. Expected effect: classifier overhead on a storm drops from ~17.5 s to ~0.6 s and upstream calls from 5 to 1 per request.
3. Add full jitter to the remaining ladder (`delay * random.uniform(0.5, 1.0)`) so concurrent sessions do not retry in lockstep.
4. Make the budget configurable: `auto_model_routing_classifier_max_retries: int = 2` on `Config`, `--auto-model-routing-classifier-max-retries`, env `ANTHROUTER_AUTO_MODEL_ROUTING_CLASSIFIER_MAX_RETRIES`, validated `0 <= n <= 4` at the bottom of `parse_args()` per `CLAUDE.md`. Also add it to `CONFIG_FIELD_METADATA` (`group: 'Model Routing'`, ADR-0007) and to the runtime-editable map if it should be live-editable.
5. New reason code `classifier_rate_limited`, added to the `ReasonCode` literal ([`model_router.py:345-383`](../../anthrouter/model_router.py)), returned instead of `classifier_failed` when the classifier's last exception was a 429. Keeps `classifier_failed` meaning "unexpected failure" and makes the storm one SQL query away. Update the "Classifier failure evidence" paragraph in `docs/agents/architecture.md` and keep the `classifier_raw_response` preview format unchanged.
6. **Deferred, needs an ADR if taken**: a short-lived breaker that skips the classifier for N seconds after K consecutive 429s. It changes routing behaviour (more requests served at the requested tier without classification), so it is a tradeoff, not a bug fix. Do not build it until (2) is measured; (2) alone removes the latency and 80% of the amplification.

**Tests** — `tests/test_model_router.py`:

- `test_classifier_429_without_retry_after_is_not_retried` — backend called exactly once; reason code `classifier_rate_limited`; no sleep.
- `test_classifier_429_with_retry_after_is_retried_within_budget` — attempts equal `max_retries + 1`.
- `test_classifier_5xx_still_retries_with_backoff` — unchanged behaviour for 502/503.
- `test_classifier_connection_error_still_retries` — `connection_error=True` path unchanged.
- `test_classifier_rate_limited_keeps_requested_model` — `applied is False`, `routed_model == requested`.
- `test_classifier_retry_delay_is_jittered` — monkeypatched `random` bounds the sleep to `[0.5x, 1.0x]`.
- `tests/test_handlers.py::test_classifier_rate_limited_row_records_reason_and_preview` — DB row carries `classifier_rate_limited` and `HTTP 429 …`.
- `tests/test_config.py::test_classifier_max_retries_env_and_bounds` — env var read; 5 and -1 rejected by `parse_args()`.
- `tests/test_transport.py::test_error_response_carries_retry_after` — `AnthropicRequestError.retry_after` is 7.0 for a 429 with `Retry-After: 7` and None without.

### Priority 3 — E: make the locked fail-closed path self-consistent

Pick one rule and apply it to all fail-closed returns that follow the lock substitution ([`model_router.py:1640`](../../anthrouter/model_router.py)):

- **Option 1 (recommended)**: match the existing `missing_final_user_text` precedent — dispatch the baseline and *record* it, i.e. set `payload['model'] = routing_baseline` and return `routed_model=routing_baseline`, `applied=(routing_baseline != requested)`. The lock keeps meaning what its help text says, and the DB stops lying. Cost rows become correct, including the Haiku→Sonnet increase.
- **Option 2**: restore `payload['model'] = requested` on every fail-closed return, so a failed classifier means "client model, untouched", and the lock applies only to successful decisions. Stronger reading of the fail-closed invariant, but it makes the lock inconsistent across paths.

Also fix the `disabled` and `model_not_eligible` early returns ([`model_router.py:1641-1659`](../../anthrouter/model_router.py)), which today dispatch the baseline while reporting `raw_model` — same defect, different trigger.

**ADR**: yes — `docs/adr/0016-locked-baseline-fail-closed-model.md`. The two options have a genuine tradeoff (truthful accounting versus the literal fail-closed wording in `CLAUDE.md`), and the decision governs several code paths.

**Tests** — `tests/test_handlers.py`:

- `test_locked_baseline_classifier_failure_dispatches_and_records_same_model` — upstream payload model and the DB `routed_model` agree.
- `test_locked_baseline_disabled_routing_dispatches_and_records_same_model`.
- `test_unlocked_classifier_failure_keeps_client_model` — regression guard for the no-lock case.
- `tests/test_model_router.py::test_locked_fail_closed_applied_flag_reflects_substitution`.

### Priority 4 — D: keep the rate-limit evidence from error responses

1. Call `parse_ratelimit_headers(resp)` on the error branch as well, before `resp.read()`, and merge into `ratelimit_out` ([`transport.py:150-205`](../../anthrouter/transport.py)). The existing five `ratelimit_*` columns then populate on `rate_limited` rows — no schema change.
2. Log `request-id`, `retry-after` and `anthropic-ratelimit-*` at WARNING in `handle_error_response()` for 429 and 5xx. This is the only way to quote an ID to Anthropic support, and the only way to settle the per-model hypothesis in section 2.
3. If `error.details.error_code` is present, include it in the log line so `enforced_spend_limit_reached` is distinguishable from a bucket 429 at a glance.
4. **Deferred**: a `request_id` column on `requests`. Useful, but it is a migration; do it only if the log proves insufficient.

**Tests** — `tests/test_transport.py`:

- `test_ratelimit_headers_captured_on_429` — `ratelimit_out` is populated when the stub returns 429 with the headers.
- `test_ratelimit_headers_captured_on_500`.
- `test_error_log_includes_request_id_and_retry_after` (caplog).
- `tests/test_handlers.py::test_rate_limited_row_stores_ratelimit_window`.

### Priority 5 — C: stop inventing `Retry-After: 1`

In `_send_json` ([`handlers.py:1266-1269`](../../anthrouter/handlers.py)), send `Retry-After` only when the originating `AnthropicRequestError.retry_after` is set (available once priority 2 step 1 lands), using that value. Omit the header otherwise, which is what the upstream did. Requires threading the value into `_send_error`/`_send_sse_error`.

**Tests** — `tests/test_handlers.py`:

- `test_429_omits_retry_after_when_upstream_gave_none`.
- `test_429_echoes_upstream_retry_after`.

### Priority 6 — G: record the attempt count

Have `_send_with_retries` report the attempt number it succeeded or failed on (return it alongside the response, or stash it on the exception), and pass it into `record_request(attempt=…)`. Low value until the retry paths above are correct, so do it last.

**Test**: `tests/test_handlers.py::test_retried_request_records_attempt_greater_than_one`.

### Risks

- Priority 2 changes routing-failure *timing*, not routing *outcomes* — fail-closed stays fail-closed. Residual risk: a genuinely transient 429 that would have cleared within 15 s now returns `classifier_rate_limited` and routes at the requested tier. The requested tier is what the client asked for, so the cost direction is safe.
- `classifier_rate_limited` is a new vocabulary item: the admin UI groups by `reason_code`, so any hard-coded reason-code list in `anthrouter/ui/src` must learn it, and `anthrouter/ui/dist` must be rebuilt with `make ui-build` and committed (CI `dist-check.yml` enforces this).
- Priority 3 option 1 will make historical and future rows disagree about identical situations. Say so in the ADR; do not backfill.
- Priority 1's longer cooldown means the OAuth card can show staler data. The card already renders `usage_stale`, so this is a UI-copy question at most.
- `oauth_usage.py` has no test file at all today, so priority 1 must add one before touching the backoff arithmetic.

---

## 5. Read-only queries to confirm or re-check each hypothesis

All are safe to run as `sqlite3 -readonly ~/.anthrouter/anthrouter.db "<sql>"`. None selects prompt or response text.

```sql
-- B, C: the storm, its reason codes and its latency cost
SELECT status, reason_code, count(*) AS n,
       min(duration_ms), avg(duration_ms), max(duration_ms)
  FROM requests
 WHERE request_ts >= '2026-10-13T18:23'
 GROUP BY status, reason_code;

-- B: is every failure the same 429, or are several causes mixed in?
SELECT substr(classifier_raw_response, 1, 60) AS preview, count(*)
  FROM requests
 WHERE reason_code = 'classifier_failed'
 GROUP BY preview ORDER BY 2 DESC;

-- C: which models surfaced 429 to the client (per-model bucket hypothesis)
SELECT requested_model, routed_model, status, count(*)
  FROM requests
 WHERE request_ts >= '2026-10-13T18:23'
 GROUP BY 1, 2, 3;

-- D: rate-limit header coverage, whole history (expect 0 before the fix)
SELECT count(*) AS rows_total,
       sum(ratelimit_requests_remaining IS NOT NULL
           OR ratelimit_tokens_remaining IS NOT NULL
           OR ratelimit_input_tokens_remaining IS NOT NULL
           OR ratelimit_output_tokens_remaining IS NOT NULL
           OR ratelimit_reset_at IS NOT NULL) AS rows_with_window
  FROM requests;

-- E: locked fail-closed rows whose cost was booked against the client's model
SELECT id, request_ts, requested_model, routed_model, applied,
       model_tier, cost_estimate, net_savings_usd, status
  FROM requests
 WHERE reason_code IN ('classifier_failed', 'classifier_invalid',
                       'disabled', 'model_not_eligible')
   AND request_ts >= '2026-10-13'
 ORDER BY id;

-- G: has attempt ever been anything but 1?
SELECT attempt, count(*) FROM requests GROUP BY attempt;

-- Baseline for "is this new?": every classifier-failure family, all time
SELECT substr(request_ts, 1, 10) AS day, reason_code, count(*)
  FROM requests
 WHERE reason_code LIKE '%classifier%'
   AND reason_code IN ('classifier_failed', 'classifier_invalid',
                       'affirmation_classifier_failed')
 GROUP BY day, reason_code ORDER BY day;
```

Issue E cannot be settled from the database alone — no column records the model actually put on the wire. Confirm from the log instead:

```bash
grep -A2 "Model lock: forcing routing baseline" ~/.anthrouter/anthrouter.log \
  | grep -E "Model routing:|Routing request:"
```

A `Model routing: … routed=<client model> applied=False` line followed by `Routing request: … model=<baseline>` is the mismatch.

Issue A, poller rate:

```bash
grep "OAuth usage 429" ~/.anthrouter/anthrouter.log | cut -c1-16 | uniq -c | tail
```

---

## 6. Citations

**Code** (all paths relative to the repository root)

- `anthrouter/oauth_usage.py:108-144` background loop and sleep computation; `:167-179` `Retry-After` override; `:146-159` the undocumented `/api/oauth/usage` call.
- `anthrouter/model_router.py:1489-1556` classifier retry ladder and `classifier_failed`; `:1497` classifier sends with the client's credentials; `:1636-1640` lock substitution; `:1641-1659` `disabled`/`model_not_eligible` early returns; `:1715-1726` `missing_final_user_text` fallback precedent; `:2008-2026` classifier fail-closed return; `:345-383` `ReasonCode` literal; `:959-965` system-prompt classifier fails open; `:1886-1900` affirmation classifier, no retry.
- `anthrouter/http_util.py:21` `MAX_RETRIES`; `:90-112` `parse_retry_after`; `:116-158` `parse_ratelimit_headers`; `:160-179` `retry_delay` / `should_retry`; `:182-212` `handle_error_response` and its non-JSON fallback.
- `anthrouter/transport.py:120-157` retry loop; `:145-148` headers captured only on 200; `:150-205` error branch.
- `anthrouter/handlers.py:666`, `669-674`, `715-727` credential extraction, OAuth cache trigger, `route_model` call; `:677-684` 429 → `status='rate_limited'`; `:1001-1022` `_record_db`; `:1157` `proxy-status` window; `:1266-1269` hard-coded `Retry-After: 1`.
- `anthrouter/db.py:63` status CHECK; `:71` `attempt` default; `:275` `record_request(attempt=1)`; `:303-306`, `:368-372` ratelimit and savings columns.
- `anthrouter/mapper/common.py:30-37` the unused `AnthropicRequestError.retry_after`.
- `anthrouter/model_config.py:11-16` `haiku` → `claude-haiku-5-5`.
- `anthrouter/admin.py:420-450` `_get_oauth_usage`, including the fetch trigger at `:434` and the hard-coded `cooldown_remaining_seconds` at `oauth_usage.py:234`.
- `anthrouter/config.py:380`, `:749` `--oauth-usage-poll-interval`; `:688-694` `--lock-requested-model`.
- Existing tests: `tests/test_transport.py:916-927` 429 without timing surfaces immediately; `tests/test_handlers.py:820-833` `classifier_failed` row shape; `tests/test_handlers.py:468-481` locked-baseline tier slot. No `tests/test_oauth_usage.py` exists.

**Documentation**

- [Rate limits](https://platform.claude.com/docs/en/api/rate-limits) — per-model limits, `retry-after` on bucket 429s, spend-cap 429 without `retry-after`, `enforced_spend_limit_reached`, the full `anthropic-ratelimit-*` header table, acceleration limits.
- [Claude API errors](https://platform.claude.com/docs/en/api/errors) — 429 `rate_limit_error` semantics, documented error body shape, `request-id`, SDK retry policy honouring `retry-after`.
- [RFC 9110 §10.2.3](https://www.rfc-editor.org/rfc/rfc9110#section-10.2.3) — `Retry-After` is a minimum delay; 0 is valid.
- `/api/oauth/usage`: no Anthropic documentation found. Third-party reports only — [Claude-Code-Usage-Monitor #202](https://github.com/Maciek-roboblog/Claude-Code-Usage-Monitor/issues/202), [claude-hud #287](https://github.com/jarrodwatts/claude-hud/issues/287), which also flag a Consumer Terms concern about using subscription OAuth tokens outside Claude Code and Claude.ai. Treated as **undocumented** throughout this note.

**Data**

- `~/.anthrouter/anthrouter.db`, read-only; queries in section 5.
- `~/.anthrouter/anthrouter.log`, lines 629,629 onward (the 2026-10-13 10:18 and 11:35 process starts); total 635,120 lines.
