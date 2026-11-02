# Rate-limit errors on billing-header-only requests (requests #17047–#17058)

**Date**: 2026-11-02
**Status**: Investigation complete. Fix 1 (narrow form: keep the billing block when it is the only system block) is implemented by ADR-0025.
**Symptom**: Session `45fcbd5e`, 2026-11-02T21:11:41Z–21:11:53Z (admin UI 13:11:41–13:11:53). Rows #17047–#17058 asked for `claude-opus-5-5`, were routed to `sonnet`, and failed with `429 — rate_limit_error`. Two rows show `error`, the others show `rate_limited`. Row #17051 in the same window succeeded on `opus`.
**Scope**: Read-only. `~/.anthrouter/anthrouter.db` opened with `sqlite3 -readonly`. `~/.anthrouter/anthrouter.log` searched. No credential was read. Only the first characters of prompts were read, plus the short system prompts of the failing rows (one telemetry line each).

Builds on these notes. Their content is not repeated here:
- [2026-10-23 rate-limit errors](2026-10-23-rate-limit-errors.md): the header-less `429 rate_limit_error: Error` signature, the "identity gate", and how anthrouter handles and records upstream 429s (§3 there).
- [ADR-0017](../adr/0017-security-monitor-passthrough.md), amendment: stripping the `x-anthropic-billing-header:` block from an identity-less request makes upstream answer 429. The fix in ADR-0017 covers only the security-monitor request. The research note that the amendment links to (`2026-10-23-rate-limit-errors-after-passthrough.md`) is not in the repo.

---

## 1. Summary

1. **Same failure as ADR-0017's amendment, on a new request shape.** Each failing request has a `system` field with only one block: the `x-anthropic-billing-header:` telemetry line. There is no Claude Code identity block and there are no tools. The sanitizer (`ANTHROUTER_SANITIZE_SYSTEM_PROMPT="strip"`) removes that block, so the request goes upstream with an empty system prompt. Upstream rejects it with a header-less 429.
2. **It is not a quota.** In the same minutes, identity-bearing requests on `sonnet`, `opus` and `haiku` succeeded on the same token.
3. **The "error" rows are the same 429 on a streaming request.** anthrouter records a streaming 429 as `status='error'`. This is already described in the 2026-10-23 note, §3.3.
4. **The model does not matter here (strong inference).** All 95 requests of this shape were routed to `sonnet`. The cause is the missing billing block, not the Sonnet bucket.

---

## 2. Evidence

### 2.1 The rows

Query: `SELECT … FROM requests WHERE id BETWEEN 17040 AND 17060`.

| ids | requested → routed | reason_code | status / error | ms |
| --- | ------------------ | ----------- | -------------- | -- |
| 17047, 17052 | `claude-opus-5-5` → `sonnet` | `missing_final_user_text` | `error` / `429 — rate_limit_error` | 520, 434 |
| 17048–17050, 17053–17058 | `claude-opus-5-5` → `sonnet` | `missing_final_user_text` | `rate_limited` / `429 — rate_limit_error` | 353–664 |
| 17051 | `claude-opus-5-5` → `opus` | `classifier_deep` (system 50, user 78) | `success` | 3468 |
| 17046 | `claude-opus-5-5` → `haiku` | `classifier_trivial` | `success` | 4093 |

