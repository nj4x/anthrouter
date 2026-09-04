# Critic review: CLAUDE.md refactoring

Reviewed as **Design Decisions Reached During Grilling** — this manifest lists files, and the critic reads each one as the artifact under review. The files are ground truth; this manifest is only a pointer list.

## What was done

`CLAUDE.md` was refactored for progressive disclosure: a minimal root holding only universal instructions, plus intent-grouped satellite files under `docs/agents/`. 30 instructions were extracted from the original, grounded against the codebase, and dispositioned as essential (kept in root), moved (to a satellite), or rewritten (stale claim corrected).

Two stale claims were found by codebase grounding and corrected with the user's approval:

- **S-1** — the original claimed "`admin.py` exposes only `handle_get` — read-only by design; with one backend there is no runtime control to expose via POST." This is false: `admin.py:157` defines `handle_post_config()`, and `POST /admin/config` is live, gated by `ANTHROUTER_ADMIN_TOKEN`, documented in `docs/adr/0005-runtime-config-editor.md:28-34`.
- **S-2** — the original said root `CONTEXT.md` was "(not yet created)". It exists (1.9 KB, added in commit `0daaa3a`).

## What the critic should assess

1. **Essentials** — are the 12 instructions kept in the root genuinely universal (apply to more than half of tasks, or are foundational constraints)? Was anything essential demoted to a satellite where an agent would miss it?
2. **Categories** — did each moved instruction land in the right satellite (`build-and-test`, `architecture`, `domain`)?
3. **Staleness fixes** — do the S-1 and S-2 rewrites accurately match the current code and ADR-0005? Check the rewritten text in `docs/agents/architecture.md` against the cited source.
4. **Content preservation** — compare the pre-refactor backup against the new root plus satellites. Does the disposition map account for every instruction, and did any technical content get dropped or distorted in the move?
5. **Reference integrity** — does every satellite have an inbound `See ...` reference from the root, and does every path referenced in the root actually exist?

## Files under review

- CLAUDE.md
- CONTEXT.md
- README.md
- docs/agents/build-and-test.md
- docs/agents/architecture.md
- docs/agents/domain.md
- plans/disposition-1788484534.md
- CLAUDE.md.bak-1788484534
- docs/agents.bak-1788484534/domain.md

## Changes applied after critic pass 1

Pass 1 returned revise/major. All findings were addressed:

- **[major]** Root `CLAUDE.md` "What this is" and `CONTEXT.md` line 3 both still described a "read-only admin UI", contradicting the S-1 fix in `architecture.md`. Both now state the UI is read-only by default with `POST /admin/config` as the sole write endpoint gated by `ANTHROUTER_ADMIN_TOKEN` (ADR-0005).
- **[major]** `docs/agents/domain.md` had a literal duplicate `CONTEXT-MAP.md` bullet introduced during the refactor merge. Duplicate removed.
- **[minor]** The `dist/` constraint was verbatim-duplicated in root and `build-and-test.md`. Root is now authoritative; the satellite cross-references it.
- **[minor]** The installer constraint ("the installer only ever runs `pip install`, never `npm build`") had been dropped in the move. Restored in the root `dist/` paragraph.
- **[minor]** `python -m anthrouter` appeared twice in root. "Core commands" and "Running the proxy" are collapsed into one section.
- **[minor]** `docs/agents/code-style.md` held a single instruction. Merged into the root "Code conventions" list as a third bullet; the file is deleted and its inbound reference removed.
- **[minor]** The classifier-input privacy contract was reproduced in full in both root and `architecture.md`. Root is authoritative; the satellite cross-references it and keeps only the sentinel mechanics.

## Changes applied after critic pass 2

Pass 2 verified all nine pass-1 fixes landed, but found the stale "read-only" claim had a third home that pass 1 missed:

- **[major]** `README.md` lines 66, 89 and 92 still described the admin API as unconditionally read-only, with line 92 asserting "There are no runtime controls — with one backend there is nothing to switch." Rewritten to match ADR-0005: every GET is read-only, `POST /admin/config` is the sole write endpoint gated by `X-Admin-Token`, and the surface is fully read-only when no `ANTHROUTER_ADMIN_TOKEN` is set.
- **[minor]** `docs/research/2026-08-31-anthproxy-vs-anthrouter-comparison.md` carries the same now-false claim. It is a dated research artifact, so it keeps its original text under a header note marking that one claim stale as of ADR-0005.
- **[minor]** The disposition map sat in `docs/agents/` with no inbound reference from the root, violating the reference-integrity rule it was itself auditing. It is a transient audit artifact, so it moved to `plans/` alongside this manifest.
- **[minor]** The disposition map's line-count metadata was wrong. Corrected to the measured values (90 → 55 lines by `wc -l`).
