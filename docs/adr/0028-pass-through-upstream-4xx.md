---
artifact-type: adr
lineage-rules: root
---

**Status**: Approved. Decision 1 is amended by ADR-0031: an expired-Thread 404 goes to the client as a 400.

# Pass upstream 4xx statuses through to the client

## Context

`handle_error_response()` (`anthrouter/http_util.py`) maps four upstream statuses to the same status. A 400 gets the upstream `error.type`, or `invalid_request_error` when upstream sends none. 401, 403 and 429 get a fixed type: `authentication_error`, `permission_error` and `rate_limit_error`. Every other status, 4xx or 5xx, becomes `AnthropicRequestError(status_code=502, error_type='api_error')`, with the message prefixed `Upstream error (HTTP <n>)`. `do_POST` and `_usage_sse_wrapper` record that as `502 — api_error`, and `_send_error` sends the 502 to the client.

### Upstream API change that exposed this

Anthropic's message threads beta (2026-11-02, header `message-threads-2026-08-12`; details and sources in ADR-0027) added a 404 to the Messages endpoint. A Thread expires 5 minutes after its last request ([release notes](https://platform.claude.com/docs/en/release-notes/api)). A continuation of an expired Thread gets:

```
HTTP 404: No thread state was found for the requested `previous_message_id`. Replay the full conversation with `thread: {"type": "create"}` to start a new Thread.
```

The release notes do not document this 404 or its `error.type`. Retrying the same request cannot succeed, and the message says how to recover. Every observed 404 came 7.1–20.6 minutes after the previous turn, consistent with expiry. ADR-0027 records the open question of whether a 404 can have another cause. The client received a 502 instead, treated it as transient, and resent the same continuation with exponential backoff. Session `84fe251b` sent it 12 times between 23:37:28Z and 23:40:08Z. Each retry paid for a classifier call, and the client never replayed. Across 2026-11-02, 37 rows recorded `502 — api_error` for an upstream 404. The client already replays correctly after a Thread 400, which keeps its status.

`handle_error_response()` also logs the upstream `request-id` only for 429 and 5xx. ADR-0026 stores the request-id on every row, for every status, but the installs that produced these rows predate it. So none of these 404s, and none of the 216 Thread-mismatch 400s, can be matched to Anthropic's records, not even from the log.

## Decision

1. **Every upstream 4xx keeps its status.** For any `400 <= status < 500`, `handle_error_response()` raises `AnthropicRequestError(status_code=<upstream status>)`. The `error_type` is the upstream `error.type` when present, and the default in the table below otherwise. The message is upstream's message with no `Upstream error (HTTP n)` prefix, as the 400 branch already does.
2. **5xx and 529 keep the current mapping** to `502 api_error`. Those are transient and the client should retry them.
3. **The DB records the real status.** Both record paths already format `f'{exc.status_code} — {exc.error_type}'`, so a Thread 404 is stored as `404 — not_found_error`. `status` stays `error`. Only 429 becomes `rate_limited` (ADR-0026).
4. **The request-id is logged for every upstream error.** The `Upstream error HTTP %d` warning carries `request-id=` for every status, not only 429 and 5xx. `retry-after` and `ratelimit` stay on the 429/5xx line only. This belongs in the same decision: the DB column from ADR-0026 is useless when the row cannot be found, and the WARNING line is where these investigations start. It is the only place the request-id sits next to the upstream message.
5. Retry policy does not change. `RETRYABLE_STATUSES` is checked against the upstream status, and no 4xx other than 429 is in it.

## API Contract

| Upstream status | Client status | `error.type` sent to client (when upstream sends none) | Retried by anthrouter transport | Change |
| --------------- | ------------- | ------------------------------------------------------ | ------------------------------- | ------ |
| 400 | 400 | upstream type (`invalid_request_error`) | no | none |
| 401 | 401 | upstream type (`authentication_error`) | no | type now taken from upstream when present |
| 403 | 403 | upstream type (`permission_error`) | no | type now taken from upstream when present |
| 404 | 404 | upstream type (`not_found_error`) | no | was 502 `api_error` |
| 408 | 408 | upstream type (`invalid_request_error`) | no | was 502 `api_error` |
| 409 | 409 | upstream type (`invalid_request_error`) | no | was 502 `api_error` |
| 413 | 413 | upstream type (`request_too_large`) | no | was 502 `api_error` |
| 429 | 429 | upstream type (`rate_limit_error`) | yes | type now taken from upstream when present |
| other 4xx | same | upstream type (`invalid_request_error`) | no | was 502 `api_error` |
| 5xx, 529 | 502 | `api_error` | 500, 502, 503, 504 yes | none |

- **Message:** upstream's `error.message`, unprefixed, for every 4xx. 5xx keeps the `Upstream error (HTTP n): ` prefix.
- **Headers:** `retry-after` is still forwarded only on 429.
- **Streaming:** the status is fixed at 200 once headers are sent, so a post-header error goes out as an SSE `error` event. That event's `error.type` follows the table above.
- **408 and 409:** these are often transient, but they pass through unchanged instead of being retried. Anthropic's official SDKs retry 408 and 409 themselves, so passing the real status lets those clients keep their own retry behaviour. Under the old 502 mapping they also retried, but without knowing why.
- **Compatibility:** for 401/403/429, upstream sends the same types as the old fixed values, so in practice nothing changes. A client that saw a 502 for an upstream 4xx other than 400/401/403/429 now sees the upstream status. A 5xx is still a 502.

## Considered Options

- **Pass through only the Thread 404.** This is the smallest change. It was rejected because the same fault, a non-retryable upstream 4xx shown as a retryable 502, applies to 413, 422 and any 4xx a future beta adds. Fixing only one message would leave the next one to rediscover.
- **Keep 502 but carry the upstream type and message.** Rejected. Clients choose whether to retry from the status, not the type, so the retry loop would stay.

## Consequences

- A client gets upstream's own non-retryable status and recovery text. For a Thread 404 that means a replay with `{"type": "create"}` instead of a retry loop. (Observed later: Claude Desktop does not replay after the 404. It stops, and the user must type `Try again`. ADR-0031 sends this 404 to the client as a 400.)
- Classifier calls see the same change. The classifier retries a failure when `status_code >= 500` (`model_router.py`). An upstream 404, 413 or 422 on a classifier call used to arrive as 502 and was retried. Now it fails at once as `classifier_failed`, which is correct for a non-transient error. A 408 or 409 on a classifier call is no longer retried either. The classifier fails closed to the routing baseline (ADR-0016), so a lost retry costs one routing decision, never a failed client request.
- Dashboards and queries that grouped every non-400/401/403/429 upstream 4xx under `502 — api_error` will see new `error` values (`404 — not_found_error`, `413 — request_too_large`, …). Historical rows are not backfilled.
- A 502 from anthrouter now means an upstream 5xx or a failure inside anthrouter, never an upstream 4xx.
