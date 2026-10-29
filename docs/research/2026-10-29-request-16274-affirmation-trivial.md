# Request #16274 "yes please" routed to Haiku: classifier input on affirmation turns

**Date**: 2026-10-29
**Status**: Investigation complete. Nothing implemented.
**Symptom**: Request #16274 (2026-10-29T17:38:36Z, session `3342fbf3-…`) recorded `classification='trivial'`, `reason_code='classifier_trivial'`, `routed_model='haiku'`. The user prompt was `yes please`. The turn that followed wrote four interview-plan files, which is not trivial work.
**Scope**: Read-only. `~/.anthrouter/anthrouter.db` and `~/.anthproxy/anthproxy.db` were opened with `?mode=ro`. No code, test, config or database was changed. The `session_id` column holds device and account identifiers; only the session UUID prefix is quoted here. No credential value was read.

Hypothesis under test: "The prompt was an affirmation, and the classifier scored it in isolation, without the previous assistant response."

---

## 1. Verdict

**Hypothesis partly refuted.**

- Not true that the classifier lacked the previous assistant response. The recorded classifier input holds a `prior_response_summary` with the prior assistant text, ending in "Ready to draft?" (DB `requests.id=16274`, column `classifier_summary_json`).
- True that the classifier judged `yes please` as if in isolation. The one-integer system prompt tells it to judge "ONLY from the user's intent in final_user_text" and to never escalate because of the other fields (`anthrouter/model_router.py:261-265`). The sentence that tells it to use `prior_response_summary` is appended only in JSON mode (`anthrouter/model_router.py:1042-1043`). JSON mode was off (`ANTHROUTER_AUTO_MODEL_ROUTING_CONFIDENCE_BUMP="False"` in `~/.anthrouter/config.env`; DB `classifier_format='standard'`).
- `yes please` was not recognised as an affirmation, so the affirmation path did not run. The router took the normal classifier path (log line below; `reason_code='classifier_trivial'`, not `affirmation_*`).
- Even if it had been recognised, the result would have been Haiku. The session tier cache already held `haiku` from the previous turn #16273, and the affirmation path inherits a cached tier without a classifier call (`anthrouter/model_router.py:1855-1872`).

anthproxy has the same code and the same gap. It is the origin of this logic; anthrouter is a port.

---

## 2. Verified facts: the DB rows

### 2.1 Row #16274

Source: `~/.anthrouter/anthrouter.db`, table `requests`, `id=16274`.

| Column | Value |
|---|---|
| `user_prompt_text` | `yes please` |
| `requested_model` | `claude-opus-5-5` |
| `routed_model` / `model_tier` | `haiku` / `haiku` |
| `classification` | `trivial` |
| `reason_code` | `classifier_trivial` |
| `classifier_model` | `haiku` |
| `classifier_format` | `standard` (one-integer prompt, not JSON) |
| `classifier_raw_response` | `8` |
| `user_prompt_score` / `system_prompt_score` / `routing_weighted_score` | `8.0` / `82.0` / `23.0` |
| `output_tokens` | `1022` |

`classifier_summary_json` (verbatim, abbreviated in the middle):

```json
{"final_user_text": "yes please", "text_truncated": false, "total_messages": 41,
 "prior_user_messages": 13, "prior_assistant_messages": 13, "tool_use_count": 13,
 "tool_result_count": 13, "final_non_text_blocks": 0, "has_images": false,
 "prior_response_summary": "Settled. Recording answers:\n\n- Q1 deliverables: 4 files, same as Shuo's.\n- Q2 timing: same 120-min plan, ... - Q9: one neutral motivation question in intro.\n\nReady to draft?"}
```

The blend matches the configured weights: `0.2 × 82 + 0.8 × 8 = 22.8 → 23` (`ANTHROUTER_AUTO_MODEL_ROUTING_SYSTEM_PROMPT_WEIGHT="0.2"`, `..._USER_PROMPT_WEIGHT="0.8"`). 23 is below the trivial threshold `30.0` (`ANTHROUTER_AUTO_MODEL_ROUTING_TRIVIAL_THRESHOLD="30.0"`). So the blend did not rescue it.

### 2.2 The log for #16274

`~/.anthrouter/anthrouter.log:706191-706194` (local time, UTC−7):

```
10:38:28,138 ... Model lock: forcing routing baseline from claude-opus-5-5 to sonnet
10:38:28,140 ... Model router: classifying (requested=sonnet prompt_chars=10 est=0/0 (n/a)) prompt='yes please'
10:38:29,426 ... Model router: score=8 (tier=trivial) (requested=sonnet)
10:38:30,368 ... Model routing: requested=claude-opus-5-5 routed=haiku classification=trivial applied=True reason=classifier_trivial ...
```