Common to every failing row:
- `input_tokens`, `output_tokens` and cache columns are NULL. `estimated_input_tokens` is 0. `attempt` is 1.
- All `ratelimit_*` columns are NULL. Upstream sent no `anthropic-ratelimit-*` header.
- `system_prompt_sanitized_sha256` is `4f53cda1…`. Its `prompt_store` content is `[]`, an empty list.
- `system_prompt_sha256` (the client's original prompt) is 194–243 characters. It is a single block, for example: `x-anthropic-billing-header: cc_version=2.1.309.092; cc_entrypoint=claude-desktop; cch=…; cc_prompt_id=…; cc_turn_origin=task_notification; cc_prompt_index=3; cc_turn_index=5;`.
- `tools_sha256` is NULL. The user prompt is 349–426 characters and starts with `[tool_result …]`.

The two successful rows have a sanitized system prompt that starts with `You are Claude Code, Anthropic's official CLI for Claude, running within the Claude Agent SDK.` (hashes `772db4b3…` and `ed2970e0…`). The sanitizer also removed their billing block, but the identity block stayed.

### 2.2 The log

`~/.anthrouter/anthrouter.log`, lines 719301–719372. Log time is UTC−7.

Non-streaming retry (#17048):
```
14:11:41,357 Model lock: forcing routing baseline from claude-opus-5-5 to sonnet
14:11:41,357 Model routing: requested=claude-opus-5-5 routed=sonnet classification=None applied=True reason=missing_final_user_text ctx_anchor=-------- …
14:11:41,357 Sanitized system prompt: dropped 1 volatile telemetry block(s) to restore prompt-cache prefix reuse
14:11:41,357 Routing request: operation=messages stream=False model=sonnet
14:11:41,763 WARNING anthrouter.http_util: Upstream error HTTP 429: Error type=rate_limit_error  request-id=req_011Cgenjy8ZJdm3J6zKPo5MU  retry-after=None  ratelimit=None
14:11:41,767 "POST /v1/messages?beta=true HTTP/1.1" 429 -
```

Streaming request (#17047), recorded as `error`:
```
14:11:40,820 Routing request: operation=messages stream=True model=sonnet
14:11:40,820 "POST /v1/messages?beta=true HTTP/1.1" 200 -
14:11:41,340 WARNING anthrouter.http_util: Upstream error HTTP 429: Error type=rate_limit_error  request-id=req_011CgenjwQ8Gj5y79Sc7MHtv  retry-after=None  ratelimit=None
14:11:41,344 SSE priming error (post-header): Error
```

The other upstream request-ids in the window are `req_011Cgenk44Dmh9M41gMFTVNp`, `req_011CgenkBGpB4CCZbe2xGU9V`, `req_011CgenkNksmDWPLAqokAs1a` (streaming, #17052), `req_011CgenkPgBis3nMYtSNcjk4`, `req_011CgenkQaFjY3yWmHp1hiHR` and `req_011CgenkVG3aXCjJKuLBzMNK`. Every one has the same signature: HTTP 429, message `Error`, no `retry-after`, no rate-limit headers, and an answer in 0.2–0.7 s. This is the signature of a request-shape rejection (2026-10-23 note, §2.3). It is not bucket exhaustion.

### 2.3 Aggregates

Last 24 hours, by routed model and status:

| routed_model | status | error | rows |
| ------------ | ------ | ----- | ---- |
| `sonnet` | `rate_limited` | `429 — rate_limit_error` | 83 |
| `sonnet` | `error` | `429 — rate_limit_error` | 12 |
| `sonnet` | `success` | | 77 |
| `opus` | `success` | | 144 |
| `haiku` | `success` | | 50 |
| `haiku` | `rate_limited` | `429 — rate_limit_error` | 1 (#17044) |
| `haiku` | `error` | `502 — upstream_failure` | 1 (#17076, not related) |
| `claude-sonnet-5` | `success` | | 3 (`security_monitor_passthrough`) |

The 95 Sonnet 429s are exactly the 95 rows whose sanitized system prompt is `[]`:
- All 95 have an original system prompt that is a single `x-anthropic-billing-header:` block. All 95 have no tools. All 95 have `cc_entrypoint=claude-desktop`, and 33 of them have `cc_is_subagent=true`.
- All 95 have `reason_code='missing_final_user_text'`. They asked for `claude-opus-5-5` (43) or `claude-fable-5-1` (52).
- They are the only rows of this shape in the database (data starts 2026-10-13). The first one is #16973 at 21:03:25Z. **None of them succeeded.**
- From 21:00Z to the end of the data, 11 identity-bearing requests to `sonnet` and 28 to `opus` succeeded. So the Sonnet bucket was not exhausted, and there was no account-wide throttle.
- #17044 (haiku) is a separate identity-less request with no system prompt at all (a quota probe, user text `quota`). It is the same family as the 2026-10-23 note's no-system rows.

### 2.4 Contrast: billing block kept

#17091 and #17096 (21:19Z) are security-monitor requests. They have no Claude Code identity block either. ADR-0017 forwards them unsanitized, so the billing block stays, and both succeeded. This is the controlled contrast: identity-less with the billing block succeeds; identity-less without it gets 429.

---

## 3. Code path

1. **Routing pins `sonnet`.** The final user turn is only a `tool_result`, so `build_routing_summary()` returns None. `route_model()` then falls back to the lock baseline, not to the requested model (`anthrouter/model_router.py:1777-1790`). With `--lock-requested-model sonnet` (`~/.anthrouter/config.env`), the baseline is `sonnet` (`anthrouter/handlers.py:1077-1081`). This choice is deterministic, so every client retry lands on `sonnet` again. The walk-back cache and affirmation inheritance are not involved: the log shows `ctx_anchor=--------` and `sess_ctx=0`. The tier-cache replay (`handlers.py:1144-1153`) found no cached tier. This is ADR-0016's accepted behaviour. It is not the cause, because the model does not matter here (§2.3, §2.4).
2. **The sanitizer empties the system prompt.** `_prepare()` exempts only the security monitor from sanitizing (`anthrouter/handlers.py:1019-1032`). All other requests go through `sanitize_system_prompt()` (`anthrouter/sanitizer.py:88-92`). That calls `strip_volatile_system_blocks()` (`anthrouter/mapper/common.py:173-192`). The prefix list is `('x-anthropic-billing-header:',)` (`common.py:168-170`). The docstring allows this case on purpose: "Dropping every block is allowed: an all-telemetry system field legitimately sanitizes to nothing" (`common.py:183-185`). ADR-0017's amendment shows this assumption is wrong for requests that have no identity block.
3. **Status recording.** A non-streaming 429 is stored as `rate_limited` (`handlers.py:992-997`). A streaming 429 arrives after the HTTP 200 headers are sent, so it is stored as `error` with the same error text (`handlers.py:1259-1272`, log `SSE priming error (post-header)` at `handlers.py:1765`). Only the text `'{status} — {error_type}'` is stored. The upstream body and the `request-id` are only in the log (2026-10-23 note, §3.3).
4. **No fallback.** anthrouter does not retry a 429 that has no `retry-after`. It does not retry on a different model, and it does not restore the original system prompt. The client retried by itself: the same two `tool_result` prompts were sent 5–6 times each.

---

## 4. Root cause

**Confirmed:**
- Every failed request in the window, and all 95 in the database, had a `system` field with only the `x-anthropic-billing-header:` block. After the sanitizer ran, the upstream request had an empty system prompt.
- Upstream answered each one with `HTTP 429: Error type=rate_limit_error`, with no `retry-after` and no rate-limit headers, in under 0.7 s.
- Identity-bearing requests on the same token and on the same model succeeded in the same minutes.

**Inferred (strong):** upstream rejects a request that carries neither the Claude Code identity block nor the billing block. anthrouter's `strip` sanitizer removes the billing block, which is the only marker these Claude Desktop subagent and task-notification requests carry. This is the mechanism that ADR-0017's amendment found for the security monitor. ADR-0017 fixed only that one prefix.

**Not confirmed:** whether `opus` would accept the stripped request. None of these requests reached `opus`. The billing-block contrast (§2.4) points to the request shape, not the model. One controlled request with the billing block kept would settle it.

---

## 5. Recommended fix

1. **Keep the billing block when stripping it would remove the only marker.** In `strip_volatile_system_blocks()` (`anthrouter/mapper/common.py:187-192`), return the system field unchanged when `kept` is empty. A more general rule is to strip the billing block only when a Claude Code or Agent SDK identity block remains. Change the docstring at `common.py:183-185` to match, and add a test for a billing-only `system` field. The cost is small: these requests are short and have no tools, so a cache miss on them costs little.
2. Optional: when the lock baseline would downgrade a request that has no user text, also skip the downgrade for requests with no identity block. This follows the 2026-10-23 note, option 1. It is not needed to stop these 429s.
3. Already proposed in the 2026-10-23 note and still open: store the upstream `request-id` and record a streaming 429 as `rate_limited`, so the admin UI shows one status for one cause.
