# Rate-limit errors seen through anthrouter (request #13224)

**Date**: 2026-10-23
**Status**: Investigation complete. A narrowed form of option 1 (security-monitor requests only) is implemented by ADR-0017.
**Symptom**: Request #13224 (2026-10-23T18:44:18.499Z) and its four retries #13225–#13228 were recorded as `status='rate_limited'`, `error='429 — rate_limit_error'`.
**Scope**: Read-only. `~/.anthrouter/anthrouter.db` opened with `sqlite3 -readonly`. No code, config or database was changed. No credential was read. Prompt text was not read beyond the first characters of the system prompt and the stored classifier summary.

Related notes, not repeated here:
- [2026-10-13 classifier 429 triage](2026-10-13-classifier-429-triage-and-fix-plan.md): retry amplification, missing evidence, lock-model record. Fixed by #30.
- [2026-10-14 classifier 429 vs anthproxy](2026-10-14-classifier-429-anthproxy-diff.md): identity-less requests on an OAuth token get 429. Fixed for anthrouter's own classifier by #40 (`2fe0cd5`).
- [rate-limit-sonnet-4-6-1m](rate-limit-sonnet-4-6-1m.md): an earlier 429 burst (2026-09-01), rows since aged out of the 30-day retention.

---

## 1. Summary

1. **#13224 did not hit a quota.** It was not RPM, input TPM or output TPM. It was Claude Code's auto-mode **security-monitor** classifier request. The client asked for `claude-sonnet-5`. anthrouter rewrote it to `claude-sonnet-5-5` (`--lock-requested-model sonnet`, then the classifier said "standard", which maps to `sonnet`). The upstream answered 429 in about 0.5 s with the message `Error`. The response had no `retry-after` and no `anthropic-ratelimit-*` headers. Six seconds later the main conversation request on the same token succeeded.
2. **The pattern holds across the whole database (inference, strong):** every one of the 15 `rate_limited` rows lacks the Claude Code identity system block. Of the 6,270 rows that carry it, none was rate-limited. This is the same "identity gate" the 2026-10-14 note found for anthrouter's own classifier calls. #40 fixed it for those calls only, not for client requests that arrive without the identity.
3. **Model rerouting is the trigger for #13224 (inference, medium).** Claude Code's own docs say the auto-mode classifier runs on Sonnet 5, and it deliberately falls back to Sonnet 5 when the configured Sonnet model is Sonnet 5.5 or Opus 5.5. anthrouter overrides that choice and sends the request to Sonnet 5.5.
4. **Top recommendation:** do not reroute Claude Code side-channel requests (the auto-mode monitor, and other identity-less requests). Pass them through on the model the client asked for, even under `--lock-requested-model`. Second: forward the upstream 429 faithfully (original body, `request-id`, `anthropic-ratelimit-*`, `x-should-retry`), and persist `request-id` so a support ticket can be opened.

---

## 2. What happened in #13224

### 2.1 The row

Query: `SELECT … FROM requests WHERE id=13224` (all columns listed below).

