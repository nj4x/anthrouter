# Porting anthproxy changes after 29ca778 into anthrouter

**Date**: 2026-11-01
**Source**: `/Users/roman/projects/anthproxy`, `git log 29ca778..HEAD`
**Target**: `/Users/roman/projects/anthrouter` at `13efa8b`

## Commit range and whether 29ca778 is included

```
fa72858 Tighten caveman editor prompt and add prose-rate log (ADR-0053 Decisions 10-12) (#107)
0fbe646 Re-land caveman rewrite (ADR-0053): fix prune stall, detect inline system header, rewrite output style (#106)
b786efc Revert "Rewrite contradicting communication guidance ... (ADR-0053) (#105)"
29ca778 (base, exclusive) Rewrite contradicting communication guidance for caveman-mode requests (ADR-0053) (#105)
```

Port 29ca778 too. Commit b786efc reverts it, and 0fbe646 adds it back with fixes. With 29ca778 left out, the range applies on top of a tree that never had it: the revert undoes nothing, and 0fbe646 alone does not hold the whole feature. Port the net diff `git diff 29ca778^..HEAD` as one feature. Do not cherry-pick the commits one by one: 3 commits, 1 feature, ADR-0053 Decisions 1-12.

Net diff, 13 files, +1424/-21: `anthproxy/caveman_rewrite.py` (new, 541 lines), `config.py`, `db.py`, `handlers.py`, `store.py`, `README.md`, `docs/adr/0053-...md`, `docs/agents/architecture-boundaries.md`, `docs/agents/credentials-and-errors.md`, `tests/test_caveman_rewrite.py` (new, 585 lines), `tests/test_classifier_failure_diagnostics.py`, `tests/test_db.py`, `tests/test_record_bodies.py`.

## Feature: caveman-mode system-prompt rewrite (ADR-0053)

### What it does in anthproxy

- **Detection** (`caveman_rewrite.detect_mode`): regex `CAVEMAN MODE ACTIVE — level: (\w+)`, applied to every user and inline `role:'system'` message before the first assistant message.
- **Stage** `_rewrite_caveman_system` (handlers.py) runs in `prepare_routing` right after `_sanitize_system_prompt`. It has 2 targets: the `system` field, and the inline system blocks of the opening turn (`opening_system_blocks` / `write_opening_system_blocks`, Decision 9). It is skipped for peer-bound requests and for security-monitor requests. It never raises.
- **Editor model call**: `send_classifier_message`, falling back to `send_message`, on the active backend. Payload: `--caveman-rewrite-model`, `_MAX_TOKENS`, the editor system prompt, and the sentinel `_anthproxy_internal_classifier`. The model returns `{find, replace}` ops, and each op must stay inside one communication section (Decision 4: unique `find`, no heading touched, no overlap). Any failed op rejects the whole set.
- **`CavemanRewriter`**:
  - Single-flight per key.
  - At most 4 concurrent calls. Calls beyond that are refused (`busy`), not queued.
  - Timeout set by `--caveman-rewrite-timeout`.
  - A `rejected` result is remembered in memory for 600 s.
- **`RewriteCache`**: key is `sha256(P \0 mode \0 EDITOR_VERSION)` (Decision 11). Memory, plus one atomic JSON file per key under `--caveman-rewrite-dir`.
- **Observability**:
  - One `Caveman rewrite: mode= target= outcome= edits= dispatched=` log line per target.
  - Prompt-cache read ratio per session state, rewritten vs original (`RATIO_TRACKER`, hooked into `_record_session_context`).
  - Prose rate: articles and copulas per 100 words (`prose_rate`, `PROSE_TRACKER`, `_record_caveman_prose`, called from both the JSON and SSE completion paths, Decision 12).
- **DB**:
  - Migration v16->v17 adds `requests.system_prompt_caveman_sha256`.
  - Migration v17->v18 adds partial indexes on the sanitized and caveman hash columns.
  - The `prompt_store` prune becomes `_PRUNE_PROMPT_STORE_SQL`: one `NOT EXISTS` per column. This fixes the prune stall that caused the revert.
  - `_restore_sanitized_hashes` restores each stage's own stashed hash.
- **Config** (anthproxy `config.py`): `caveman_rewrite` (`on|off`, default `on`), `caveman_rewrite_model` (default `claude-sonnet-5-5`), `caveman_rewrite_timeout` (default 30.0), `caveman_rewrite_dir` (default `''`, which resolves to `<home>/rewrites`).

### Matching code in anthrouter

