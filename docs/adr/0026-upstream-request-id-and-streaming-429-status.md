---
artifact-type: adr
lineage-rules: root
---

**Status**: Accepted

# Store the upstream request-id and record a streaming 429 as rate_limited

## Context

anthrouter records one cause, an upstream HTTP 429, under two statuses. A non-streaming 429 is raised out of `_dispatch`, and `do_POST` records it as `status='rate_limited'`. A streaming request commits HTTP 200 before the upstream is called (`_send_sse`), so a 429 comes back as an `AnthropicRequestError` raised while priming the stream. `_usage_sse_wrapper` recorded that as `status='error'`. The `error` text was the same in both cases: `429 — rate_limit_error`. Dashboards and the admin UI that count `rate_limited` missed every streaming 429, and Claude Code streams almost every request.

The upstream `request-id` response header was never stored. It appeared only in the `Upstream error HTTP 429 ... request-id=...` WARNING from `http_util.handle_error_response`, which is written only for 429 and 5xx. Matching a row to that log line meant searching by timestamp. Anthropic support asks for the `request-id` when a 429 is disputed.

Research: `docs/research/2026-10-23-rate-limit-errors.md` §3.3 and `docs/research/2026-11-02-rate-limit-billing-header-only.md` §5 item 3.

## Decision

1. Schema v7 adds a nullable `requests.upstream_request_id TEXT` column (`_apply_migration_6`, with the column-exists guard from `_apply_migration_3`).
2. `AnthropicTransport._send_with_retries` writes `attempt_out['upstream_request_id']` from the `request-id` header of each attempt's response, for every status. It resets the value to None before each dispatch, so the stored id is always the final attempt's, and a final connection failure stores NULL rather than an earlier attempt's id. `_record_db` passes the value to every row: success, error and rate-limited, streaming and not.
3. In `_usage_sse_wrapper`, an `AnthropicRequestError` with `status_code == 429` records `status='rate_limited'`, the same as the non-streaming path. Every other upstream error keeps `status='error'`.
4. The request drawer shows an `Upstream request-id` row in the Request section when the column is set, and no row when it is NULL.

Out of scope: a mid-stream in-band `event: error` frame (for example `overloaded_error`) still records `status='error'`, `error='sse_error'`. That frame arrives inside a 200 response, and the stream's own error type is not parsed today. The client still gets HTTP 200 plus an in-band error frame for a streaming 429; this decision changes only what is recorded.

## Consequences

- One status per cause: `rate_limited` counts now include streaming 429s, so the count rises after deploy without any change in upstream behaviour.
- Rows written before v7 have NULL `upstream_request_id`. NULL on a new row means no upstream response arrived, or the response carried no `request-id` header.
- The id is stored for successful requests too. That costs one short string per row and lets any row be matched to Anthropic's records, not only failed ones.
- With retries, only the final attempt's id is kept. The ids of retried attempts are neither stored nor logged: the `Upstream HTTP %d (attempt …) — retrying` line does not carry them.