| Field | Value |
| ----- | ----- |
| `request_ts` | `2026-10-23T18:44:18.499Z` |
| session | `1605e63f-e56f-4a82-9ec3-adee8c7c0786` (same account and device as every other row) |
| `requested_model` | `claude-sonnet-5` |
| `routed_model` | `sonnet` → `claude-sonnet-5-5` (`ANTHROUTER_MODEL_ALIASES` in `~/.anthrouter/config.env`; default in `anthrouter/model_config.py:14`) |
| `reason_code` | `classifier_standard` (classifier score 45; system-prompt score 78; blended 52) |
| `classifier_model` | `haiku` (→ `claude-haiku-5-5`) |
| `status` / `error` | `rate_limited` / `429 — rate_limit_error` |
| `duration_ms` | 3436 (about 2.8 s of that is anthrouter's classifier call, per the log) |
| `attempt` | 1 |
| token columns | all NULL (no usage is learned on a failed dispatch: `handlers.py:1035-1044`) |
| `estimated_input_tokens` | 0 |
| `ratelimit_*` columns | all NULL (the upstream sent no `anthropic-ratelimit-*` header) |
| system prompt | starts `"You are a security monitor for autonomous AI coding agents."`, 141,831 chars, no Claude Code identity block (`prompt_store` join on `system_prompt_sanitized_sha256`) |
| user prompt | 190,735 chars: a transcript of the session ending `Respond with <severity>N</severity> ONLY.` (from `classifier_summary_json`) |

This is Claude Code's auto-mode classifier. Claude Code docs: "A separate classifier model reviews actions before they run" and "By default, classifier requests go to Claude Sonnet 5" ([permission modes](https://code.claude.com/docs/en/permission-modes)).

### 2.2 The log

`~/.anthrouter/anthrouter.log` lines 682057–682064 (local time PDT = UTC−7):

```
11:44:15,063 Model lock: forcing routing baseline from claude-sonnet-5 to sonnet
11:44:16,871 Model router: score=45 (tier=standard) (requested=sonnet)
11:44:17,895 Model routing: requested=claude-sonnet-5 routed=sonnet classification=standard applied=True reason=classifier_standard … mode=classifier
11:44:17,901 Routing request: operation=messages stream=False model=sonnet
11:44:18,494 WARNING anthrouter.http_util: Upstream error HTTP 429: Error type=rate_limit_error  request-id=req_011CgKfQvvwameJJYYCtcgY3  retry-after=None  ratelimit=None
11:44:18,539 "POST /v1/messages?beta=true HTTP/1.1" 429 -
```

The upstream error, as logged: HTTP 429, `error.type=rate_limit_error`, `error.message="Error"`, `request-id=req_011CgKfQvvwameJJYYCtcgY3`, no `retry-after`, no `anthropic-ratelimit-*`. The full body was not persisted (§3.3). The four retries got the same answer: `req_011CgKfR9U12U7HSbf3W96XB`, `req_011CgKfRPohRm9x6GdVNSgbB`, `req_011CgKfRiP9f13AGXubWyzZQ`, `req_011CgKfS73urzGWwNpUEesci` (lines 682072–682112).

### 2.3 Which limit? None of the documented ones

- **Not a documented rate limit.** Anthropic says a rate-limit 429 is "describing which rate limit was exceeded" ([rate limits](https://platform.claude.com/docs/en/api/rate-limits#rate-limits)). The message here is only `Error`.
- **Not the spend cap.** A spend-cap 429 has a long message and `details.error_code: enforced_spend_limit_reached` ([rate limits, spend cap](https://platform.claude.com/docs/en/api/rate-limits#reaching-your-spend-cap)). Also, other requests on the same token succeeded before and after.
- **No pressure on the Sonnet 5.5 bucket.** Each model row has its own RPM/ITPM/OTPM, and Sonnet 5.5 is separate from Sonnet 5 ([rate limits, footnote 3](https://platform.claude.com/docs/en/api/rate-limits#rate-limits)). In minute 18:44 the DB holds 7 requests. Their uncached input is 272,968 tokens, almost all of it one Haiku 5.5 cache write (#13223, `cache_creation_tokens=271308`). The only Sonnet 5.5 traffic in that minute is the five rejected calls. The previous Sonnet 5.5 request was #13204 at 18:29, and it succeeded. Query: `SELECT substr(request_ts,12,5), count(*), sum(input_tokens+cache_creation_tokens) … BETWEEN '18:40' AND '18:47' GROUP BY minute`.
- **No account-wide throttle.** #13229 (18:44:36, same session, `claude-haiku-5-5`, Claude Code identity) succeeded with `cache_read_tokens=290214`. #13230–#13237 (another session, `claude-opus-5-5`) succeeded from 18:45:58.
- **Fast rejection.** Each 429 came back 0.47–0.65 s after dispatch (log timestamps). That is consistent with a policy check before inference (inference).

Conclusion (inference): the 429 is a request-shape rejection that Anthropic labels `rate_limit_error`. It is not a token-bucket limit. The signature matches the one seen on 2026-10-13/14 for identity-less classifier calls (`Error`, no headers, sub-second).

### 2.4 Neighbouring requests and Claude Code's retries

| id | ts (UTC) | requested → routed | reason | result |
| -- | -------- | ------------------ | ------ | ------ |
| 13223 | 18:44:13.798 | sonnet-5-5 → haiku | `affirmation_classified` | success; cache write 271,308 |
| 13224 | 18:44:18.499 | sonnet-5 → sonnet | `classifier_standard` | 429 |
| 13225 | 18:44:21.440 | sonnet-5 → sonnet | `classifier_trivial_blended_standard` | 429 |
| 13226 | 18:44:24.794 | sonnet-5 → sonnet | `classifier_invalid` | 429 |
| 13227 | 18:44:29.196 | sonnet-5 → sonnet | `classifier_trivial_blended_standard` | 429 |
| 13228 | 18:44:34.511 | sonnet-5 → sonnet | `classifier_standard` | 429 |
| 13229 | 18:44:36.920 | sonnet-5-5 → haiku | `session_cached_walkback_tool_result` | success |

Observations:
- Claude Code sent the monitor request 5 times over 16 s, then moved on. anthrouter returns a non-streaming 429 with no `Retry-After`, so the client falls back to its own backoff.
- **Each retry triggered a fresh anthrouter classifier call** on Haiku 5.5, about 1–2.8 s each (log lines `Model router: classifying … prompt_chars=4000`). The classifier gave 45, 25, invalid, 25, 38. All five ended on `sonnet` anyway, because the lock baseline is `sonnet` and blending lifted the trivial scores. That is about 9 s of added latency and 5 wasted Haiku calls for nothing.
- #13226: the Haiku classifier answered `Wait, I need to output only an integer. This request is a harness-style transcript of CLI commands with embedded instruc…` (`classifier_invalid`). The monitor's transcript confused the router's classifier.
- `affirmation_classified` on #13223 moved the main conversation from Sonnet 5.5 to Haiku and forced a 271k-token cache write. That write counts toward Haiku ITPM ([cache-aware ITPM](https://platform.claude.com/docs/en/api/rate-limits#cache-aware-itpm)). It did not cause this 429, but it is the largest ITPM cost in the window.

### 2.5 Overall frequency

Data covers 2026-09-25T01:14Z to 2026-10-23T18:47Z, 6,279 rows (30-day retention).

| status / error | rows |
| -------------- | ---- |
| `success` | 6,261 |
| `rate_limited` / `429 — rate_limit_error` | 15 |
| `error` / `502 — api_error` | 2 (DNS failure, #8664–8665) |
| `error` / `502 — upstream_failure` | 1 (mid-stream `IncompleteRead`, #11829) |
| 529 / `overloaded_error` | 0 |

By day, 429s fell on 2026-10-13 (3), 10-14 (3), 10-15 (4) and 10-23 (5).

By system-prompt identity (join on `prompt_store`, `LIKE '%You are Claude Code, Anthropic''s official CLI%' OR LIKE '%Claude Agent SDK%'`):

| system prompt | success | rate_limited | error |
| ------------- | ------- | ------------ | ----- |
| Claude Code identity | 6,267 | **0** | 3 |
| no system prompt | 3 (2026-09-25/28 only) | **10** | 0 |
| other (security monitor) | 0 | **5** | 0 |

By model:
- The 10 no-system rows (#7945–#8676, 10-13 to 10-15) all asked for `claude-haiku-4-5-20251001`. They were dispatched as `claude-haiku-4-5-20251001` (3 rows on 10-13 plus 1 on 10-14), `sonnet` (2) or `haiku` (4). So keeping the model unchanged did not save them. The identity-less shape alone was enough.
- The 5 monitor rows all asked for `claude-sonnet-5` and were sent to `claude-sonnet-5-5`. `claude-sonnet-5` never reached the upstream unchanged in this database. The lock rewrites every request.

By preceding classifier call: the 10-13 rows record `classifier_failed` (the classifier itself got 429 before #40). The 10-23 rows had a successful classifier call right before each 429. So classifier traffic did not exhaust a limit on 10-23.

The OAuth usage poller is no longer looping: 11 `OAuth usage 429` log lines on 2026-10-23, against about 7 per second on 10-13. It is not a factor here.

---

## 3. How anthrouter handles upstream 429 and 529 today

### 3.1 Retries

- Transport constants: `MAX_RETRIES = 3`, `RETRYABLE_STATUSES = {429, 500, 502, 503, 504}`, base 1 s, max 30 s (`http_util.py:21-24`).
- A 429 is retried **only if** the upstream sent `retry-after` or `retry-after-ms` (`http_util.py:168-179`). Sleep = upstream value, else `1·2^attempt` capped at 30 s, no jitter (`http_util.py:160-165`, `transport.py:158-165`). #13224 had no header, so it was not retried.
- **529 is not in the retry set.** It falls through to `handle_error_response` and is re-raised as **HTTP 502 `api_error`** with message `Upstream error (HTTP 529): …` (`http_util.py:240-245`). The client never sees 529 or `overloaded_error`. The official SDK still retries a 502 because it retries any status ≥ 500 (§4.4).
- Network errors: up to 3 retries with the same backoff (`transport.py:136-151`).
- No handler-level retry (`handlers.py:7-11`).

### 3.2 What the client receives

- **Non-streaming:** status 429 is preserved. The body is **re-synthesised** as `{"type":"error","error":{"type":"rate_limit_error","message":<upstream message>}}` (`handlers.py:1253-1255`, `mapper/common.py:8-15`). The upstream `request_id`, any `error.details` and every upstream header are dropped. The only header kept is `Retry-After`, rounded up to whole seconds, and only when the upstream sent one (`handlers.py:1274-1291`). `anthropic-ratelimit-*`, `request-id` and `x-should-retry` are not forwarded.
- **Streaming:** HTTP 200 and the SSE headers are committed **before** the upstream is called (`handlers.py:1367-1382`). An upstream 429 or 529 therefore reaches the client as HTTP 200 plus an in-band `event: error` frame, with no `Retry-After` (`handlers.py:1384-1404`, `_send_sse_error` at `handlers.py:1357-1365`). Anthropic's own API would return a real 429 status before the stream starts ([errors](https://platform.claude.com/docs/en/api/errors#http-errors)). So the client SDK's status-based retry never fires. Inference: Claude Code may still retry on the error event; not verified.

### 3.3 What is recorded

- Non-streaming 429: `status='rate_limited'`, `error='429 — rate_limit_error'` (`handlers.py:685-690`).
- **Streaming 429: `status='error'`**, not `rate_limited`. The error text is the same (`handlers.py:906-908`, `924-931`). Dashboards that count `status='rate_limited'` miss streaming 429s.
- A mid-stream `overloaded_error` SSE event is recorded as `status='error'`, `error='sse_error'` (`handlers.py:902-903`, `929`).
- `ratelimit_*` columns are filled from response headers on errors as well as successes (`transport.py:167-168`, `handlers.py:1026`). They were NULL for #13224 because the upstream sent none.
- `attempt` is now recorded (`handlers.py:1027`).
- **Not recorded in the DB:** the upstream `request-id`, the raw error body, `retry-after`, and `error.details.error_code`. The `request-id` is only in the log line (`http_util.py:208-218`). That log line reads `error.code` (`http_util.py:202`), not `error.details.error_code`, which is where Anthropic puts `enforced_spend_limit_reached`. So a spend-cap 429 would look the same as #13224 in both the log and the DB.

### 3.4 The router's classifier and rate limits

- **It uses the client's credential.** `send_fn` falls back to `target.backend.send_message`, the same transport and the same forwarded `x-api-key` or bearer token (`model_router.py:1507-1516`). Every classifier call counts toward the client's limits on the classifier model's bucket (Haiku 5.5 here). It does not count toward the routed model's bucket.
- Each call is small: max 256 output tokens (`model_router.py:242`). In this window it used about 2,553 input tokens (log line 682075). On 2026-10-23 it ran once per Claude Code retry, because each retry missed the walk-back cache.
- Extra calls: the system-prompt role classifier is a separate call gated by `auto_model_routing_system_prompt_weight` (AGENTS.md). It is cached by prompt hash (`ANTHROUTER_AUTO_MODEL_ROUTING_SYSTEM_PROMPT_CACHE_SIZE=256`).
- **On a classifier 429:** it retries only if transient, that is a connection error, 5xx, or a 429 *with* `retry-after` (`model_router.py:1539-1573`). It waits at most 10 s (`model_router.py:1423`, `1583-1591`), and `max_retries` is 1 in this config (`config.env`). Then it returns `classifier_rate_limited` or `classifier_failed` and fails closed. Transport retries are nested inside these (3 per classifier attempt for 5xx).
- **Fail-closed target under the lock:** failure keeps the *baseline* (`sonnet`), not the client's model. ADR-0016 accepts that the wire carries the baseline under `--lock-requested-model` (`docs/adr/0016-locked-baseline-fail-closed-model.md`). For #13224, even a classifier failure would have sent the request to `claude-sonnet-5-5`.
- Since #40, classifier payloads get the Claude Code identity block prepended (`mapper/anthropic_transform.py:29-37`, `431-436`). That applies only to payloads that carry the classifier sentinel key. Client payloads are untouched, which is why the security monitor still went out without the identity.
- All `ReasonCode` values: `model_router.py:347-384`.

### 3.5 Does rerouting move traffic onto a different limit?

Yes. Each model row has its own bucket ([rate limits](https://platform.claude.com/docs/en/api/rate-limits#rate-limits)). Sonnet 5 and Sonnet 5.5 are separate buckets, and so are Opus 5 and Opus 5.5. Haiku 5.5 is separate too. The published Start/Build/Scale numbers for Sonnet 5, Sonnet 5.5, Opus 5.5 and Haiku 5.5 are the same (for example, Start tier: 1,000 RPM / 2,000,000 ITPM / 400,000 OTPM). Moving between them does not lower the API-key ceiling. It does move usage into a different bucket, and a tier switch causes a cache miss: the new model writes the whole context to cache, which counts toward ITPM (#13223: 271k tokens). Subscription (OAuth) limits are not published per model in these docs, so their per-model behaviour is unknown.

---

## 4. Anthropic's documented behaviour (primary sources)

### 4.1 Rate-limit model ([platform.claude.com/docs/en/api/rate-limits](https://platform.claude.com/docs/en/api/rate-limits))
- Limits are set per organization and by usage tier (Start, Build, Scale, Custom; plus an Evaluation tier for new organizations).
- "The API uses the token bucket algorithm … your capacity is continuously replenished up to your maximum limit, rather than being reset at fixed intervals."
- Short intervals: "a rate of 60 requests per minute (RPM) might be enforced as 1 request per second. Short bursts of requests can exceed the limit."
- Limits are measured "in requests per minute (RPM), input tokens per minute (ITPM), and output tokens per minute (OTPM) for each model class".
- **Cache-aware ITPM:** `input_tokens` and `cache_creation_input_tokens` count toward ITPM. `cache_read_input_tokens` do not count "for most models". Haiku 3.5 is the exception.
- ITPM is estimated at the start of a request and then adjusted. OTPM counts the tokens actually generated, and `max_tokens` does not factor in.
- Acceleration limits: a "sharp increase in usage" can also cause 429s.
- **Spend cap:** HTTP 429 `rate_limit_error` with `details.error_code: enforced_spend_limit_reached` and **no** `retry-after`. Retrying "fails until access resumes".
- The Claude Code workspace has its own spend limits and "can instead receive a 429 that carries a `retry-after` header".

### 4.2 Response headers (same page, "Response headers")
`retry-after` ("Not every 429 carries it"), `anthropic-ratelimit-{requests,tokens,input-tokens,output-tokens}-{limit,remaining,reset}` (reset times in RFC 3339), and `anthropic-priority-*` (Priority Tier only). The `tokens-*` set reports the most restrictive limit currently in effect.

### 4.3 429 vs 529 ([platform.claude.com/docs/en/api/errors](https://platform.claude.com/docs/en/api/errors))
- 429 `rate_limit_error`: "Your organization has hit a rate limit, reached its usage tier's monthly spend cap, or reached a spend limit on the Claude Code workspace."
- 529 `overloaded_error`: "The API is temporarily overloaded … can occur when the API experiences high traffic across all users." It is not tied to the organization.
- Error bodies always carry `error.type`, `error.message` and a top-level `request_id`. Every response has a `request-id` header.
- When streaming, "an error can occur after the API returns a 200 response". It then arrives as an SSE error event.
- "The official SDK automatically retries transient failures (such as connection errors, rate limits, and 5xx server errors) with exponential backoff, twice by default, honoring the `retry-after` header when present."

### 4.4 Official Python SDK retry logic
`anthropic-sdk-python` `main` at `f26777e`:
- [`_constants.py`](https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/_constants.py): `DEFAULT_MAX_RETRIES = 2`, `INITIAL_RETRY_DELAY = 0.5`, `MAX_RETRY_DELAY = 8.0`.
- [`_base_client.py`](https://github.com/anthropics/anthropic-sdk-python/blob/main/src/anthropic/_base_client.py), `_should_retry` (lines 932–965): it obeys `x-should-retry: true/false` first. Then it retries 408, 409, **429** (always, header or not) and any status **≥ 500**, so 529 is retried.
- `_parse_retry_after_header` (872–904): `retry-after-ms`, then `retry-after` as seconds, then as an HTTP date.
- `_calculate_retry_timeout` (906–930): uses `retry-after` if > 0. Otherwise `min(0.5·2^n, 8)` times a jitter factor in `[0.75, 1.0]`.

Consequence for a proxy: dropping `x-should-retry` takes away the server's retry hint. Turning 529 into 502 keeps the SDK retrying, but hides the cause.

### 4.5 Claude Code auto mode ([code.claude.com/docs/en/permission-modes](https://code.claude.com/docs/en/permission-modes))
- "By default, classifier requests go to Claude Sonnet 5." With `ANTHROPIC_DEFAULT_SONNET_MODEL` set, Claude Code uses "Its value, or Sonnet 5 if its value is Claude Sonnet 5.5 or Claude Opus 5.5". **Claude Code itself avoids Sonnet 5.5 and Opus 5.5 for this workload.**
- A failed or unparseable classifier request means Claude Code "denies the action".
- When `ANTHROPIC_BASE_URL` points at a proxy, Claude Code asks the server-side review first. It "falls back to its own classifier requests" when the server does not review. Inference: this is why only 5 monitor requests ever reached anthrouter's DB.
- "classifier calls count toward your token usage" on API accounts.

---

## 5. Root cause

**#13224 (inference, high confidence on the mechanism, medium on the model's role):** Claude Code made a client-side auto-mode classifier call on `claude-sonnet-5`, with the security-monitor system prompt and no Claude Code identity block. anthrouter's lock and classifier rewrote the model to `claude-sonnet-5-5`. The upstream rejected that request shape on this OAuth token with a header-less `429 rate_limit_error: Error`. That is a policy or shape rejection under a rate-limit label, not bucket exhaustion. Claude Code retried 4 times, and each retry paid for a fresh router classifier call. Then the action was most likely denied (docs: no verdict means deny).

Two facts cannot be told apart with the current data:
- (a) The identity-less shape is rejected on any model. Supported by the 10-14 no-system rows that were rejected even on unchanged `claude-haiku-4-5-20251001`.
- (b) Sonnet 5.5 specifically rejects it, while Sonnet 5 would accept it. Supported by Claude Code's deliberate Sonnet 5 fallback away from 5.5.

Claude Code is designed to send this request unproxied on Sonnet 5, and presumably that works. So the rewrite is the part anthrouter controls either way.

**General pattern:** in 30 days, every anthrouter 429 (15 of 15) is an identity-less request. None of the 6,270 identity-bearing requests was rate-limited. There is no evidence of a real RPM/ITPM/OTPM exhaustion in the retained data. There are no 529s.

---

## 6. Options (ranked)

1. **Do not reroute requests that are not the main Claude Code conversation.** If the system prompt does not start with the Claude Code or Agent SDK identity block (for example the auto-mode monitor, quota probes or no-system requests), skip routing *and* the lock. Pass through `requested_model` unchanged, with a new reason code such as `side_channel_passthrough`, added to `ReasonCode`.
   - *Pros:* fixes #13224's trigger. Removes the 5× wasted classifier calls. Respects Claude Code's own model choice. Fails closed in the strict sense.
   - *Cons:* no savings on these requests (small: 15 rows in 30 days). Needs an ADR, because it narrows `--lock-requested-model` (ADR-0016). It does not fix reading (a) if that is the true cause.
2. **Forward upstream errors faithfully.** Pass through the original JSON body (keeping `request_id` and `details`). Pass through `request-id`, `retry-after`/`retry-after-ms`, `x-should-retry` and `anthropic-ratelimit-*`. Return 529 as 529 `overloaded_error`, not 502.
   - *Pros:* the client and SDK make the right retry decisions (§4.4). Spend-cap 429s become distinguishable. Matches the "transparent passthrough" goal.
   - *Cons:* small change to the error contract. Tests assert the current shape.
3. **Persist error evidence.** Add `upstream_request_id`, `retry_after` and `error_code` columns (from `error.details.error_code`). Record a streaming 429 as `rate_limited`, not `error`. Fix the log to read `details.error_code`.
   - *Pros:* cheap. Makes the next investigation a single query. Gives `request-id` values for a support ticket.
   - *Cons:* schema migration.
4. **Delay the streaming 200 until the upstream answers.** Commit HTTP 200 only after the upstream status is known, so a pre-stream 429 or 529 reaches the client as a real status with `Retry-After`. Keep the keepalive comments only after that point.
   - *Pros:* restores SDK retry semantics for streaming, which is most traffic.
   - *Cons:* loses the "headers early" behaviour that keepalives were added for. Needs care around first-byte timeouts.
5. **Avoid duplicate classifier work on client retries.** Cache the routing decision by (session, final-user-text hash) for a short TTL, so a retried identical request reuses it.
   - *Pros:* removes about 2 s and one Haiku call per client retry.
   - *Cons:* another cache to keep consistent with the tier and walk-back caches.
6. **Add the Claude Code identity to every outbound request** (anthproxy's approach).
   - *Pros:* probably eliminates this 429 class.
   - *Cons:* the 2026-10-14 note flags a compliance question. It would also change the monitor's prompt, which Anthropic tuned. **Not recommended** without an owner decision.
7. **Limit cache-busting tier switches** (for example #13223's 271k-token cache write on a Sonnet-to-Haiku switch). Prefer to stay on the cached tier when the context is large.
   - *Pros:* lowers ITPM use, which is the real quota risk if traffic grows. Also lowers cost.
   - *Cons:* lower savings on cheap turns. Not related to #13224.

---

## 7. Open questions

1. Does `claude-sonnet-5` accept the same identity-less monitor request on this OAuth token? One controlled request would settle reading (a) vs (b) in §5. It was not done, because this investigation was read-only.
2. Was the auth an OAuth bearer token? Inference yes: the account UUID is in `session_id`, and the OAuth usage poller ran today. anthrouter does not log the auth type per request.
3. Did Claude Code deny the action after the 5th 429, or did the server-side review supply a verdict? The anthrouter data cannot show this. Claude Code's `classifier_verdict` telemetry would ([monitoring](https://code.claude.com/docs/en/monitoring-usage)).
4. How does Claude Code react to an HTTP 200 + SSE `error` frame carrying `rate_limit_error`, compared with a real 429? This decides how urgent option 4 is.
5. The 2026-10-13/14 no-system `claude-haiku-4-5-20251001` requests: which Claude Code feature sends them? A quota probe is likely. Were they rejected because they had no identity, or because of the model?
