# ADR-0021 critic manifest

Design Decisions Reached During Grilling. Design decisions reached during grilling on request #16274 (`yes please` routed to Haiku). Implementation is in the working tree, uncommitted. Review the ADR and the code that realizes it.

- docs/adr/0021-classifier-scores-replies-against-prior-response.md

Implementation under review (`git diff`):

- anthrouter/model_router.py — prompt rule in `_CLASSIFIER_SYSTEM` / `_CLASSIFIER_SYSTEM_JSON`, suffix removal, `affirmation_reclassified` branch
- anthrouter/request_text.py — closed-token `is_short_affirmation`
- tests/test_model_router.py — `TestAffirmationTokenSet`, `TestAffirmationReclassify`, updated prior-response tests
- scripts/replay_classifier.py — manual replay script
- AGENTS.md — classifier-input privacy wording, anthproxy ADR citations

Evidence: docs/research/2026-10-29-request-16274-affirmation-trivial.md

---

## Session Ledger

| Role         | Outcome          |
|--------------|------------------|
| orchestrator | —                |
| planner      | complete         |
| critic #1    | revise (major)   |
| critic #2    | revise (major)   |
| critic #3    | approve (minor)  |

## Critic Review

- **Final verdict:** approve
- **Severity:** minor
- **Iterations used:** 3 of ∞
- **Approval status:** ✓ Automatically approved by critic. No manual review required.
- **Risks / questions:** the three minor findings from critic #3 were addressed in the ADR text (Decision 3 stands alone; higher-of compares the uncapped tier; shared `affirmation_inherited` on reclassify failure is deliberate).
