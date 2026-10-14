# Classifier 429 after the #30 fixes: anthrouter versus anthproxy

**Date**: 2026-10-14
**Status**: Investigation complete. Nothing implemented.
**Symptom**: Request #8353 (2026-10-14T19:02:27Z) and its two neighbours #8354 and #8355 record `reason_code='classifier_rate_limited'`. They are the first requests after the #30 merge (`3eee1bd`, 18:58:12Z) and the 19:00:07Z update. Every classifier call anthrouter made since 2026-10-13 got HTTP 429. Over the same days, anthproxy made classifier calls for the same Claude Code session and none of them got a 429.
**Scope**: Read-only. Both databases were opened read-only: `~/.anthrouter/anthrouter.db` with `?mode=ro`, and `~/.anthproxy/anthproxy.db` with `?immutable=1`. No code, test, config or database was changed. No credential value was read or written here. The request IDs below are Anthropic `request-id` values, not secrets.

Related notes, not repeated here:
- [2026-10-13 classifier 429 triage](2026-10-13-classifier-429-triage-and-fix-plan.md) covers retry amplification, missing evidence and the lock-model record. The #30 fixes implement it.
- [2026-09-29 OAuth usage 429 loop](2026-09-29-oauth-usage-429-retry-loop.md) covers the usage poller.

---

## 1. Summary

The #30 fixes work as designed. A classifier 429 now costs one upstream call and about 0.4–0.7 s, where it used to cost five calls and about 17 s. But the fixes treat the 429 as a rate-limit event, and it is not one. Each classifier call is rejected because of its shape: same account, same second, same token, and the main request right after it succeeds.

Likely causes, ranked:

1. **Classifier requests carry no Claude Code identity system prompt. Confidence: high.** anthproxy puts `You are Claude Code, Anthropic's official CLI for Claude.` at the start of the `system` of *every* outbound request, classifier calls included. anthrouter never does: its classifier `system` holds only the classifier instructions. In anthrouter's database since 2026-10-01, every request whose system prompt has a Claude Code or Agent SDK identity line succeeded (452/452). Every request with no system prompt got 429 (4/4). Every classifier call got 429 (79/79). Before 2026-10-01, the same identity-less shapes succeeded: 28/28 `quota` probes with no system prompt, and every classifier call through 2026-09-29.
2. **Classifier requests send no `anthropic-beta` header. Confidence: medium, as a contributing factor.** anthproxy always sends `claude-code-20250219,oauth-2025-04-20`. anthrouter builds the beta header only from the client's own betas, which it copies into the *main* payload. The classifier payload has none, so the header is left out completely. This difference has existed since anthrouter's first commit, and classifier calls still succeeded until 2026-09-29. On its own it does not explain the change, but it may be part of the same gate.
3. **Model change: `haiku` now resolves to `claude-haiku-5-5`. Confidence: low as a root cause, medium as the trigger in time.** Since `ceb53e3` (2026-10-08), anthrouter's classifier runs on Haiku 5.5. It used to run on `claude-haiku-4-5-20251001`. anthproxy's live alias file probably still maps `haiku` to Haiku 4.5. The model alone does not fail: main request #8347 ran on Haiku 5.5 with the same token and succeeded, but it carried the Claude Code system prompt. The gate may apply only to newer models. With the current data, that cannot be told apart from "the gate became active between 2026-09-29 and 2026-10-13" (see §5).

The other differences (retries, call count, caching, timeouts, credential source, streaming) do not explain the 429s. After #30, the two projects are in practice the same on retries.

