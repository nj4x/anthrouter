---
artifact-type: adr
lineage-rules: root
---

**Status**: Accepted

# Strip the attribution reminder from user messages, by exact opening text

Ported from anthproxy ADR-0054 as of anthproxy `09b5c13` (PR #108) plus the review fixes in anthproxy PR #109 (bounded opener scan, blank-only sibling check).

## Context

Claude Code appends a `<system-reminder>` to user-message content that tells the agent how to attribute commits and pull requests:

```
<system-reminder>
Attribution for git commits and pull requests you create from here on (…):
- End git commit messages with:
Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>
- End pull request descriptions with:
🤖 Generated with [Claude Code](https://claude.com/claude-code)
</system-reminder>
```

The operator does not want those lines in their repositories' history. The reminder is client-injected content, so the proxy is the one place that can remove it for every client and every project. The sanitizer cannot do this: it reads only the top-level `system` field, and this reminder rides inside `role:'user'` content, often in the same text block as other reminders.

The obvious match, "any reminder containing `Co-Authored-By` or `Generated with`", is wrong: Claude Code also delivers the user's own CLAUDE.md and memory inside `<system-reminder>` blocks, so a CLAUDE.md that discusses `Co-Authored-By` would be deleted.

## Decision

1. **Match.** A `<system-reminder>…</system-reminder>` span whose body, after leading whitespace, starts with `Attribution for git commits and pull requests`. The prefix is case-sensitive and a hardcoded constant in `request_text.py`; additions are code changes. Tags are case-insensitive and the opener may carry attributes, scanned for at most 512 characters so a run of unterminated openers stays linear. Keywords inside the body are never matched.
2. **Removal.** The entire span goes, tags included; surrounding text in the same block is kept byte for byte. A span without a closing tag does not match (fail open). A text block left empty is dropped, and its `cache_control` (if any) moves to the preceding block; when there is no preceding block without its own `cache_control`, that block is left as it is. A message the removal would leave with only blank text is left unchanged, since upstream rejects empty user content.
3. **Scope.** Every `role:'user'` message, string and list content alike, top-level text only: `tool_result` content and inline `role:'system'` messages pass through. Every turn, not only the latest, so the cached prefix stays stable between requests. When nothing matches, the original `messages` list is returned unchanged.
4. **Stage.** `_strip_attribution_reminder` in `handlers.py`, called in `ProxyRequestHandler._prepare` after `sanitize_system_prompt` and before `_rewrite_caveman_system`. It never raises: any fault dispatches the payload unchanged. The payload is mutated in place, so the transport's in-connection 429/5xx retries resend the stripped messages.
5. **Suppression.** Skipped for security-monitor requests (ADR-0017), like the sanitizer and caveman rewrite. Upstream's peer-bound skip and its re-application in `_retry_on_new_backend` have no counterpart here: anthrouter has one backend, no peer hop, and no handler-level retry.
6. **Control.** `--strip-attribution-reminder {on,off}`, env `ANTHROUTER_STRIP_ATTRIBUTION_REMINDER`, default `on`, live-editable through the admin config editor. Separate from `--sanitize-system-prompt`, whose `warn` mode and cache rationale do not apply.
7. **Recording.** One INFO log line per request that had a reminder stripped, with the count. No schema change. Prompt hashes are unaffected, since this stage does not touch `system`.

## Consequences

- Commits and PRs made through the proxy stop carrying `Co-Authored-By` and `Generated with` lines to the extent this reminder was the source. Attribution guidance elsewhere (the `system` prompt, the user's CLAUDE.md) is not touched.
- A reworded reminder escapes the strip silently until the constant is updated: inert rather than destructive. The INFO line's absence is the only signal.
- Not a security boundary; it removes one injection point.
- Because the default is `on`, every session in progress when this ships takes one cache miss on its next request.
- In the chained topology (caveman in front, anthrouter dispatching), anthrouter is the innermost hop and strips once.

## Considered Options

- **Keyword match.** Rejected: deletes operator instructions that mention those strings.
- **Remove only the offending lines.** Rejected: leaves an empty "attribution guidance" reminder and tracks line wording.
- **Extend the sanitizer.** Rejected: different field, different modes, policy rather than cache cost.
- **Latest turn only.** Rejected: partial stripping changes the cached prefix between turns.

## Amendment (2026-11-03): recording

Decision 7 ("No schema change") is amended. ADR-0024 added `requests.user_prompt_dispatched_text`, the last user message as dispatched. ADR-0030 adds `requests.attribution_reminders_stripped`, the strip count across all user turns. The INFO log line stays.