The affirmation path logs `affirmation classifying with prior_response_summary` (`anthrouter/model_router.py:1937-1941`). That line is absent. So the main classifier dispatch ran, not the affirmation branch.

### 2.3 Neighbours in the same session

Same `session_id`, ids 16253–16291:

| id | prompt (start) | reason_code | score (user/sys/blend) | routed |
|---|---|---|---|---|
| 16267 | `/grilling` skill | `classifier_standard_blended_deep` | 55/82/60 | opus |
| 16268–16271 | tool_result turns | `session_cached_walkback_tool_result` | – | opus |
| 16273 | `Q1,2 - ok / Q3 - no, treat as autonomous candidate / Q4-9 - ok` | `classifier_trivial` | 18/75/29 | haiku |
| **16274** | `yes please` | `classifier_trivial` | 8/82/23 | **haiku** |
| 16275–16285 | tool_result turns (`File created successfully at: …` ×4, a Python error, pip) | `session_cached_walkback_tool_result` | – | haiku |
| 16286 | `please make one amendment - Jamie's position would be intermediate - not senior` | `classifier_trivial` | 5/85/21 | haiku |

Facts from this table:

- #16273 also had a `prior_response_summary` (the Q1–Q9 questions) and still scored 18 (DB `id=16273`, `classifier_summary_json`, `classifier_raw_response='18'`).
- #16274's trivial result was written to the session tier cache. `_apply_tier_cache` stores `routing.routed_model` whenever `classification is not None` (`anthrouter/handlers.py:1000-1007`).
- The 11 drafting turns #16275–#16285 then replayed `haiku` through the walk-back cache (`anthrouter/model_router.py:1791-1807`). The four deliverable files were written by Haiku.

### 2.4 How often this happens

From `~/.anthrouter/anthrouter.db`:

