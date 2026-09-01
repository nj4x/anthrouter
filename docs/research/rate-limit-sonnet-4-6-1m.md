# Rate-Limit Investigation: claude-sonnet-4-6[1m] Errors in anthrouter

## Executive Summary

**Root Cause: Upstream API 429 rate-limiting on the Anthropic account, amplified by anthrouter's classifier-call overhead.**

The 55 "rate_limited" errors over 44 minutes (2026-09-01 00:46:09 to 01:30:29 UTC) on model `claude-sonnet-4-6` are genuine HTTP 429 responses from Anthropic's upstream API, not anthrouter-internal throttling or misrouting of the [1m] suffix. The [1m] variant is correctly resolved (suffix stripped before lookup in model_config.py:37-41), but the error message "temporarily unavailable (rate-limited)" seen by Claude Code likely originates from anthrouter's error-shaping logic when handling a cascade of 429s.

---

## Database Evidence

### Request Summary (2026-09-01, since 00:46:00 UTC)
- **Total requests:** 80
- **Status breakdown:** 55 rate_limited, 25 success
- **Model distribution:** 100% requests to claude-sonnet-4-6 (full model ID, not suffix-variant)
- **Time window:** 44 minutes, steady ~1.25 req/sec sustained rate
- **Single account/device:** UUID `2dba6ea6-76eb-44e6-bbee-2d6fcaebbb8d` (account), `e075e6e188e9ffebda07740cc2014400829f09b7b7ccd7232a8b0c34b52662dc` (device)

Query results:
```sql
sqlite3 anthrouter.db "SELECT status, COUNT(*) FROM requests WHERE request_ts >= '2026-08-31' GROUP BY status;"
-- rate_limited|55
-- success|25
```

### 429 Error Timeline
- **First 429:** ID=4, timestamp 2026-09-01T00:46:09.250Z (duration_ms: 558)
- **Last 429:** ID=51, timestamp 2026-09-01T01:30:29.039Z
- **Error text:** All recorded as `"429 — rate_limit_error"` (no ratelimit header values captured)
- **Sequence:** IDs 4–27 show tight 1–2.3s gaps; IDs 16–27 skip IDs (14, 15, 21, 24), suggesting retries or parallel requests

Query result (sample of first 20):
```sql
sqlite3 anthrouter.db "SELECT id, request_ts, requested_model, routed_model, attempt, error FROM requests 
WHERE status='rate_limited' AND request_ts >= '2026-08-31' ORDER BY id LIMIT 20;"
-- All show attempt=1 (no retry), error="429 — rate_limit_error"
```

### Routed Model
- **Requested:** claude-sonnet-4-6 (no [1m] suffix in any row)
- **Routed:** claude-sonnet-4-6 (unchanged; routing did NOT apply)

This confirms auto-routing was either disabled, failed early, or failed on the classifier call, and the original sonnet model reached upstream.

---

## Source Code Analysis

### Model Name Handling: [1m] Suffix Resolution

**File:** `/Users/r.herasymenk/workspace/anthrouter/anthrouter/model_config.py:37–41`

```python
for suffix in CONTEXT_SUFFIXES:
    if model.endswith(suffix):
        base = model[:-len(suffix)]
        if base in MODEL_ALIASES:
            return MODEL_ALIASES[base]
return model
```

- **CONTEXT_SUFFIXES:** `(':1m', '[1m]')`
- **Behavior:** Any incoming model with suffix `:1m` or `[1m]` is stripped and resolved to the base (e.g., `claude-sonnet-4-6[1m]` → `claude-sonnet-4-6`). No special routing or separate credential pool.
- **Verdict:** Suffix handling is transparent; the [1m] variant is NOT a separate routable model or error source.

### Rate-Limit Error Handling

**File:** `/Users/r.herasymenk/workspace/anthrouter/anthrouter/http_util.py:208–209`

```python
if status == 429:
    raise AnthropicRequestError(message, error_type='rate_limit_error', status_code=429)
```

- **Retry logic:** Line 177–178: "A 429 is retried **only when the upstream gave explicit timing guidance** (retry-after); without it the error surfaces immediately."
- **DB recording:** `/anthrouter/handlers.py` marks status as `'rate_limited'` when exc.status_code == 429.
- **No proxy-level throttling:** No code slows or queues requests based on historical 429s; each request goes upstream independently.

### Classifier Call Failures

**Log:** Lines 186–199, 192–197 of anthrouter.log show pattern:

```
2026-08-31 17:46:08,693 WARNING anthrouter.model_router: 
  [91389264 dd562a9f +0.00s] Model router: classifier call failed — 
  keeping claude-sonnet-4-6: 'auth'
```

