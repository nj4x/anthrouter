---
artifact-type: adr
lineage-rules: root
---

# ADR-0004: Admin request-detail panel exposes full prompt/tools content unconditionally

## Context

`admin.py`'s `_get_request_detail()` returns only `system_prompt_sha256`/`tools_sha256` hashes; `RequestDetailDrawer.tsx` renders no prompt or tool content, unlike the predecessor project `anthproxy`, whose `admin.py`/`db.py` JOIN `prompt_store` to return full content. A prior research pass flagged this as a privacy consideration for anyone with admin UI access and sketched a possible `--admin-scrub-prompt-content` opt-out flag, but left the tier choice open.

## Decision

Ship the `prompt_store` JOIN unconditionally, matching `anthproxy`'s behavior, with no scrub flag.

**Trust equivalence justification:** The default bind address is `127.0.0.1` (`config.py:112`). When the admin UI is enabled and the server listens on a non-loopback address, `server.py:51-56` logs a `SECURITY` warning: "admin UI has no authentication — any host on this network can read conversation history." Under loopback default, the operator has full SQLite read access (same trust boundary as the UI) — both channels grant access to the same content. A display-layer scrub flag would block only the UI path without securing the SQLite or HTTP API paths, creating a false sense of data protection for operators who believe scrubbing the UI is sufficient defense. Proper defense is network isolation via `--host` and firewall controls, not application-layer obfuscation.

**Attack vector distinction:** The scrub flag is not rejected because hashes are publicly derivable. Rather, it's rejected because (1) hashes are already reachable via `get_prompt()` endpoint **given knowledge of the hash**, and (2) in the default topology where operator and admin share the same trust boundary, no additional exposure occurs — the operator can already read the content directly. However, the flag is deliberately rejected, not optional, to prevent operators from deploying with a false understanding that scrubbing the UI (without isolating the network) protects data.

**Rejected alternative: on-demand fetch.** The existing `/admin/prompts/{hash}` endpoint (`admin.py:66-67,172-173`) already serves content by hash, so the drawer could fetch `system_prompt_content`/`tools_content`/`system_prompt_sanitized_content` lazily on demand instead of via JOIN, avoiding the `db.py:get_request()` schema change entirely. Rejected because it costs up to three additional HTTP round trips per drawer open (one per hash) and requires per-field loading-state handling in `RequestDetailDrawer.tsx`, for no security benefit — both approaches expose the same content under the same trust boundary described above. The JOIN approach returns all needed content in the single existing detail-fetch call.

**Precondition and failure modes:** The sanitizer writes both pre-strip and post-strip content to `prompt_store` via `db.py:record_request(..., prompt_store_entries={...})` at line 285 in the normal path — so `system_prompt_sanitized_sha256` has a corresponding `prompt_store` row when non-NULL. However, if `_extract_prompt_capture()` fails (handlers.py:872 returns `{}`), the `prompt_store_entries` key is absent and the sanitized-content write is silently skipped while the SHA is still written (handlers.py:662). In that failure case, the LEFT JOIN returns NULL content, which is acceptable — the UI renders it as absent, the same as any data-integrity failure. `system_prompt_sha256` and `tools_sha256` are written to `prompt_store` on every recorded request via `record_request()`; if a row is absent for a non-NULL hash (data-integrity failure), LEFT JOIN returns NULL and the UI renders it as absent — the same degraded behavior. Operators investigating missing content should query `prompt_store` directly by hash.

**Changes required:**
- `anthrouter/db.py:get_request()`: rewrite to use an explicit `SELECT` column list (currently `SELECT *` at line 369) to avoid column-name collisions when adding three aliased **LEFT** JOINs to `prompt_store`:
  ```sql
  SELECT r.*, ps_sys.content AS system_prompt_content, ps_tools.content AS tools_content, ps_san.content AS system_prompt_sanitized_content
  FROM requests r
  LEFT JOIN prompt_store ps_sys  ON ps_sys.content_hash  = r.system_prompt_sha256
  LEFT JOIN prompt_store ps_tools ON ps_tools.content_hash = r.tools_sha256
  LEFT JOIN prompt_store ps_san  ON ps_san.content_hash  = r.system_prompt_sanitized_sha256
  ```
  Nullable columns require LEFT JOIN — a plain INNER JOIN would suppress rows with no system prompt or tools. The third JOIN handles cases where the sanitizer ran and removed blocks — `system_prompt_sha256` and `system_prompt_sanitized_sha256` differ (see `db.py:478-479` for the query identifying these rows). Rendering only the pre-strip content without labeling it as such would mislead operators debugging prompt-cache behavior (the upstream sees the sanitized version, not the original).
- `anthrouter/admin.py:_get_request_detail()`: no changes required. The aliased columns from the JOIN flow through `get_request()` into the returned dict automatically — the current code at admin.py:98-101 passes the full request dict through unchanged and already includes the new fields.
- `anthrouter/ui/src/components/RequestDetailDrawer.tsx`: render:
  - `system_prompt_content`: labeled "System prompt (original)" — always shown when present.
  - `system_prompt_sanitized_content`: labeled "System prompt (sanitized, sent upstream)" — shown only when **both** are present **and** the request row's `system_prompt_sha256` differs from `system_prompt_sanitized_sha256`. Compare the hash values directly (already available in the request row), not the content strings, to avoid large string comparisons in the browser and to guarantee correctness with the DB's own deduplication boundary.
  - `tools_content`: labeled "Tools" — always shown when present.

## Consequences

Anyone granted admin UI access can read full historical system prompts and tool schemas for every recorded request. Operators who expose the admin UI beyond loopback already receive an explicit security warning at startup; scoping that risk is their responsibility via `--host` and network controls, not via a scrub flag.