| anthproxy | anthrouter | Status |
|---|---|---|
| `caveman_rewrite.py` (stdlib only, no `anthproxy` imports) | none | **Applicable as-is.** Copy it to `anthrouter/caveman_rewrite.py`. Change the sentinel literal to `CLASSIFIER_SENTINEL_KEY` from `anthrouter/mapper` (as used in `anthrouter/model_router.py:58,894`). |
| `prepare_routing` + `_sanitize_system_prompt` | `ProxyRequestHandler._prepare`, `anthrouter/handlers.py:879-935` (the sanitizer is called at `:901`) | **Needs adaptation.** anthrouter has no `snapshot`, `PreparedRequest` or peer. Call the stage inside `_prepare` after the sanitizer block, and skip it when `monitor` is true (same rule as `:901`, ADR-0017). Remove the `snapshot.name == 'peer'` check: anthrouter has one backend and no chained hops. |
| `snapshot.backend.send_classifier_message` | `self.transport` (`AnthropicTransport`, `anthrouter/transport.py:219 send_message`). model_router already uses `getattr(target.backend, 'send_classifier_message', target.backend.send_message)` at `model_router.py:952,1520,1975` | **Needs adaptation.** Use the same getattr fallback on `self.transport`. Pass the client `credentials` that `_prepare` already receives (`handlers.py:853`). The editor call spends the client's own key, which is the passthrough rule: never substitute a credential. `build_body` strips the sentinel. Check that `send_message(payload, credentials, config)` matches the signature. |
| `_sanitized_capture` stash + `_restore_sanitized_hashes` (retry restore) | none. anthrouter derives the capture once per request in `_prepare` (`handlers.py:10`, `:900-911`) | **Not applicable as written.** Write `system_prompt_caveman_sha256` and its `prompt_store_entries` row straight into `self._prompt_capture`, as the sanitizer does at `:905-911`. There is no stash/restore to port. |
| `_record_session_context` ratio log | `handlers.py:1248` (signature `(self, stats)`, unlike anthproxy's 4 arguments) | **Needs adaptation.** Take the input, cache-creation and cache-read token counts from `stats`. Reset `self._caveman_state = None` in the per-request reset block near `handlers.py:810-816`. |
| `_record_caveman_prose` in the JSON and SSE completion paths | JSON path near `handlers.py:1057`, SSE path near `:1125` (both call `_record_session_context`) | **Applicable with small changes.** Hook it next to those calls. Find the anthrouter variable that holds the accumulated response text. Record only when the stream did not error. |
| DB migrations v16/v17 and `_PRUNE_PROMPT_STORE_SQL` | `anthrouter/db.py`: `_SCHEMA_VERSION = 4` (`:34`), `_MIGRATIONS` (`:179`). `prompt_store` uses **ref_count triggers** (`:128-147`) and deletes with `ref_count <= 0` (`:576`), not `NOT EXISTS` | **Needs adaptation.** Add `_apply_migration_4`: column `system_prompt_caveman_sha256`, bump to v5. The anthproxy prune fix and its indexes are **not applicable**: anthrouter's ref-count design already avoids the scan. Instead, drop and recreate both `trg_requests_refcount_ai`/`_ad` triggers with `UNION SELECT new/old.system_prompt_caveman_sha256`, otherwise caveman rows leak or get pruned too early. The `content_type` CHECK (`:95`) allows only `'system','system_sanitized','tools'`. Either store the caveman content as `'system_sanitized'`/`'system'`, or rebuild the table to add `'system_caveman'`. Reusing `'system'` is the simplest choice. Pick one and record it in the ADR. Extend `record_request` (`:267-310`) with the new parameter and column. |
| `store.py` (+2) | `prompt_store_entries` handled inside `db.record_request` | Fold this into the db change. |
| Config 4 fields (`ANTHPROXY_CAVEMAN_*`) | `Config` near `config.py:387`, argparse near `:756`, home resolution near `:934` | **Needs adaptation.** Rename env vars to `ANTHROUTER_CAVEMAN_REWRITE`, `_MODEL`, `_TIMEOUT`, `_DIR`. Resolve the default dir to `<anthrouter_home>/rewrites`. anthrouter also has the runtime config editor (ADR-0005/0007): `EDITABLE_FIELDS` (`config.py:34`) and `FIELD_METADATA` (enum validation at `:412-435`). Proposal: make `caveman_rewrite` (enum on/off) and `caveman_rewrite_model` editable. `caveman_rewrite_timeout` and `caveman_rewrite_dir` are keys of the cached rewriter instance. Either make them file-editable (restart required) or exclude them, and say so in the exclusion comment at `config.py:21-32`. The help text should say "opening turn", not "first user message", which is stale in anthproxy. |
| Default `on` | n/a | **Decided (2026-11-01): default `on`**, matching upstream. The editor call is billed to the client's credential; document this cost in the ADR and README. |
| ADR-0053 | anthrouter ADRs run to 0021 | Add **`docs/adr/0022-caveman-system-prompt-rewrite.md`**. Map the cross-references: anthproxy ADR-0029 (sanitizer) to anthrouter's sanitizer docs, ADR-0047 (security monitor) to anthrouter ADR-0017, ADR-0023 (peer) removed. Add a Decision covering client-credential billing and the ref-count trigger change. |
| `docs/agents/architecture-boundaries.md`, `credentials-and-errors.md` | `docs/agents/architecture.md` (request path) | Update the request path in `architecture.md` and in the AGENTS.md "Request path" line (sanitization, then caveman rewrite). Add a `GLOSSARY.md` entry for "caveman rewrite" / "communication section". |
| README (+10) | anthrouter README | Port the flag section with renamed env vars. |
| UI | `anthrouter/ui` | **No `dist` rebuild needed** unless the new hash is shown in the request detail view (upstream does not show it: "The UI does not yet show the caveman hash column"). The config modal reads `FIELD_METADATA`. Check whether `ui/src` hardcodes field lists. If it does, edit it and rebuild with `make ui-build`. |

### Tests to port

- `tests/test_caveman_rewrite.py` (585 lines) to `tests/test_caveman_rewrite.py`. The pure-module tests (detection, op validation, cache, single-flight, busy, rejected TTL, prose rate, EDITOR_VERSION) should port nearly verbatim. The handler and integration tests need rewriting against `ProxyRequestHandler._prepare` and `self.transport` fakes, in the style of `tests/test_handlers.py`. Drop the peer-suppression tests and keep the security-monitor suppression test.
- `tests/test_db.py`: anthproxy changes assert schema v18. anthrouter needs asserts for v5, the new column, and ref_count when the caveman hash equals another hash (UNION dedup) and when it differs.
- `tests/test_record_bodies.py`, `tests/test_classifier_failure_diagnostics.py`: anthrouter has no such files. Port the relevant asserts (an extra `record_request` kwarg / column count) into `tests/test_handlers.py` / `tests/test_db.py` if those tests pin column lists.
- `tests/test_config.py`: 4 flags with env vars, choices validation, dir default, and EDITABLE_FIELDS/FIELD_METADATA consistency if a test covers it.

## Ordered port plan

1. Copy `caveman_rewrite.py` and its pure unit tests. Switch to the anthrouter sentinel constant. Green with no wiring.
2. Config: 4 fields, argparse with `ANTHROUTER_CAVEMAN_*`, dir resolution, EDITABLE_FIELDS/FIELD_METADATA decision. Add config tests.
3. DB: migration v4->v5 (column plus recreated ref_count triggers), `record_request` kwarg, content_type decision. Add db tests.
4. Handler wiring in `_prepare` (monitor skip, transport editor call with client credentials, capture hash), `_caveman_state` reset, ratio and prose logs. Add handler tests.
5. Docs: ADR-0022, architecture.md, AGENTS.md request-path line, GLOSSARY, README.
6. Check whether the UI needs changes. Most likely no `dist` change. Run `make test` and `make lint`.

## Risks

- **Latency**: on a cold miss the first caveman request blocks up to the timeout (30 s) inside `_prepare`. Each new session misses for the `messages` target, because the inline system text holds per-session data.
- **Cost and credentials**: the editor calls are billed to the client's key. With default `on`, users get this spend without opting in. The classifier already does this, but they should be told.
- **Prompt cache**: the rewrite changes P. It is byte-stable thanks to the cache, but the first rewritten turn invalidates the client's prompt cache prefix compared with an unrewritten earlier turn. Disk persistence across restarts is what keeps P' stable. Make sure the rewrites dir is created with sane permissions: it holds system prompt content.
- **Ref-count triggers**: recreating triggers in a migration must be exact. A missed column makes the row leak or get pruned early, which breaks the admin prompt-content view (ADR-0004).
- **Interaction with volatility tracking (ADR-0014)**: the tracker observes the post-strip system. Decide whether it should see pre- or post-caveman text. Pre-caveman is the likely choice, to keep the sanitizer's signal clean. Order the call accordingly.
- **Inline system rewrite (ADR-0010, gated by model)**: anthrouter already rewrites inline system messages in some cases. Check how this stage composes with that existing rewrite of the opening turn's `role:'system'` blocks, and run it after.
- **Upstream still moving**: ADR-0053 got 3 amendments in 1 day. Pin the port to anthproxy `fa72858` and note that sha in ADR-0022.