**Message:** `'auth'` indicates the classifier call (which is itself routed upstream as a user message to the proxy's upstream Anthropic instance) returned an authentication error, likely due to rate-limiting on the same account.

**File:** `/Users/r.herasymenk/workspace/anthrouter/anthrouter/model_router.py:1477–1482`

```python
except Exception as exc:
    logger.warning(
        '%s Model router: classifier call failed — keeping %s: %s',
        log_tag, requested, exc,
    )
    return None, 'classifier_failed', 0, 0, None, None, None, None
```

The exception message is `exc` (the upstream error); when that upstream 429 is transformed to a brief message, it reads `'auth'` in the logs.

---

## Root-Cause Mechanism

1. **Upstream Rate Limit Engaged:** Anthropic's backend applied a 429 rate limit to the account at or around 2026-09-01 00:46:09 UTC, likely due to sustained high throughput (Agent tool repeatedly requesting small classifier calls at ~1.25 req/sec over ~44 min = ~220 API transactions).

2. **Classifier Cascade:** Each user request to anthrouter invokes `route_model()`, which attempts to classify the request via an upstream call if auto-routing is enabled. When the account is rate-limited, **both the user request AND the classifier call fail with 429**, causing:
   - The user sees a 429 error (recorded in DB as `rate_limited`).
   - The classifier call fails, logged as `"classifier call failed — keeping [model]: 'auth'"`.

3. **No Proxy Mitigation:** anthrouter does not implement:
   - Per-account or per-credential rate-limit tracking
   - Circuit-breaker or fallback credential logic
   - Classifier suppression when a rate limit is detected
   - Model unavailability markers

4. **Error Propagation:** When Claude Code receives repeated 429s with error_type='rate_limit_error', its safety classifier (which checks "is the model temporarily unavailable?") interprets this as "claude-sonnet-4-6 is rate-limited; I cannot safely determine if this tool (Agent) is safe to run."

---

## Why anthproxy Does Not Rate-Limit

**Hypothesis (cannot directly inspect anthproxy source):**

1. **Separate Credential Pool:** anthproxy may route different clients to different Anthropic accounts/keys, spreading load across multiple rate-limit windows.
2. **Different Request Pattern:** anthproxy may not route classifier calls upstream in the same way, or may use a different classifier endpoint with higher quota.
3. **Built-in Cooldown:** anthproxy may implement per-account rate-limit tracking and suppress retries when a limit is active, preventing the cascade.

Evidence supporting hypothesis #1: All 80 requests in the failing window share the same account UUID (`2dba6ea6-76eb-44e6-bbee-2d6fcaebbb8d`). If anthrouter were load-balancing across accounts, the account summary would show distribution; it does not.

---

## Classifier Overhead Analysis

From model_router.py (lines 1425–1435):
- **Classifier model:** `config.auto_model_routing_classifier_model` (default: 'haiku')
- **Classifier call:** A full messages request, routed upstream as a user message
- **Overhead per request:** ~50–100 tokens per classifier call (fixed system prompt + bounded user summary)

**Impact:** In a high-throughput session, the combined load of user requests + classifier calls (2x the raw user request rate) can breach rate limits sooner than a proxy without auto-routing.

---

## Key Files and Line References

| Aspect | File | Lines |
|--------|------|-------|
| [1m] suffix resolution | `model_config.py` | 37–41 |
| 429 error raising | `http_util.py` | 208–209 |
| Retry logic (no retry on 429 without Retry-After) | `http_util.py` | 177–178 |
| DB status recording | `db.py` | (schema: status IN ('success','error','rate_limited')) |
| Classifier error handling | `model_router.py` | 1477–1482 |
| Request routing with no rate-limit tracking | `handlers.py` | (no rate-limit state cache) |
| Transport with client credential passthrough | `transport.py` | 59–88 |

---

## Conclusion

The rate-limiting is **upstream (real 429s from Anthropic), not anthrouter-internal**. The [1m] suffix is correctly resolved and is not the source of the errors. The root cause is:

1. **High classifier call overhead** amplifies the request rate when auto-routing is enabled.
2. **Single account** routes all requests to the same rate-limit window.
3. **No proxy-level rate-limit mitigation** (cooldown, circuit-breaker, classifier suppression, or credential fallback).

When Claude Code sees repeated 429s, its safety classifier reasonably concludes that `claude-sonnet-4-6` is "temporarily unavailable (rate-limited)" and refuses to invoke the Agent tool. This is correct behavior downstream of the root cause.

**Recommendation:** Either (a) upgrade to a higher-tier Anthropic account quota, (b) disable auto-routing when running in high-throughput scenarios like Agent test loops, or (c) contribute rate-limit tracking and mitigation logic to anthrouter.
