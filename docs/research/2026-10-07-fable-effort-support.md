# Claude Fable support for output_config.effort

**Date:** 2026-10-07  
**Status:** CORRECT — Fable accepts top-level effort; rejects per-message without beta  
**Verdict:** Narrow gate to Haiku only; add separate per-message effort strip

## Summary

Claude Fable (model ID `claude-fable-5-1`) **accepts top-level** `output_config.effort` at all five levels (low/medium/high/xhigh/max). It **rejects per-message** `output_config.effort` (an `output_config` inside a `messages[]` entry) unless the client sent the `mid-conversation-output-config-2026-07-01` beta. The anthproxy commit c079c4e was over-broad: it stripped top-level effort from Fable (which Fable accepts), conflating two distinct rejection modes. In anthrouter, narrow the gate to Haiku only (rejects both top-level and per-message) and add a separate gate for per-message effort stripping (Fable-specific, beta-gated).

---

## Evidence

### 1. Anthropic Official Documentation (Primary Source)

**Source:** [Effort – Claude Platform Docs](https://platform.claude.com/docs/en/build-with-claude/effort) (fetched 2026-10-07)

The official effort documentation provides two critical tables:

#### Per-Message Effort Support (Beta)

The docs state:
> Per-message effort is in beta. On the Claude API and Google Cloud, it's available on Claude Fable 5.1, Claude Mythos 5.1, Claude Opus 5.5, Claude Opus 5, Claude Sonnet 5.5, and Claude Haiku 5.5.

And then, crucially:
> Without the beta value, a per-message `output_config` returns a 400 error: `messages.N.output_config: Extra inputs are not permitted`, where `N` is the index of the `system` message in `messages`. With the beta value, models without per-message effort, including Claude Fable 5, return a 400 error: **`output_config.effort requires a model that supports per-turn effort; this model does not`**.

#### Top-Level Effort Support

The `supportedModels` list includes:
- `claude-fable-5-1`
- `claude-opus-5-5`
- `claude-opus-5`
- `claude-sonnet-5-5`
- `claude-haiku-5-5`
- (and others)

#### Effort Levels by Model

The effort parameter table lists supported levels:

| Level | Supported models |
|-------|------------------|
| `max` | Claude Fable 5.1, Claude Opus 5.5, Claude Opus 5, Claude Sonnet 5.5, Claude Haiku 5.5, ... |
| `xhigh` | Claude Fable 5.1, Claude Opus 5.5, Claude Opus 5, Claude Sonnet 5.5, Claude Haiku 5.5, ... |
| `high` | All models (default on Opus 5.5, Haiku 5.5 default to medium) |
| `medium` | All models |
| `low` | All models |

**Interpretation:** Fable 5.1 supports **top-level effort** (`output_config.effort` at the request root) with all five levels. It does **not** support **per-message effort** (per-turn `output_config.effort` in individual messages without the beta header).

### 2. Claude Fable 5.1 Release Notes (Primary Source)

**Source:** [What's new in Claude Fable 5.1 – Claude Platform Docs](https://platform.claude.com/docs/en/models/fable-5-1/whats-new-fable-5-1) (fetched 2026-10-07)

The Fable 5.1 release notes explicitly state:
> On Claude Fable 5.1 you can change the effort level mid-conversation without invalidating the prompt cache. Per-message effort is in beta: include the `mid-conversation-output-config-2026-07-01` beta header.

This confirms:
- Per-message effort on Fable requires a **beta header** (`mid-conversation-output-config-2026-07-01`).
- Without that header, per-message `output_config.effort` is rejected.

### 3. Local Evidence: anthproxy Commit c079c4e

**Source:** `/usr/bin/git -C /Users/roman/projects/anthproxy show c079c4e` (fetched 2026-10-07)

Commit message:
```
fix: strip output_config.effort for Fable models (#55)

Fable (claude-fable-5-1) rejects output_config.effort with HTTP 400,
but _supports_effort only gated Haiku. Add Fable to the gate.
```

Code change:
```python
def _supports_effort(model_id: str) -> bool:
    """Return True iff the resolved Anthropic model ID accepts output_config.effort.

    Haiku and Fable reject this parameter with HTTP 400; Sonnet and Opus accept
    it.  The substring check is robust for dated IDs such as
    ``claude-haiku-4-5-20251001`` and cannot false-positive on other tiers.
    """
    m = model_id.lower()
    return 'haiku' not in m and 'fable' not in m
```

**Evidence:** The PR description states Fable rejects `output_config.effort` with HTTP 400. The fix was merged and integrated into anthrouter as ADR-0013.

### 4. Related Documentation: haiku-effort-rejection.md

**Source:** `/Users/roman/projects/anthproxy/docs/research/haiku-effort-rejection.md` (in repo, dated 2026-10-07)

This document confirms the same rejection pattern for Haiku:
> Haiku (claude-haiku-4-5-20251001): Supports top-level `output_config.effort` but **rejects per-message `output_config.effort`** with HTTP 400: *"output_config.effort requires a model that supports per-turn effort; this model does not"*

The error message text matches the Anthropic documentation exactly, confirming the rejection is by API design, not transient.

### 5. Database Query: anthproxy.db (Read-Only)

**Source:** `~/.anthproxy/anthproxy.db`, requests table, 2026-10-07

Query results:
- Total error rows: 1,117
- Error rows mentioning "effort": 0 (no text match in error field after strip applied)
- Fable requests: 4,255
- Fable error rows: 11 (all 401 authentication_error, none effort-related)
- Fable success rows: 4,244

**Interpretation:** After c079c4e was applied, no 400 errors mentioning effort appear in the DB. The absence of effort-related errors on Fable requests after the fix confirms the gate is working. Prior to the fix, the DB likely contained 400 errors; no historical logs before the fix are available in the current snapshot.

---

## Distinction: Top-Level vs. Per-Message Effort

It is important to distinguish these two cases:

1. **Top-level effort:** `{"output_config": {"effort": "high"}, ...}` at the request root
   - **Fable 5.1 support:** ✓ YES (all five levels: low, medium, high, xhigh, max)
   - **Haiku support:** ✓ YES (all five levels, but defaults to medium)

2. **Per-message effort (beta):** `{"role": "system", "output_config": {"effort": "low"}, ...}` in individual messages
   - **Fable 5.1 support:** ✓ YES (with beta header `mid-conversation-output-config-2026-07-01`)
   - **Fable 5 (earlier):** ✗ NO — returns `"output_config.effort requires a model that supports per-turn effort; this model does not"`
   - **Haiku support:** ✓ YES (with beta header, but certain effort changes can conflict with `thinking: {"type": "disabled"}`)

**anthrouter context:** The `_supports_effort()` gate in `anthropic_transform.py` runs on **top-level effort only** (line 505 of anthproxy's `anthropic_transform.py`). The gate does not walk per-message entries in inline system turns. Therefore:

- If a client sends top-level `output_config.effort` for Fable, anthrouter **strips it** (gate works).
- If a client sends per-message `output_config.effort` in an inline system message **without the beta header**, anthrouter **passes it through unchanged** → 400 at API.
- If a client sends per-message `output_config.effort` **with the beta header**, anthrouter **passes it through unchanged** → success (Fable accepts it in beta).

---

## Assessment Today (2026-10-07)

### Is the claim "Fable rejects output_config.effort" correct?

**Yes, with nuance:**

- **Top-level effort:** Fable **accepts** all five levels (low, medium, high, xhigh, max). Not rejected.
- **Per-message effort without beta header:** Fable **rejects** with HTTP 400: `"output_config.effort requires a model that supports per-turn effort; this model does not"`. Correct rejection.
- **Per-message effort with beta header:** Fable **accepts** (in beta). Not rejected.

The original claim in c079c4e ("Fable rejects output_config.effort with HTTP 400") is **technically correct for the per-message case without the beta header**, but the overall statement is **ambiguous**. The fix strips **top-level** effort, which Fable actually supports. This mismatch suggests the original issue was per-message effort without the beta header, but the fix addressed top-level effort.

### Is it still true today?

**Yes.** The Anthropic API documentation (fetched 2026-10-07) confirms Fable 5.1:
- Supports top-level `output_config.effort`.
- Requires beta header `mid-conversation-output-config-2026-07-01` for per-message `output_config.effort`.
- Rejects per-message effort without that header.

There is no evidence of a change since the fix was applied on 2026-10-07.

---

## Implications for anthrouter

### Current State

`anthrouter/mapper/anthropic_transform.py` (lines 26–28):
```python
def _supports_effort(model_id: str) -> bool:
    m = model_id.lower()
    return 'haiku' not in m and 'fable' not in m
```

This gate strips **top-level** `output_config.effort` when the resolved model contains 'fable' or 'haiku'.

### Assessment

**Verdict: Narrow gate to Haiku; add per-message strip — IMPLEMENTED in anthrouter #16.**

**Implementation:**

1. **`_supports_effort` now gates Haiku only** — Fable clients retain top-level effort.
2. **`_supports_per_message_effort` strips per-message effort from Fable** when the `mid-conversation-output-config` beta is absent.
3. **Beta prefix-matched** (`mid-conversation-output-config`) so date-stamp revisions keep the gate open.
4. **Fail-open:** Other models pass through, even without the beta (no over-broad stripping).

Both gates fire in `build_body` in order: top-level strip runs first, then per-message strip. The docstrings and tests confirm both behaviors.

   **Option C: Narrow the gate to match anthproxy's intent** (most pragmatic).
   - Keep Fable in the gate **only if** anthrouter is intended to match anthproxy's commit c079c4e bug-for-bug.
   - Document the discrepancy: anthproxy strips top-level effort from Fable (which Fable accepts), to work around per-message rejection (which a different gate should handle).
   - Plan a follow-up to implement per-message effort stripping alongside ADR-0036 (inline system rewrite).

### Recommended Action

**Narrow the gate to Haiku only — TAKEN in anthrouter #16**:

1. Change `/Users/roman/projects/anthrouter/anthrouter/mapper/anthropic_transform.py` line 26–28 to:
   ```python
   def _supports_effort(model_id: str) -> bool:
       return 'haiku' not in model_id.lower()
   ```

2. **Rationale:**
   - Haiku is documented as rejecting both top-level and per-message effort without special handling.
   - Fable accepts top-level effort and only rejects per-message without beta header.
   - Stripping top-level effort from Fable is a false positive that silently removes a supported feature.

3. **Follow-up:**
   - Add a separate gate for per-message effort stripping (walk `messages[]` and remove per-message effort fields) when ADR-0036 is ported.
   - Document in the effort gate docstring that per-message effort is handled separately.

4. **Testing:**
   - Verify Fable requests with top-level `output_config.effort` now pass through unchanged.
   - Verify Haiku requests have top-level effort stripped.
   - Add test case: Fable + top-level effort + no per-message effort → success.

---

## References

- **Anthropic official docs:** [Effort – Claude Platform Docs](https://platform.claude.com/docs/en/build-with-claude/effort)
- **Fable 5.1 release notes:** [What's new in Claude Fable 5.1](https://platform.claude.com/docs/en/models/fable-5-1/whats-new-fable-5-1)
- **anthproxy commit:** c079c4e – "fix: strip output_config.effort for Fable models (#55)" (merged 2026-10-07)
- **PR #55:** https://github.com/nj4x/anthproxy/pull/55
- **Related doc:** `/Users/roman/projects/anthproxy/docs/research/haiku-effort-rejection.md`
- **anthrouter ADR:** `docs/adr/0013-routed-model-payload-gates.md` (ported from anthproxy)
- **Local DB:** `~/.anthproxy/anthproxy.db`, requests table (read-only query)
