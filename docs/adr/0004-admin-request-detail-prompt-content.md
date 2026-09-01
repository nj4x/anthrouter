---
artifact-type: adr
---

# ADR-0004: Admin request-detail panel exposes full prompt/tools content unconditionally

## Context

`admin.py`'s `_get_request_detail()` returns only `system_prompt_sha256`/`tools_sha256` hashes; `RequestDetailDrawer.tsx` renders no prompt or tool content, unlike the predecessor project `anthproxy`, whose `admin.py`/`db.py` JOIN `prompt_store` to return full content. A prior research pass flagged this as a privacy consideration for anyone with admin UI access and sketched a possible `--admin-scrub-prompt-content` opt-out flag, but left the tier choice open.

## Decision

Ship the `prompt_store` JOIN unconditionally, matching `anthproxy`'s behavior, with no scrub flag. The trust equivalence claim applies under the default topology: `--host` defaults to `127.0.0.1` (`config.py:112`); `server.py:51-56` already logs a `SECURITY` warning when `--enable-ui` is active and the server is bound to a non-loopback address ("admin UI has no authentication — any host on this network can read conversation history"). Under that default, admin API access and direct SQLite read access share the same trust boundary — `prompt_store` already holds this content, unencrypted, reachable via the existing `get_prompt()` endpoint for any known hash — so a display-layer scrub flag would not reduce actual exposure, only UI convenience. Rejected alternative: an `--admin-scrub-prompt-content` flag — rejected because the non-loopback case already carries an explicit security warning directing operators to bind to loopback; adding a scrub flag alongside that warning creates a false sense that scrubbing the UI is a sufficient substitute for network access control.

Changes required:
- `anthrouter/db.py:get_request()`: add two aliased **LEFT** JOINs to `prompt_store` (nullable columns require LEFT JOIN — a plain INNER JOIN would suppress rows with no system prompt or tools):
  ```sql
  LEFT JOIN prompt_store ps_sys  ON ps_sys.content_hash  = r.system_prompt_sha256
  LEFT JOIN prompt_store ps_tools ON ps_tools.content_hash = r.tools_sha256
  LEFT JOIN prompt_store ps_san  ON ps_san.content_hash  = r.system_prompt_sanitized_sha256
  ```
  Return `ps_sys.content AS system_prompt_content`, `ps_tools.content AS tools_content`, and `ps_san.content AS system_prompt_sanitized_content`. The third JOIN is needed because when the sanitizer stripped blocks, `system_prompt_sha256` and `system_prompt_sanitized_sha256` differ (`db.py:479`); returning only the pre-strip content without labeling it as such would mislead operators debugging prompt-cache behavior.
- `anthrouter/admin.py:_get_request_detail()`: pass through the three content fields.
- `anthrouter/ui/src/components/RequestDetailDrawer.tsx`: render `system_prompt_content` (labeled "System prompt (original)"), `system_prompt_sanitized_content` (labeled "System prompt (sanitized, sent upstream)" — shown only when it differs from the original), and `tools_content`.

## Consequences

Anyone granted admin UI access can read full historical system prompts and tool schemas for every recorded request. Operators who expose the admin UI beyond loopback already receive an explicit security warning at startup; scoping that risk is their responsibility via `--host` and network controls, not via a scrub flag.
