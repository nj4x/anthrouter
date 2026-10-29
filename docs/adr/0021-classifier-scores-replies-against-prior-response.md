---
artifact-type: adr
lineage-rules: root
---

**Status**: Accepted

# Classifier scores short replies against the prior response

A short user reply such as `yes please` or `Q1 ok, Q2 no` carries no complexity on its own. Its meaning and difficulty come from the assistant message it answers. Request #16274 (`yes please`, session `3342fbf3`) routed to Haiku. The turn that followed wrote four interview-plan files, which is not trivial work.

#16274 took the main classifier path. The classifier had received the prior assistant text (`prior_response_summary`, ending "Ready to draft?"), but both classifier system prompts told it to judge complexity "ONLY from the user's intent in final_user_text" and never to escalate because of other fields. The "use prior_response_summary" suffix was appended only when `auto_model_routing_confidence_bump` was on, so the default configuration never used it. The score was 8, the result was written to the session tier cache, and the eleven drafting turns that followed replayed Haiku through the walk-back cache.

A second gap sits behind the first. `is_short_affirmation` did not match `yes please`, because only a leading "please" was dropped. Had it matched, the affirmation path would have inherited the cached tier from #16273 (an answer to a question list, score 18, Haiku) without a classifier call. The result would have been Haiku either way.

Evidence: `docs/research/2026-10-29-request-16274-affirmation-trivial.md`.

## Decision

1. **Prompt rule.** `_CLASSIFIER_SYSTEM` and `_CLASSIFIER_SYSTEM_JSON` state one rule for both formats. Without `prior_response_summary`, score `final_user_text` alone, as now. With it, when `final_user_text` approves, answers or chooses among what the prior response proposed, score the work being approved. Examples are added to both prompts:
   - `"yes please"`, prior "…Ready to draft the 4 files?" → 50
   - `"Q1 ok, Q2 no"`, prior list of design questions → 45
   - `"thanks, that works"`, prior closing summary → 5
   - `"yes"`, prior "Want me to fix the typo?" → 10

   The `use_json` gate on the prior-context suffix is removed. `_CLASSIFIER_SYSTEM_JSON_PRIOR_SUFFIX` is deleted, because the rule now sits in the base prompts. This applies to both the main classifier and the affirmation classifier, and to every reply that has a prior assistant message, not only to closed-set affirmations. The routing precedence is unchanged: the long-context size floor and the walk-back cache still decide before any classifier call.

   Decision 1 alone fixes #16274.

2. **Wider affirmation detection.** `is_short_affirmation` also matches text of at most 4 tokens where every token is in a closed set (`yes, yep, yeah, ok, okay, sure, please, go, ahead, do, it, continue, proceed, start`) and at least one token is an anchor that carries assent alone (`yes, yep, yeah, ok, okay, sure, go, continue, proceed`). Each token is stripped of edge punctuation, so `yes, please` matches. The anchor rule rejects filler-only text such as `it`, `do`, `ahead`, `please please` and a bare `start`, as the existing docstring requires. Phrases such as `do it anyway` and `yes park here` still reach the main classifier.

   Decision 3 also stands on its own: phrases that already matched before this ADR, such as `yes` or `go ahead`, inherit a trivial cache with no call, and Decision 1 does not reach that path.

   Decision 2 saves a classifier call when the cache holds a standard or deep tier. It must ship with Decision 3: without it, an approval that now matches would inherit a trivial cache with no call and replay the #16274 failure.

