# Thread routing ADR manifest — Design Decisions Reached During Grilling

Design decisions reached during grilling of the 2026-11-02 Thread-error investigations (sessions `9a0efeeb` and `84fe251b`). Anthropic released message threads in beta that day. anthrouter re-routed Thread continuations to a different model (upstream 400), and it rewrote the upstream 404 for an expired Thread as a retryable 502.

- docs/adr/0027-pin-thread-continuation-model.md
- docs/adr/0028-pass-through-upstream-4xx.md

Also changed: `GLOSSARY.md` (Thread, Thread continuation).

---

## Session Ledger

| Role         | Outcome           |
|--------------|-------------------|
| orchestrator | —                 |
| planner      | complete          |
| critic #1    | revise (major)    |
| critic #2    | approve (minor)   |

## Critic Review

- **Final verdict:** approve
- **Severity:** minor
- **Iterations used:** 2 of ∞ (backstop 10)
- **Approval status:** ✓ Automatically approved by critic. No manual review required.
- **Risks / questions:** All six critic #2 minor issues were applied after approval: compatibility wording, ADR-0026 request-id context, the reason for the 408/409 classifier change, softening "harmless" for stale entries, the reason for detecting future Thread types, and a Considered Options section in ADR-0028.