- Only 18 rows ever took an affirmation path: 17 `affirmation_inherited`, 1 `affirmation_classified` (ids 8656–14970).
- The one `affirmation_classified` row (#13223, prompt `continue`) also scored 5 with a long `prior_response_summary` in its input. So even the dedicated affirmation path gives a trivial score when JSON mode is off.
- Short approval-like prompts that missed the phrase set and went to the classifier as `classifier_trivial`: `yes please` (2), `sure continue`, `please resume`, `yes park here`, `yes update handoff please`, `go to approval gate`, `do it anyway`, and others.

---

## 3. Verified facts: what the classifier sees on an affirmation turn (anthrouter)

1. `build_routing_summary()` sets `is_short_affirmation` only when the text came from the final message itself and `is_short_affirmation(final_user_text)` is true (`anthrouter/model_router.py:784-786`).
2. `is_short_affirmation()` lowercases, collapses spaces, strips trailing punctuation, drops one *leading* `please `, and tests membership in a fixed set (`anthrouter/request_text.py:76-104`). `yes please` normalises to `yes please`. That string is not in the set, and `please` is trailing, not leading. Result: `False`.
3. `prior_response_summary` is extracted unconditionally, before the affirmation branch (`anthrouter/model_router.py:1833-1841`). It is the text blocks of the most recent text-bearing assistant message, cut 30/70 head/tail to `auto_model_routing_prior_response_summary_limit` (default 1000) (`anthrouter/model_router.py:798-839`).
4. The affirmation branch runs only when `auto_model_routing_affirmation_inherit` is on and `summary.is_short_affirmation` is true (`anthrouter/model_router.py:1855-1856`). With a cached tier it returns `affirmation_inherited` with no classifier call (`:1857-1872`). Without one it calls the classifier with `prior_response_summary` (`:1874-2022`).
5. On the main path, `build_classifier_payload()` puts `prior_response_summary` into the user message through `to_classifier_json()` (`anthrouter/model_router.py:487-501`, `:1048-1056`). The system prompt is `_CLASSIFIER_SYSTEM` when `confidence_bump` is off (`:1041`). The prior-context suffix is added only when `use_json` is true (`:1042-1043`; same gate on the affirmation path at `:1927-1928`).
6. `_CLASSIFIER_SYSTEM` says (`anthrouter/model_router.py:261-265`): "Judge complexity ONLY from the user's intent in "final_user_text". The other fields … describe the harness setup and prior context, NOT the difficulty of THIS request — never escalate because of them. A short, simple request is low (e.g. 5)". Its examples include `"thanks, that works" → 5` (`:273`).
7. The JSON prompt has the same "ONLY from final_user_text" sentence (`anthrouter/model_router.py:313-316`). The suffix appended in JSON mode only says "use it as additional context" (`:337-340`). The two instructions conflict.

So, for #16274, the classifier received the prior assistant text but was told, in plain words, not to use it.

Privacy: `to_classifier_json()` sends only the dataclass fields listed at `anthrouter/model_router.py:488-498` plus `prior_response_summary`. Routing-internal flags (`is_short_affirmation`, `recovered_via_walkback`, `system_prompt_sha256`, `trailing_skipped`) are excluded (`:471-486`). That matches the contract in `AGENTS.md` § Classifier-input privacy. Note: `prior_response_summary` is already prior assistant text, so it already goes past the "final user text plus counts" wording of that contract. The contract text does not mention it.

---

## 4. Verified facts: anthproxy

Location: `/Users/r.herasymenk/workspace/anthproxy` (remote `https://github.com/nj4x/anthproxy.git`, HEAD `71d0bc9`).

- Same affirmation phrase set and same normaliser (`anthproxy/request_text.py:76-104`). A `diff` of the two `request_text.py` files shows only activity-label changes, nothing in affirmation detection.
- Same `_CLASSIFIER_SYSTEM` text, including "Judge complexity ONLY …" (`anthproxy/model_router.py:252-300`, sentence at `:268`). A `diff` against `anthrouter/model_router.py:245-293` is empty.
- Same unconditional `prior_response_summary` extraction (`anthproxy/model_router.py:1812-1820`) and same `use_json` gate on the suffix (`anthproxy/model_router.py:1036`, `:1906`).
- `anthproxy/summary.py` builds background session summaries, but only for activity labels (ADR-0041, `anthproxy/summary.py:1-25`). `model_router.py` does not use it.
- anthproxy's DB shows the same miss: one `yes please` row was routed by the classifier, not by an affirmation path (`~/.anthproxy/anthproxy.db`, `requests`, query on `user_prompt_text`).

anthproxy's design record shows the gap was known, and the record contradicts itself:

- ADR-0013 says the system prompt "must be amended to accept and prioritize `prior_response_summary` … judge complexity from the context in prior_response_summary; it describes what the user is agreeing to proceed with" (`anthproxy/docs/adr/0013-prior-response-context-for-affirmations.md:7`).
- The same ADR rejects "Gate enrichment on `confidence_bump`" because the flag defaults to False and "would silently disable enrichment for most deployments" (`:32`).
- The same ADR also says the affirmation call always uses `_CLASSIFIER_SYSTEM_JSON` (`:43`), but `:9` says "Do not override the `confidence_bump` flag".
- The spec settles on the gated version: "When `confidence_bump` is False, the base `_CLASSIFIER_SYSTEM` is used unchanged; it treats `prior_response_summary` as harness context (similar to tool counts) and judges complexity from `final_user_text` alone" (`anthproxy/docs/specs/prior-response-affirmation-enrichment.md:80`).
- The implemented suffix is weaker than the ADR wording: "use it as additional context" (`anthproxy/model_router.py:345`), not "judge complexity from the context in prior_response_summary".

Net: the effect that ADR-0013 rejected is what runs in both repos with the default `confidence_bump=False`.

Side note: anthrouter's code cites "ADR 0010/0012" for the system-prompt blend (`anthrouter/model_router.py:478-481`, `AGENTS.md`). Those are anthproxy's numbers (`anthproxy/docs/adr/0010-weighted-classifier-combination.md`). anthrouter's own `docs/adr/0010` and `0012` are about other things (`docs/adr/0010-inline-system-rewrite-gated-by-model.md`, `docs/adr/0012-tier-cache-key-includes-requested-tier.md`).

---

## 5. Inference: root cause chain

These follow from the facts above but were not tested by re-running the classifier.

1. #16273 answered a list of questions ("Q1,2 - ok …"). The classifier judged the short answer text only and gave 18. The tier cache became `haiku`.
2. #16274 `yes please` missed the phrase set, so it went to the main classifier. The prompt told the classifier to ignore `prior_response_summary`. Score 8, cache stays `haiku`.
3. #16275–#16285 replayed `haiku` for the drafting work.

Fixing only affirmation detection (step 2) would give `affirmation_inherited` → `haiku`, because of step 1. The defect is the prompt instruction, which affects every short reply to a proposal: approvals, answers to questions, and choices.

---

## 6. Options to improve classifier input

### A. Make the classifier use `prior_response_summary` in both prompt formats (recommended)

- **Change**: In `_CLASSIFIER_SYSTEM` (`anthrouter/model_router.py:245-293`) and `_CLASSIFIER_SYSTEM_JSON` (`:297-329`), replace the "ONLY from final_user_text" rule with a two-part rule. Without `prior_response_summary`: judge `final_user_text` alone, as now. With it: if `final_user_text` approves, answers or chooses among what the prior response proposed, score the work that it approves. Add examples, such as `"yes please"` with prior `"… Ready to draft?"` → 50, and `"thanks, that works"` with a prior closing summary → 5. Remove the `use_json` gate on the suffix at `:1042` and `:1927`, or fold the suffix into both base prompts.
- **Why it fixes #16274**: the input already carried "4 files … Ready to draft?". Both #16273 and #16274 would score from that text.
- **Privacy**: no change. The field is already sent (`:499-500`).
- **Cost**: no new tokens in the input. The prompt grows by a few lines.
- **Risk**: a reply of thanks after a long, complex answer could be scored high. The "thanks" example and the "approves/answers/chooses" wording limit that. Scores for every turn that has a prior assistant message can move, so check against stored `classifier_summary_json` rows before release (replay them through the new prompt).
- **Process**: this reverses the choice in anthproxy's spec (`…/prior-response-affirmation-enrichment.md:80`). Record it as an anthrouter ADR.

### B. Widen affirmation detection

- **Change**: In `is_short_affirmation()` (`anthrouter/request_text.py:87-104`), accept a trailing `please`, and accept a short sequence where every token is in an approval vocabulary (`yes please`, `ok do it`, `sure continue`, `yes go ahead`).
- **Effect on #16274**: none by itself. It would route to `affirmation_inherited` → cached `haiku`.
- **Privacy**: none. Detection is local.
- **Risk**: low if the vocabulary stays closed. Phrases like `yes park here` or `do it anyway` still carry content and should stay on the classifier.

### C. Do not inherit a trivial cached tier on an affirmation turn

- **Change**: In the cached branch (`anthrouter/model_router.py:1857-1872`), when the cached tier is the trivial tier and `prior_response_summary` exists, call the classifier as the no-cache branch does, and route at the higher of the two results.
- **Effect**: an approval after a trivial-scored turn gets a fresh, context-based score. With A in place, this stops a bad earlier score from being replayed.
- **Cost**: one extra classifier call on those turns only.
- **Risk**: needs a new reason code in `ReasonCode` (`:347-…`), for example `affirmation_reclassified`.

### D. Add a derived flag for a pending proposal

- **Change**: In `to_classifier_json()` (`:487-501`), add a boolean such as `prior_response_asks_confirmation` (prior text ends with `?`). Add it to the dict on purpose, as the privacy contract requires.
- **Privacy**: one bit, derived from text already sent.
- **Value**: small. With A, the classifier can read the question from the text.

### E. Send a tool-activity summary of the prior turn

- Tool names and schemas are banned from classifier input (`AGENTS.md` § Classifier-input privacy; `anthrouter/model_router.py:458-461`). Counts are already sent. Not recommended.

### Recommendation

Do **A** first. It is a prompt-only change, adds no input fields, and addresses the actual cause seen in #16273 and #16274. Add **B** and **C** after it, so approvals skip the classifier when the cache is sound and re-score when the cache holds a trivial tier. Port the same change to anthproxy, which has the same gap.

---

## Sources

- `~/.anthrouter/anthrouter.db`, table `requests`, ids 16253–16291, 16273, 16274, 13223; aggregate queries on `reason_code` and `user_prompt_text`.
- `~/.anthrouter/anthrouter.log:706191-706204`.
- `~/.anthrouter/config.env` (routing keys only).
- `~/.anthproxy/anthproxy.db`, table `requests`, query on `user_prompt_text`.
- `anthrouter/model_router.py`, `anthrouter/request_text.py`, `anthrouter/handlers.py` at `0eeeebc`. The installed copy in `~/.anthrouter/src` is at `c8f9428`; `0eeeebc` changes only the security-monitor reply.
- `anthproxy/model_router.py`, `anthproxy/request_text.py`, `anthproxy/summary.py`, `anthproxy/docs/adr/0013-prior-response-context-for-affirmations.md`, `anthproxy/docs/specs/prior-response-affirmation-enrichment.md` at `71d0bc9`.
- `AGENTS.md`, `docs/agents/architecture.md:14-20`.