3. **Re-score a trivial cached tier.** "Trivial tier" means the model configured for the `trivial` label in `auto_model_routing_classification`. On an affirmation turn whose cached tier equals that model and whose `prior_response_summary` is non-empty, call the classifier as the no-cache branch does, including the weighted system-prompt blend when routing mode is `classifier`. The higher-of rule compares the cached tier with the new uncapped resolved tier, the value the cache stores. The routed model then keeps the no-cache branch's baseline cap. In the default label map the trivial model is the lowest tier, so the higher-of rule routes at the new result. It is a guard for custom label maps, where the trivial model can share a model with, or rank above, another label. The row records the new classifier label. The cache gets the uncapped resolved tier, as on the no-cache branch (or the cached tier when higher-of keeps it). The reason code is `affirmation_reclassified` (added to `ReasonCode`). If the reclassify call fails, keep the cached tier with `affirmation_inherited`. The shared reason code is deliberate: the outcome is the same as an inherit, and the failed call is still visible in the log line for the classifier call. Standard and deep cached tiers still inherit with no call.

   ```mermaid
   flowchart TD
     A[Affirmation turn] --> B{Cached tier?}
     B -- none --> C{Classifier call succeeds?}
     C -- yes --> C1[Route at result<br/>affirmation_classified]
     C -- no --> C2[Standard floor, no cache write<br/>affirmation_classifier_failed]
     B -- standard or deep --> D[Inherit, no call<br/>affirmation_inherited]
     B -- trivial --> E{prior_response_summary non-empty?}
     E -- no --> D
     E -- yes --> F{Classifier call succeeds?}
     F -- no --> G[Keep cached tier<br/>affirmation_inherited]
     F -- yes --> H[Route at higher of cached and new tier<br/>write cache, affirmation_reclassified]
   ```

   The two failure policies differ on purpose. With no cache there is no earlier result, so the branch fails closed to the standard floor, as before. With a trivial cache an earlier classifier result exists, so the branch keeps it rather than guess.

## Alternatives considered

- **Re-score every cached tier on an affirmation.** Rejected for cost. A deep cache cannot move up, and higher-of would keep it. A standard cache can move up to deep when the prior response proposes larger work than the cached task. That under-routing is accepted: standard is a working tier, and a call on every approval in a standard session is the larger cost. Revisit if replay data shows frequent standard-to-deep approvals.
- **Always call the classifier on affirmations, never inherit.** Rejected. It adds one call to every approval turn, and the cache is sound on most of them.
- **Open-ended token rule (any short text that starts with an approval word).** Rejected. Short replies such as `yes park here` and `do it anyway` carry new content and must reach the main classifier.
- **Keep the prior-context rule gated on `confidence_bump`.** Rejected. The flag defaults to False, so the rule would stay off in the default configuration, which is how #16274 happened.

## Consequences

- **Privacy unchanged.** `prior_response_summary` is already in `to_classifier_json()` output. No field is added. `AGENTS.md` § Classifier-input privacy is updated to name it as bounded prior assistant text.
- **Scores move on every turn with a prior assistant message.** Before release, run `scripts/replay_classifier.py` against the local DB. It replays stored `classifier_summary_json` rows through `_CLASSIFIER_SYSTEM`, the default format; the JSON format is not replayed. Release criterion: #16274 scores at least 38 (standard). The maintainer reviews rows that move up a tier and are not replies to a proposal. The script is manual and is not run in CI.
- **Extra classifier calls** on affirmation turns whose cached tier is trivial: one user-prompt call, plus one system-prompt call when the blend runs and its cache misses. A re-score that returns trivial writes trivial again, so in a session that is trivial throughout, every later approval repeats the call. That cost is accepted: those turns use the cheapest classifier, and a stale trivial tier is the failure this ADR removes.
- **Reverses anthproxy's choice.** anthproxy ADR-0013 asked for the prompt to prioritise `prior_response_summary`. Its spec (`prior-response-affirmation-enrichment.md:80`) then gated it on `confidence_bump`. This ADR keeps the ADR-0013 intent. anthproxy is not changed here.
- **Architecture doc.** `docs/agents/architecture.md` (routing layer 3, Affirmation inheritance) names the trivial-tier exception.
- **Citation fix.** The "ADR 0010/0012" references for the weighted blend in `model_router.py` and `AGENTS.md` point to anthproxy ADRs. They are corrected to name anthproxy.
