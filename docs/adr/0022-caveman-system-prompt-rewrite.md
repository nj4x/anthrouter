---
artifact-type: adr
lineage-rules: root
---

**Status**: Accepted

# Rewrite contradicting communication guidance for caveman-mode requests

Ported from anthproxy ADR-0053 as of anthproxy `fa72858` (net diff `29ca778^..fa72858`, Decisions 1-12). Upstream amended that ADR three times in one day; later upstream changes are not included here.

## Context

caveman-kit injects a terse-output ruleset through a SessionStart hook and a per-turn reminder. The opening turn carries the header `CAVEMAN MODE ACTIVE — level: <level>`. Its first rule, "Drop articles, copulas, connectives", is mostly not followed. The likely cause is the Claude Code prompt: it is far larger than the ruleset and carries its own communication and tone guidance, which asks for full sentences. Detection must use the request content the proxy sees.

## Decision

1. **Detection.** `caveman_rewrite.detect_mode` matches `CAVEMAN MODE ACTIVE — level: (\w+)` in the opening turn: every `role:'user'` or `role:'system'` message before the first assistant message. No match: the stage does nothing.
2. **Stage order and suppression.** `_rewrite_caveman_system` runs in `ProxyRequestHandler._prepare` after `sanitize_system_prompt`, on the system prompt and on the opening turn's inline `role:'system'` messages as dispatched. It is skipped for security-monitor requests (ADR-0017), which are also left unsanitized. anthrouter has one backend and no chained hops, so upstream's peer-bound suppression does not apply. The stage never raises: any fault dispatches the prompt unchanged. The volatility tracker (ADR-0014) runs inside the sanitizer and therefore sees the pre-rewrite prompt. The transport's inline-system retry runs after this stage and moves already-rewritten text.
3. **Edit ops, not a new prompt.** The model (`--caveman-rewrite-model`, default `claude-sonnet-5-5`) sees only the communication sections and returns a JSON array of `{find, replace}` ops. The call carries `CLASSIFIER_SENTINEL_KEY` (stripped by `build_body`) and goes through `send_classifier_message`, falling back to `send_message`, on `AnthropicTransport`.
4. **Client credentials pay for the editor call.** anthrouter holds no credential of its own, so the editor call is sent with the credentials the client sent on that request, exactly as the classifier is. It is billed to the client's key. The stage still defaults to `on` (decided 2026-11-01), because it acts only on requests carrying the caveman header and later requests hit the cache.
5. **Validation.** A communication section is a markdown heading whose title matches the tone/style/output keyword set, through to the next heading of the same or higher level. The ops are accepted only if every op meets all of these:
   - Its `find` is a non-empty string that occurs exactly once in its block and in no other block.
   - Its `replace` is a string.
   - It lies wholly inside one communication section and touches no heading line.
   - It does not overlap another op.
   - It does not overlap a caveman ruleset section (heading matching `caveman`, `ultracave` or `wenyan`), even inside an enclosing communication section.

   If any op fails, the whole set is rejected and P is dispatched. Text outside those sections survives byte for byte.
6. **Cache.** `P -> P'` is keyed by `sha256(P + "\0" + mode + "\0" + EDITOR_VERSION)`, where `EDITOR_VERSION` is the first 12 hex digits of the editor prompt's SHA-256. It is held in memory and persisted one JSON file per key under `--caveman-rewrite-dir` (default `<anthrouter_home>/rewrites`, created `0700`; files are `0600`), written atomically, so P' is byte-stable across restarts. P' maps to itself, so a retry carrying P' is a hit. A prompt with no communication section is cached as itself. A disk load touches the file's mtime; files unused for 30 days are deleted when a `RewriteCache` is constructed.
7. **Failure policy.** On a model error or timeout (`--caveman-rewrite-timeout`, default 30 s), or when no worker is free (`busy`), P is dispatched and nothing is cached.
   - Invalid ops (`rejected`) are remembered in memory only, for 600 s.
   - Calls are single-flight per key: concurrent first sightings make one model call and dispatch the same P'.
   - At most 4 model calls run at once; calls beyond that are refused, not queued.
8. **Two targets.** Claude Code carries its output style and the caveman ruleset in the opening turn's inline `role:'system'` message, not in `system`. `opening_system_blocks` collects the text blocks of every `role:'system'` message before the first assistant message; they go through the same rewriter and cache, keyed separately. `write_opening_system_blocks` writes the edited text back in place; non-text blocks are kept. Messages are rewritten before `system`.
9. **Editor prompt precedence.** A precedence clause such as "these rules win" is narrowed so the caveman ruleset wins on register. A completeness rule keeps its content demand, gains "in fragments", and is never deleted.
10. **Recording.** The rewritten `system` hash goes to its own column, `requests.system_prompt_caveman_sha256` (schema v5), with the content stored in `prompt_store` as `content_type='system'`, which the existing CHECK allows, so the table is not rebuilt. `system_prompt_sanitized_sha256` keeps meaning sanitizer output and `system_prompt_sha256` keeps the client's prompt. NULL means the stage did not change the dispatched `system`; a messages-only rewrite leaves it NULL and shows in the `target=messages` log line. anthrouter derives prompt capture once per request in `_prepare`, so upstream's retry stash/restore is not ported. Migration v4→v5 recreates both `prompt_store` ref-count triggers to count the new column; upstream's `NOT EXISTS` prune fix and its indexes do not apply to anthrouter's ref-count design.
11. **Observability.** Each caveman request logs `Caveman rewrite: mode= target= outcome= edits= dispatched=` once per target. Each response logs its prompt-cache read ratio and its prose rate (articles and copulas per 100 words, code excluded) next to the session's running value for the other state (original or rewritten). Both are in memory only and advisory; nothing dispatch-affecting reads them.
12. **Config surface.** `caveman_rewrite` and `caveman_rewrite_model` are live-editable through the admin config API. `caveman_rewrite_timeout` and `caveman_rewrite_dir` are CLI/env only: they key the process-wide rewriter instance.

## Consequences

- The first caveman request for a new prompt can wait up to the timeout before dispatch. Later requests hit the cache.
- The inline system message holds per-session text, so each new session is one cache miss and one billed editor call for the `messages` target.
- If the client's credential cannot use the rewrite model, every uncached caveman request logs outcome `error` and P is dispatched. Set `--caveman-rewrite off` or choose another model.
- Each `EDITOR_VERSION` change costs one editor call per distinct prompt.
- The UI does not show the caveman hash column.