**Compliance caveat, read before you fix anything.** anthproxy's fix is to make every request look like Claude Code. Anthropic's Claude Code legal page says OAuth authentication "is designed to support ordinary use of Claude Code and other native Anthropic applications". It also says Anthropic "reserves the right to take measures to enforce these restrictions … without prior notice" ([Legal and compliance](https://code.claude.com/docs/en/legal-and-compliance)). A classifier call is anthrouter's own traffic, not Claude Code's. Tagging it with the Claude Code identity is a policy question, not only an engineering one. See §6.

---

## 2. Evidence from request #8353

### 2.1 The row

`requests` row id 8353 (`~/.anthrouter/anthrouter.db`):

| Column | Value |
| ------ | ----- |
| `request_ts` | `2026-10-14T19:02:27.238Z` |
| `session_id` (inner) | `c8f81fbe-…` (Claude Code session; JSON carries `device_id`, `account_uuid`) |
| `requested_model` / `routed_model` | `claude-sonnet-5-5` / `sonnet` (lock baseline, `applied=1`) |
| `reason_code` | `classifier_rate_limited` |
| `status` | `success` (main dispatch succeeded) |
| `attempt` | 1 |
| `classifier_model` | `haiku` |
| `classifier_raw_response` | `HTTP 429 AnthropicRequestError: Error` |
| `classifier_stop_reason` | NULL |
| `duration_ms` | 5897 |
| `ratelimit_*` (all five) | NULL |
| system prompt | contains `You are Claude Code` (`instr`=123 in `prompt_store.content`) |

The rows next to it, #8354 (19:03:12Z) and #8355 (19:03:15Z), look the same. The row before it, #8352 (18:59:20Z), was in `rules` mode and made no classifier call.

### 2.2 The log (`~/.anthrouter/anthrouter.log`, local time = UTC−7)

Lines 641887–641894, for request #8353:

```
12:02:20,682 Model lock: forcing routing baseline from claude-sonnet-5-5 to sonnet
12:02:20,683 Model router: classifying (requested=sonnet prompt_chars=253 …)
12:02:21,336 WARNING anthrouter.http_util: Upstream error HTTP 429: Error type=rate_limit_error  request-id=req_011Cg2eWVkML9PNLBRbYjSVd  retry-after=None  ratelimit=None
12:02:21,336 Model router: classifier call failed (HTTP 429, rate_limit_error) — keeping sonnet: Error
12:02:21,337 Model routing: … applied=True reason=classifier_rate_limited … mode=classifier
12:02:21,339 Routing request: operation=messages stream=True model=sonnet
12:02:21,339 "POST /v1/messages?beta=true HTTP/1.1" 200 -
```

The two neighbours look the same: `request-id=req_011Cg2eZqe1yUoghbNy2rhU4` (line 641897) and `request-id=req_011Cg2eaHPJvTRUTHBg8QR6V` (line 641907).

What this shows:

- **The 429 comes from the classifier call, not from the main call.** The main dispatch, with the same credentials, went out about 3 ms later and returned 200.
- **The 429 is a real Anthropic response.** It has a `request-id`, and the log line comes from the JSON branch of `handle_error_response` (`anthrouter/http_util.py:212-218`). So the body was JSON with `error.type=rate_limit_error` and `error.message="Error"`, and no `error.code`. This corrects the 10-13 note, which read the body as plain text. A plain-text body would have logged `(plain)` (`http_util.py:220-223`).
- **There is no `retry-after` header** and no standard `anthropic-ratelimit-{requests,tokens,input-tokens,output-tokens}-*` header (`ratelimit=None`). `parse_ratelimit_headers` (`http_util.py:119-157`) does not read the `anthropic-ratelimit-unified-*` family that subscription sign-ins use. So we cannot tell whether the 429 had plan-quota headers. Claude Code's error reference says Claude Code "tells these apart from your plan limit by the absence of the unified quota headers a real limit response carries" ([Error reference](https://code.claude.com/docs/en/errors), "Server is temporarily limiting requests").
- **One attempt only.** The #37 / `9a3365b` logic returned at once, because the 429 had no `retry-after` (`anthrouter/model_router.py:1564-1573`). So #30 worked.
- **Which limit was hit is unknown.** None of requests / tokens / input-tokens was reported. The auth is an OAuth bearer, not an API key. Two facts show this: the OAuth usage poller only starts on bearer auth (see the 10-13 note, §A), and anthproxy logged `enterprise_token=yes` for the same `c8f81fbe` session (`/tmp/anthproxy.log:58305`).

### 2.3 Counts

Classifier outcomes in anthrouter, by day. A row counts if `classifier_model IS NOT NULL`. `ok` means the response was not an `HTTP …` error preview.

| Day | ok | 429 |
| --- | -- | --- |
| 2026-09-14 … 2026-09-29 | 1245 (last id 7942) | 0 |
| 2026-10-13 | 0 | 69 (first at 18:23:42Z) |
| 2026-10-14 | 0 | 10 (7 × `classifier_failed` before the fix, 3 × `classifier_rate_limited` after) |

- **Since the #30 merge**: 3 `classifier_rate_limited` rows (8353–8355), and 3 of 3 classifier calls failed.
- **Before the merge**: 69 + 7 = 76 rows with `classifier_failed` and the same `HTTP 429 … Error` preview.
- On 2026-10-14, classifier mode was on twice, from 10:49 to 10:52 local and from 12:02 to 12:06 local. Every classifier call in both windows failed. The operator then switched back to `rules` mode (the `mode=` field on `Model routing:` log lines, and `POST /admin/config` 200 at 12:00:48).

### 2.4 System-prompt identity versus outcome (anthrouter, all main requests since 2026-09-14)

The column `has identity` means the stored system prompt contains `You are Claude Code` or `You are a Claude agent, built on Anthropic's Claude Agent SDK.`

| Period | System prompt | success | error / rate_limited |
| ------ | ------------- | ------- | -------------------- |
| before 2026-10-01 | has identity | 4682 | 162 error (other causes) |
| before 2026-10-01 | none (`quota` probe) | **28** | 0 |
| since 2026-10-01 | has identity | **452** | 0 |
| since 2026-10-01 | none (`quota` probe) | 0 | **4 rate_limited** (7945, 7951, 7959, 8107) |

The 4 rate-limited main requests are Claude Code's own `quota` probes (`user_prompt_text` starts with `quota`, no `system`). Row 8107 failed in 0.45 s with no classifier involved: `rules_no_signal`, log lines 635898–635903, `Upstream error HTTP 429: Error`. So the gate also hits real Claude Code traffic that has no identity system prompt. That separates the cause from anything specific to the classifier.

### 2.5 The anthproxy A/B for the same session

`/tmp/anthproxy.log` and `~/.anthproxy/anthproxy.db`:

- 2026-10-13 and 2026-10-14: 109 + 58 classifier calls on `backend=oauth`, `classifier_model=haiku`, **all OK**. Latest is id 1643 at 2026-10-14T15:09:34Z.
- The same session `c8f81fbe`, the same 253-character "user stepped away" recap prompt that #8353 classified: anthproxy classified it at 08:09:30 local (`score=15`, log lines 58306–58308). anthrouter got a 429 on it at 12:02:20.
- A `quota`-style probe with no system prompt (`sys_blocks=0`), requested model `claude-haiku-4-5-20251001`: it succeeded through anthproxy at 2026-10-13 17:37:01 local (log lines 56563–56566). Through anthrouter, the same shape got 429 (rows 7945, 7951, 7959, 8107).

---

## 3. Side-by-side: anthrouter vs anthproxy (Anthropic OAuth path)

| Aspect | anthrouter | anthproxy | Explains the 429? |
| ------ | ---------- | --------- | ----------------- |
| **Identity system prompt** | Never added. The module docstring says it "injects no system blocks, no required betas" (`anthrouter/mapper/anthropic_transform.py:1-6`). Classifier `system` = `_CLASSIFIER_SYSTEM` only (`anthrouter/model_router.py:245`, `:1019-1055`). The system-prompt-role classifier uses `_CLASSIFIER_SYSTEM_PROMPT_TIER` (`:186`, `:873-890`). The affirmation classifier also has no identity (`:1925-1944`). | `build_body` always calls `_ensure_cc_system_prefix`, which puts `You are Claude Code, Anthropic's official CLI for Claude.` (with `cache_control`) first, for every payload, classifier included (`anthproxy/mapper/anthropic_transform.py:24`, `:353-376`, `:673-679`). Present since the initial release (`41635b8`, 2026-07-25). | **Yes, primary** |
| **`anthropic-beta`** | Built only from the client's header, which is copied into the *main* payload as `_anthropic_beta` (`anthrouter/handlers.py:658-662`). The classifier payload has none, so `merge_betas` returns `''` (`anthrouter/mapper/anthropic_transform.py:186-213`) and the header is left out (`anthrouter/transport.py:101-102`). | `REQUIRED_BETAS = ('claude-code-20250219', 'oauth-2025-04-20')` always come first (`anthproxy/mapper/anthropic_transform.py:22`, `:178-188`), and are sent on every call (`anthproxy/oauth/backend.py:45-55`). | Possibly contributing |
| **Classifier model** | `haiku` resolves to `claude-haiku-5-5` (`anthrouter/model_config.py:15`, since `ceb53e3` 2026-10-08). Before that it was `claude-haiku-4-5-20251001`. `ANTHROUTER_MODEL_ALIASES` in `~/.anthrouter/config.env` does not override `haiku`. | Default `haiku` resolves to `claude-haiku-5-5` (`anthproxy/model_config.py:129`). But `~/.anthproxy/config.json` overrides `anthropic.haiku` to `claude-haiku-4-5-20251001`, and the file overlays the defaults (`anthproxy/model_config.py:216-241`, `:277-286`). The live wire model is not recorded (see §5). | Trigger, not cause (Haiku 5.5 with identity works: row 8347) |
| **Credential** | The client's `Authorization` / `x-api-key`, forwarded byte for byte, the same dict as the main call (`anthrouter/transport.py:60-103`). Here an OAuth bearer. | The client's bearer, as `OAuthRequestCredentials` from the request (`anthproxy/oauth/backend.py:58-66`, `anthproxy/oauth_registry.py:23-26`, `:94`). Same session, same account (`enterprise_token=yes`). | No (same token) |
| **Other headers** | Passes through the client's `anthropic-version`, `anthropic-dangerous-direct-browser-access`, `x-app`, `user-agent` (`anthrouter/transport.py:52-57`). Same values as the main call, which succeeds. | Fixed values: `x-app: cli`, `User-Agent: claude-cli/2.1.280 (external, cli)`, `anthropic-dangerous-direct-browser-access: true`, `anthropic-version: 2023-06-01` (`anthproxy/oauth/backend.py:45-55`, `anthproxy/mapper/anthropic_protocol.py:11-15`). | Unlikely (anthrouter's main call has the same headers and succeeds) |
| **Retries on classifier 429** | One attempt when there is no `retry-after`. With `retry-after`, up to `auto_model_routing_classifier_max_retries` (=4 in `config.env`), capped at 10 s (`anthrouter/model_router.py:1423`, `:1511-1600`). | No classifier retry loop: one try, then `classifier_failed` (`anthproxy/model_router.py:1512-1544`). The OAuth transport never retries a 429 (`anthproxy/oauth/backend.py:109-127`). | No (one call each) |
| **Transport retries** | `MAX_RETRIES` with `should_retry`. A 429 without timing is not retried (`anthrouter/transport.py:129-160`, `anthrouter/http_util.py`). | 429 excluded explicitly (`anthproxy/oauth/backend.py:109`). | No |
| **Classifier calls per inbound request** | Up to 3: user-prompt classifier, system-prompt role classifier when weight > 0 (here 0.2, cached by hash, size 256), affirmation classifier. | Same structure (`anthproxy/model_router.py:931-934`, `:1513-1516`, `:1920-1923`). | No |
| **max_tokens / temperature / effort** | 256, temperature 0. `output_config.effort=low` on Haiku 5.5 (`anthrouter/model_router.py:242-243`, `:332`, `2d3a75a`). | 256 (`anthproxy/model_router.py:249`, `:340`). Low effort only when the model accepts it (`anthproxy/mapper/anthropic_transform.py:51`, `:604-611`). | Unlikely |
| **Streaming** | Classifier non-streaming. | Classifier non-streaming. | No |
| **Billing-header strip** | Main request: telemetry block dropped (`Sanitized system prompt` log line). | Same (`anthproxy/mapper/common.py:167-177`). | No |

---

## 4. Analysis

### 4.1 Why the #30 fixes cannot clear this

Spec #30 assumed the classifier 429 meant a quota or bucket was full, so less traffic would let it clear. Three facts contradict that:

1. **It is deterministic.** 79 of 79 classifier calls got 429 across two days. Rate-limit 429s come and go; these never stop.
2. **It is selective within the same second.** The main request on the same token, sent about 3 ms after #8353's classifier 429, returned 200. Per-model buckets cannot explain this either: #8347 ran on Haiku 5.5 at 18:56:57Z and succeeded.
3. **It has none of the signals of a documented limit.** There is no `retry-after` and no standard rate-limit header, and the message is `"Error"`. The Rate limits page says that going over RPM/ITPM/OTPM returns a 429 "along with a `retry-after` header" ([Rate limits](https://platform.claude.com/docs/en/api/rate-limits)). The 10-13 note already found that this shape does not match the documented one.

So #30 reduced the cost of each failure (fewer calls, shorter delay, correct reason code, stored evidence). It could not stop the failures, because nothing about volume causes them.

### 4.2 What separates passing requests from failing ones

The one property that splits success from failure across both proxies is this: **does the request's `system` begin with, or contain, a Claude Code or Agent SDK identity line?**

- anthrouter, since 2026-10-01: with identity, 452 successes and 0 rate-limited. Without identity, 0 successes and 83 rate-limited (79 classifier + 4 `quota` probes).
- anthproxy adds the identity to everything, and nothing it sent was rate-limited this way, including a `sys_blocks=0` `quota` probe on 2026-10-13.

anthrouter's own Claude Code `quota` probe (row 8107) failed in 0.45 s. It had Claude Code's own headers, and probably Claude Code's own betas. It had no system prompt and no classifier call. That points to the system prompt, not to anything the classifier path adds or leaves out.

### 4.3 When the gate appeared

Identity-less requests in anthrouter worked until **2026-09-28T19:20Z** (last OK `quota` probe) and **2026-09-29** (last OK classifier call, id 7942). They failed from **2026-10-13T18:23Z** onward. anthrouter did not run between 2026-09-29 12:47 and 2026-10-13 10:18 local (the `anthrouter listening` lines in the log). In that gap, two things changed together:

- **On the anthrouter side**, the wire model changed. Classifier: Haiku 4.5 became Haiku 5.5 (`ceb53e3`). `quota` probes: they used to be routed to `haiku` (Haiku 4.5); now, under `--lock-requested-model sonnet`, they are sent as `claude-sonnet-5-5`.
- **On the Anthropic side**, enforcement may have started or tightened. The documentation does not say. The legal page keeps the right to enforce OAuth-use restrictions "without prior notice".

This data cannot separate "Anthropic now gates all identity-less OAuth requests" from "Anthropic gates identity-less OAuth requests only on newer models". In both cases, the fix that removes the 429 is the identity prefix, not the model. One more data point cuts against the model-only reading: anthproxy's identity-less-by-client `quota` probe on Haiku 4.5 passed, but anthproxy *had* added the identity.

### 4.4 Why the earlier comparison missed this

The 2026-08-31 comparison report calls the two `anthropic_transform.py` files "Nearly identical" ([2026-08-31-anthproxy-vs-anthrouter-comparison.md:22](2026-08-31-anthproxy-vs-anthrouter-comparison.md)). The anthrouter commit that created the file says the identity prefix and the required betas were left out on purpose: "No system block, required beta, or cache breakpoint is injected — the client already speaks native Anthropic" (`13e6f84`, 2026-08-30). That reasoning holds for forwarded client traffic. It does not hold for requests anthrouter *creates itself* (classifier, system-prompt role classifier, affirmation classifier), and these have no client system prompt to inherit.

---

## 5. Open questions

1. **Is the `anthropic-beta` header needed as well?** The data cannot separate missing identity from missing betas, because classifier calls lack both. A single controlled call would settle it (see §6, check 1).
2. **Model or date?** Would an identity-less Haiku 4.5 classifier call with the same token still pass today? If yes, the gate depends on the model. If no, it depends on the date.
3. **anthproxy's real wire model.** `~/.anthproxy/config.json` (dated 2026-08-21) maps `haiku` to Haiku 4.5. If anthproxy was started with `ANTHPROXY_HOME` or `ANTHPROXY_CONFIG` pointing somewhere else, it used the Haiku 5.5 default. anthproxy did not record `raw_outbound_body` for these rows, so the database cannot answer this.
4. **Unified quota headers.** Did the 429s carry `anthropic-ratelimit-unified-*` headers? anthrouter does not parse that family (`anthrouter/http_util.py:119-130`). By Claude Code's documented rule, their absence means a server-side throttle unrelated to plan quota.
5. **Is the anthproxy approach allowed?** Adding the Claude Code identity to traffic that is not Claude Code is what the legal page's OAuth restrictions seem to target. This needs a decision from the owner. Engineering cannot settle it.

---

## 6. Suggested next checks and fixes (not implemented)

### Checks (read-only or one-off)

1. **Controlled A/B, 4 calls, same token, outside the proxy.** Send one tiny classifier body to `/v1/messages` four ways: (a) as is; (b) with only `anthropic-beta: claude-code-20250219,oauth-2025-04-20` added; (c) with only the identity block added as `system[0]`; (d) with both. Repeat with `claude-haiku-4-5-20251001`. Record status, `request-id` and every response header. This answers open questions 1 and 2. Get the owner's approval first, because of open question 5.
2. **Log `anthropic-ratelimit-unified-*` headers** next to the existing `request-id` / `retry-after` WARNING (`anthrouter/http_util.py:208-223`). Then the next 429 shows whether a plan quota is involved.
3. **Confirm anthproxy's config path** with `ps eww` or its launcher, to see whether `ANTHPROXY_HOME` or `ANTHPROXY_CONFIG` is set (open question 3).

### Fix options, ordered by policy risk (lowest first)

1. **Do not call the LLM classifier when the credential is an OAuth bearer.** Fall back to `rules` mode for that request. Use a new reason code, for example `classifier_skipped_oauth`, added to `ReasonCode` in `anthrouter/model_router.py`. This fails closed and adds no impersonation. The operator already does this by hand (switching to `rules` mode at 10:52 and 12:06).
2. **Give the classifier its own API key** (a new flag, for example `--classifier-api-key` / `ANTHROUTER_CLASSIFIER_API_KEY`). The OAuth token is then never used for anthrouter's own traffic. This changes the "holds no credential" posture in `CLAUDE.md`, so it needs an ADR.
3. **Treat a 429 with message `"Error"` and no `retry-after` as a circuit-breaker trigger.** After the first one, skip the classifier for the rest of the process or for N minutes. This stops the repeated failed calls, but only hides the cause. The 10-13 note already listed a breaker as needing an ADR.
4. **Copy anthproxy: add the Claude Code identity block and `REQUIRED_BETAS` to internal classifier payloads only.** This is technically the smallest change, at `build_classifier_payload` and `build_system_prompt_classifier_payload` plus the affirmation payload (`anthrouter/model_router.py:873-890`, `:1019-1055`, `:1925-1944`). It is the most likely to work, judging by anthproxy. It is also the riskiest by policy (see the caveat in §1 and open question 5). Do not choose it without an explicit decision from the owner, recorded in an ADR.

Whichever option is chosen, `docs/research/2026-08-31-anthproxy-vs-anthrouter-comparison.md:22` ("Nearly identical") should get a note: anthproxy adds a system prefix and required betas, and anthrouter does not add them on purpose.

---

## 7. Citations

**Data**
- `~/.anthrouter/anthrouter.db` (read-only): `requests` rows 7942, 7945, 7951, 7959, 8107, 8300–8362. Columns used: `reason_code`, `status`, `classifier_model`, `classifier_raw_response`, `attempt`, `ratelimit_*`, `system_prompt_sha256`, plus `prompt_store.content`, searched with `instr()` only.
- `~/.anthrouter/anthrouter.log` lines 635898–635903 (#8107) and 641870–641912 (restart, #8353–#8355).
- `~/.anthrouter/config.env`: `AUTO_MODEL_ROUTING_MODE="rules"`, `CLASSIFIER_MODEL="haiku"`, `MODEL_ALIASES` without `haiku`, `LOCK_REQUESTED_MODEL="sonnet"`, `CLASSIFIER_MAX_RETRIES="4"`. Admin token redacted.
- `~/.anthproxy/anthproxy.db` (immutable): `requests` by `backend` / `classifier_model`, 2026-10-13 and 2026-10-14. `/tmp/anthproxy.log` lines 56558–56566, 58297–58311.
- `~/.anthproxy/config.json`: `model_aliases.anthropic.haiku`.

**Code, anthrouter** (`/Users/r.herasymenk/workspace/anthrouter`)
- `anthrouter/mapper/anthropic_transform.py:1-6` (no injection, by design), `:186-213` (`merge_betas`), `:372` (`build_body`).
- `anthrouter/transport.py:52-57`, `:60-103`, `:129-160`.
- `anthrouter/handlers.py:658-662` (`_anthropic_beta` taken from the client header).
- `anthrouter/model_router.py:186`, `:242-245`, `:332`, `:873-890`, `:1019-1055`, `:1423`, `:1426`, `:1505-1600`, `:1925-1944`.
- `anthrouter/http_util.py:119-157`, `:200-226`.
- `anthrouter/model_config.py:15`.
- Commits: `13e6f84` (2026-08-30, no injection), `ceb53e3` (2026-10-08, `haiku` changed to Haiku 5.5), `2d3a75a` (2026-10-09, low effort), `fda49d2` / `9a3365b` / `3eee1bd` (the #30 fixes).

**Code, anthproxy** (`/Users/r.herasymenk/workspace/anthproxy`)
- `anthproxy/mapper/anthropic_transform.py:22`, `:24`, `:178-188`, `:353-385`, `:558`, `:673-679`.
- `anthproxy/mapper/anthropic_protocol.py:11-15`.
- `anthproxy/oauth/backend.py:45-66`, `:79-127`.
- `anthproxy/oauth_registry.py:23-26`, `:94`.
- `anthproxy/model_router.py:249`, `:340`, `:931-934`, `:1013-1045`, `:1505-1544`, `:1920-1923`.
- `anthproxy/model_config.py:124-131`, `:185-241`, `:277-286`.
- `anthproxy/_shared/__init__.py:160` (default `send_classifier_message` calls `send_message`).
- Commit `41635b8` (2026-07-25, initial release, already has `_CC_SYSTEM_PREFIX`).

**Documentation** (official only, read 2026-10-14)
- [Rate limits](https://platform.claude.com/docs/en/api/rate-limits): an over-limit 429 carries `retry-after`; limits are per model.
- [Claude API errors](https://platform.claude.com/docs/en/api/errors): `rate_limit_error` semantics and the error body shape.
- [Claude Code error reference](https://code.claude.com/docs/en/errors): "Server is temporarily limiting requests" is a server-side throttle "unrelated to your plan quota", recognised by "the absence of the unified quota headers a real limit response carries".
- [Claude Code legal and compliance](https://code.claude.com/docs/en/legal-and-compliance): OAuth is "designed to support ordinary use of Claude Code and other native Anthropic applications"; Anthropic "reserves the right to take measures to enforce these restrictions … without prior notice".
- What happens to an identity-less OAuth request, and whether the `oauth-2025-04-20` / `claude-code-20250219` betas are required: **not documented** in the official sources above.
