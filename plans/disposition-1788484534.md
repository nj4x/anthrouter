# Disposition Map: CLAUDE.md Refactoring (1788484534)

Refactored `/Users/r.herasymenk/workspace/anthrouter/CLAUDE.md` on 2025-09-03 per `/refactor-claude-md` skill.

## Stale claims fixed

| ID | Claim | Evidence | Action |
|----|-------|----------|--------|
| S-1 | "`admin.py` exposes only `handle_get` — read-only by design" | `admin.py:157` defines `handle_post_config()`. ADR-0005 documents `POST /admin/config` as deliberate scoped exception. | **Rewritten** to: "`admin.py` has two entry points: `handle_get` (read-only, always enabled) and `handle_post_config` (live config edits, enabled only when `ANTHROUTER_ADMIN_TOKEN` is set — see ADR-0005)." The same stale "read-only admin UI" phrasing also appeared in the root `CLAUDE.md` summary paragraph and in `CONTEXT.md:3`; both were corrected after critic pass 1 flagged them. |
| S-2 | "`CONTEXT.md` (not yet created)" | `CONTEXT.md` exists, 1.9KB, added in commit `0daaa3a`. | **Parenthetical dropped**. |

## Instructions: Essential vs. Moved

| Summary | Disposition | Destination |
|---------|-----------|-------------|
| "What this is" — proxy identity | ESSENTIAL | root CLAUDE.md |
| `make test` | ESSENTIAL | root CLAUDE.md |
| `make lint` | ESSENTIAL | root CLAUDE.md |
| `make ui-test` | MOVED | docs/agents/build-and-test.md |
| `make ui-build` | MOVED | docs/agents/build-and-test.md |
| Single-test invocation pattern | MOVED | docs/agents/build-and-test.md |
| `dist/` checked in, rebuild, CI | ESSENTIAL | root CLAUDE.md |
| `npm run dev` proxies /admin | MOVED | docs/agents/build-and-test.md |
| Run proxy locally (`python -m`) | ESSENTIAL | root CLAUDE.md |
| Request path walk + no retry | ESSENTIAL (1-line outline, detail moved) | root CLAUDE.md + docs/agents/architecture.md |
| Handler-level retry details | MOVED | docs/agents/architecture.md |
| `ProxyRequestHandler` reuse + state | MOVED | docs/agents/architecture.md |
| Routing precedence (4 layers) | ESSENTIAL (outline only, detail moved) | root CLAUDE.md + docs/agents/architecture.md |
| Classifier modes (3 options) | MOVED | docs/agents/architecture.md |
| `ReasonCode` stable literal | ESSENTIAL | root CLAUDE.md |
| Classifier sentinel + recursion guard | MOVED | docs/agents/architecture.md |
| `SessionState` bounded/LRU/key shapes | MOVED | docs/agents/architecture.md |
| Routing fails closed | ESSENTIAL | root CLAUDE.md |
| Sanitizer modes/hash semantics | MOVED | docs/agents/architecture.md |
| Transport: no credentials + 401 | ESSENTIAL | root CLAUDE.md |
| Model-aware shaping + thinking-block retry | MOVED | docs/agents/architecture.md |
| Persistence: 3 tables + FTS5 + admin (STALE S-1) | MOVED + REWRITTEN | docs/agents/architecture.md |
| Local commands intercepted | MOVED | docs/agents/architecture.md |
| Comments: "why" only | ESSENTIAL | root CLAUDE.md |
| Config flag conventions | ESSENTIAL (after pass 1) | root CLAUDE.md — initially moved to `docs/agents/code-style.md`; that satellite held only this one instruction, so critic pass 1 flagged the indirection as costing more than it saved. Merged back into the root "Code conventions" list and the file deleted. |
| Installer runs only `pip install`, never `npm build` | ESSENTIAL (restored after pass 1) | root CLAUDE.md — dropped in the initial move; critic pass 1 caught the omission and it was restored to the root `dist/` paragraph. |
| Classifier-input privacy + contract | ESSENTIAL | root CLAUDE.md |
| ADR design decision policy | ESSENTIAL | root CLAUDE.md |
| Issue tracker location | MOVED | docs/agents/issue-tracker.md (already exists) |
| Triage label vocabulary | MOVED | docs/agents/triage-labels.md (already exists) |
| Domain docs (STALE S-2) | MOVED + REWRITTEN | docs/agents/domain.md |

## Output structure

- **Root CLAUDE.md**: 12 essential instructions (90 → 55 lines by `wc -l`)
- **New satellites**:
  - `docs/agents/build-and-test.md` — 5 instructions
  - `docs/agents/architecture.md` — 10 instructions + stale S-1 rewritten
  - (`docs/agents/code-style.md` was created with 1 instruction, then merged back into root and deleted after critic pass 1)
  - `docs/agents/domain.md` — updated w/ stale S-2 rewritten + merged from root
- **Existing satellites** (already externalized):
  - `docs/agents/issue-tracker.md` — 1 instruction
  - `docs/agents/triage-labels.md` — 1 instruction

## Backups

- `CLAUDE.md.bak-1788484534`
- `docs/agents.bak-1788484534/` (pre-edit snapshot)

Prune when no longer needed.

## Critic review

Critic should verify:
1. Right essentials (12 selected, none removed that are universal).
2. Right categories (build-and-test, architecture, domain).
3. Stale content actually fixed (S-1 S-2 correctly addressed).
4. No valuable content lost (disposition map shows all source → destination).

See manifest: `plans/refactor-claude-md-review.md`.

This map is a transient audit artifact, not agent guidance — it lives in `plans/`
alongside the review manifest rather than in `docs/agents/`, where every file is
expected to be reachable from the root `CLAUDE.md`. Prune it once the refactor
has settled.
