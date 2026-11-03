# Requests 20007–20009: why two errors before the replay

Session `9e9ee298`, requested `claude-fable-5-1`, routed `sonnet`, prompt `Another Claude session sent a message: <agent-message from="ace7f232a4...`.

## Answer

One expired Thread, two **client** attempts, then the client's own replay. Not an anthrouter retry, not a routing fault.

1. Main conversation idle ~7 min 50 s (waiting for a subagent). Its Thread expired (5-min TTL, ADR-0027 / ADR-0031).
2. Subagent hand-back arrives. Client sends `thread=continue`, **streaming** (#20007). anthrouter has already committed HTTP 200, so upstream's 404 reaches the client as an SSE `error` event (`invalid_request_error`).
3. Client falls back to the **same request, non-streaming** (#20008). Same dead `previous_message_id`, same 404. Now it gets a real HTTP 400.
4. 45 ms later, client replays the full conversation with `thread=create` (#20009). Routed fresh by the classifier. Success.

Two error rows = stream attempt + non-stream fallback. This is exactly the pair pattern ADR-0031 Context predicted ("sends the request once streaming and once non-streaming").

## Evidence

### DB (`~/.anthrouter/anthrouter.db`, read-only; `request_ts` is record time, UTC)

| id | request_ts | reason_code | status | error | ms | attempt | upstream_request_id | ctx tokens |
|---|---|---|---|---|---|---|---|---|
| 19853 | 20:42:16.8 | thread_pinned | success | | 1661 | 1 | | 119 847 |
| 20006 | 20:50:07.1 | thread_pinned | success | | 16175 | 1 | req_011CggetGyAKbftmE9MgzfEg | 82 407 |
| 20007 | 20:50:08.0 | thread_pinned | error | `404 — not_found_error (sent to client as 400)` | 672 | 1 | req_011CggeuVC9pvPw4N1bnAr38 | – |
| 20008 | 20:50:08.4 | thread_pinned | error | `404 — not_found_error (sent to client as 400)` | 319 | 1 | req_011CggeuYETKRZ5WmouYVLky | – |
| 20009 | 20:50:19.5 | classifier_standard | success | | 8087 | 1 | req_011CggeunbQ2uqyaMyMpoP1z | 123 670 |

- Two distinct upstream `request-id`s, both `attempt=1`: two separate inbound requests, each dispatched once.
- Context size identifies the conversation. #20009 (123 670) continues the main conversation last seen at #19853 (119 847). #20005/#20006 (78–82 k) are a different conversation in the same session id (the fable subagent, rows #19854–#20006, prompts `[handback-send-enforce] ...`). So #20006 succeeding 0.25 s before #20007 is not the same Thread.
- Main-conversation gap: #19853 ends 20:42:16.8, #20007 starts 20:50:07.37 = ~470 s > 300 s.
- Same pattern earlier in the session: #19991/#19992 (two `404` errors) then #19993 success; subagent conversation idle from 20:43:11 (#19860) to 20:48:45, ~5.5 min.

### Log (`~/.anthrouter/anthrouter.log`, lines ~753580–753600, local time UTC-7)

```
13:50:07,371 [9e9ee298 65feb965] Routing request: operation=messages stream=True model=sonnet thread=continue
13:50:08,043 Upstream error HTTP 404: No thread state was found for the requested `previous_message_id`. ... request-id=req_011CggeuVC9pvPw4N1bnAr38
13:50:08,043 Upstream Thread not found (usually expired): sent to client as 400 so it replays
13:50:08,052 [9e9ee298 65feb965] SSE priming error (post-header): No thread state was found ...
13:50:08,053 "POST /v1/messages?beta=true HTTP/1.1" 200 -
13:50:08,075 [9e9ee298 a80d5dbd] Routing request: operation=messages stream=False model=sonnet thread=continue
13:50:08,393 Upstream error HTTP 404: ... request-id=req_011CggeuYETKRZ5WmouYVLky
13:50:08,398 [9e9ee298 a80d5dbd] "POST /v1/messages?beta=true HTTP/1.1" 400 -
13:50:08,445 [9e9ee298 6edb88b5] Model lock: forcing routing baseline from claude-fable-5-1 to sonnet
13:50:11,428 [9e9ee298 6edb88b5] Routing request: operation=messages stream=False model=sonnet thread=create
13:50:19,527 [9e9ee298 6edb88b5] "POST /v1/messages?beta=true HTTP/1.1" 200 -
```

Three request tags (`65feb965`, `a80d5dbd`, `6edb88b5`) = three inbound HTTP requests. First: `stream=True`, status line 200. Second: `stream=False`, status line 400. Third: `thread=create`.

### Code

- Streaming path commits 200 before upstream is called: `anthrouter/handlers.py:1934` (`self.send_response(200)` in `_send_sse`, docstring `:1924`). Priming error goes out in-band: `:1940–1946` → `_send_sse_error`.
- 404 → 400 rewrite for both paths: `anthrouter/http_util.py:193` (`_EXPIRED_THREAD_PREFIX`), `:248–257`.
- No internal retry for this error: `RETRYABLE_STATUSES = {429, 500, 502, 503, 504}` at `anthrouter/http_util.py:22`; gate `should_retry()` `:168–179` checks the upstream status (404). ADR-0031 API Contract: "anthrouter does not retry the rewritten 400."
- Thread pin bypasses routing for continuations, so both failed rows carry `thread_pinned`; the replay is a new Thread and reaches the classifier (`classifier_standard`): ADR-0027, `docs/adr/0027-pin-thread-continuation-model.md`.
- Header-first streaming is a known tradeoff: `docs/adr/0026-upstream-request-id-and-streaming-429-status.md:12` ("A streaming request commits HTTP 200 before the upstream is called").

### Ruled out

- **anthrouter retry**: separate tags, separate request-ids, `attempt=1` each, 404 not retryable.
- **Invalid routed model**: upstream message is the Thread-state 404, not a model error; `sonnet` worked for #20009 and #20010+.
- **Walk-back cache / sanitizer / caching**: failed rows are `thread_pinned` (no routing layers ran); error is Thread state, not cache.
- **Race with #20006**: different conversation (context 82 k vs 120 k).

## Causal chain

subagent runs ~8 min → main Thread idle > 5 min → upstream drops Thread state → hand-back triggers main-conversation `thread=continue` (stream) → 404 → rewritten, but headers already 200 → SSE `error` event → client does not replay on SSE error, falls back to non-stream with same `previous_message_id` → 404 → HTTP 400 → client replays with `thread=create` → success.

## Status vs ADR-0031

- ADR-0031 Decision 7 acceptance test: **passes** here. Replay followed with no user action, 45 ms after the non-streaming 400, `thread=create` logged, large context.
- ADR-0031 open point "whether an SSE `invalid_request_error` alone also triggers it": answered **no** for this client. The client fell back to non-streaming, so the replay followed the HTTP 400, not the SSE error.

## Confidence

High on mechanism (DB + log + code agree). Medium on client internals: the stream→non-stream fallback and the 400→replay are observed client behaviour (ADR-0031 Context), not documented by Anthropic. Client version for this session not checked.

## Open questions

- Would a real HTTP 400 on the streaming attempt trigger the replay directly (skipping the non-stream fallback)? Untested.
- Could not confirm the exact `previous_message_id` sent; not stored in DB.

## Fix direction (not a bug; optional optimisation)

Cost of the extra error: one wasted upstream round trip (~0.3–0.7 s), no tokens. Behaviour works as ADR-0031 designed. To drop it to one error:

- For streaming requests marked `thread=continue`, prime the first upstream chunk (or the upstream status) **before** committing HTTP 200, so an expired-Thread 404 can go out as a real HTTP 400 on the streaming attempt. Tradeoff: loses keepalives during TTFB for those requests (ADR-0026 context); needs an ADR amending ADR-0031 Decision 2, and a client test to confirm the streaming 400 triggers replay.
- Mark ADR-0031 acceptance test as passed with this evidence (#20007–#20009), and record the SSE-alone answer.
