# Admin UI Feature Implementation Audit (2026-09-01)

## Summary

Three features were requested for the admin UI across ALL tables. Investigation of commits 2b0bc50, 52f33f4, 21e9f98, 15f5fe6, 6e54afc, bee816a and the source code shows:

1. **Request # column** — **FULLY IMPLEMENTED**
2. **Classification scores (s:/u: display)** — **PARTIAL**: Routing table only; not in Requests, Sanitizer, Usage
3. **Request-detail linking** — **FULLY IMPLEMENTED**

---

## Feature Status Matrix

| Feature | Requests | Routing | Sanitizer | Usage | Notes |
|---------|----------|---------|-----------|-------|-------|
| Request # column (clickable) | ✅ Implemented | ✅ Implemented | ✅ Implemented | N/A (no table) | All tables have `#` column with row click → detail drawer |
| s:/u: classifier scores | ❌ Missing | ✅ Implemented | ❌ Missing | N/A (no scores) | Routing shows `u:NN s:NN` format; data available in API but unused elsewhere |
| Row link to detail view | ✅ Implemented | ✅ Implemented | ✅ Implemented | N/A | All tables open RequestDetailDrawer on click |

---

## Implementation Details

### Feature 1: Request # Column

**Status:** ✅ Fully implemented.

**Commit:** 2b0bc50 ("Add request # column to admin tables, align routing defaults with anthproxy")

**File citations:**

- **Requests.tsx** (line 42–64): Table has `['#', 'Time', 'Session', 'Model', 'Status', 'Tokens', 'Cost', 'Took', 'Prompt']` headers. Row 64 renders `<td className="...">#{row.id}</td>`. Rows are clickable (line 63) to open detail drawer.
- **Routing.tsx** (line 54, 62): Table headers include `'#'`. Row 62 renders request ID. Rows are clickable (line 60) via `onClick={() => setSelected(...)}`.
- **Sanitizer.tsx** (line 39, 46): Table headers include `'#'`. Row 46 renders `#{event.request_id}`. Rows are clickable (line 44).

All three tables:
- Have request ID as the first column (clickable).
- Pass `requestId` to `RequestDetailDrawer` component (Requests.tsx line 55, Routing.tsx line 94, Sanitizer.tsx line 70).

---

### Feature 2: Classification Scores (s:/u: Display)

**Status:** ⚠️ **Partial**. Routing table only; missing from Requests and Sanitizer.

**Score fields available in database/API:**
- `system_prompt_score` (Real, nullable) — added in migration 2 (db.py line 160)
- `user_prompt_score` (Real, nullable) — added in migration 2 (db.py line 161)
- `routing_weighted_score` (Real, nullable) — added in migration 2 (db.py line 162)

These are populated from `routing_decision` object attributes (db.py lines 353–355).

**Implemented:** Routing table only

- **Routing.tsx** (line 79–80): Displays `u:{Math.round(row.user_prompt_score)} s:{Math.round(row.system_prompt_score)}` when both scores are non-null.
- **Commit:** 2b0bc50: "Surface user/system classifier scores (u:NN s:NN) in the Routing table instead of an opaque classifier-model string."

**API exposure:**
- `db.py:get_routing_decisions()` (lines 452–467): **Explicitly selects** `system_prompt_score`, `user_prompt_score`, `routing_weighted_score` and returns them in the dict.
- `api.ts:RequestRow` (lines 35–37): Interface includes all three score fields.

**Missing:** Requests and Sanitizer tables

- **Requests.tsx**: Does not render `system_prompt_score` or `user_prompt_score`. Data is present in the RequestRow (api.ts) and in the API response from `db.py:get_requests()` (SELECT * line 416), but the table ignores it.
- **Sanitizer.tsx**: Does not render scores. Data is not fetched at all — `get_recent_sanitizer_events()` (db.py lines 483–495) does not SELECT the score columns.
- **Usage.tsx**: No table, scores not applicable.

**Detail drawer:** No scores shown

- **RequestDetailDrawer.tsx** (lines 94–164): Model Routing section shows `classification` badge and `reason_code`, but does NOT display `system_prompt_score`, `user_prompt_score`, or `routing_weighted_score`.
- Data is available (comes through api.ts RequestRow, fetched via `/admin/requests/{id}`), but not rendered.

---

### Feature 3: Request-Detail Linking

**Status:** ✅ Fully implemented.

**What exists:**
- All three tables (Requests, Routing, Sanitizer) are clickable rows that open a detail drawer.
- `RequestDetailDrawer` component (RequestDetailDrawer.tsx) opens on row click (via `requestId` prop).
- API endpoint `/admin/requests/{id}` (admin.py lines 53–57) returns full request detail plus sanitizer events.
- DB query `get_request()` (db.py lines 378–402) joins `prompt_store` to include full system prompt, sanitized prompt, and tools content.

**Drawer content includes:**
- Summary: time, status, duration, error (lines 95–127)
- Model Routing: requested, routed (if changed), classification (badge), reason code (lines 130–164)
- Tokens & Cost: input/output, cache hit ratio, cost, cache savings (lines 167–199)
- Prompt Hashes: system SHA256, sanitized SHA256 (lines 202–228)
- Full Prompt Content: original system prompt (lines 231–239), sanitized version when different (lines 242–253), tools (lines 255–264)
- Response text (lines 267–276)
- Sanitizer Events: stripped blocks detail (lines 279–314)

**Commit:** 52f33f4 ("Join prompt_store content into admin request-detail response"): Rewrites `db.py:get_request()` from `SELECT *` to explicit columns plus three LEFT JOINs to `prompt_store`, exposing full system-prompt/tools content.

---

## Data Available in API But Not Rendered

| Field | API Response | Table Display | Detail Drawer | Status |
|-------|---|---|---|---|
| `system_prompt_score` | ✅ Routing only | ❌ Missing from Requests, Sanitizer | ❌ Missing | Unused except Routing |
| `user_prompt_score` | ✅ Routing only | ❌ Missing from Requests, Sanitizer | ❌ Missing | Unused except Routing |
| `routing_weighted_score` | ✅ Routing only | ❌ Missing | ❌ Missing | Unused everywhere |
| `system_prompt_content` | ✅ Detail only | — | ✅ Full prompt shown | Implemented |
| `classifier_raw_response` | ✅ Everywhere | ✅ Fallback in Routing (24-char preview) | ❌ Missing | Partial |

---

## Remaining Work

### If score display is desired in Requests and Sanitizer tables:

1. **Requests.tsx**
   - Add header `'Scores'` to table headers list (line 42).
   - In `Row` component (lines 60–82), add a cell after the "Cost" column:
     ```tsx
     <td className="px-3 py-2 font-mono text-xs">
       {row.user_prompt_score != null && row.system_prompt_score != null ? (
         <span>u:{Math.round(row.user_prompt_score)} s:{Math.round(row.system_prompt_score)}</span>
       ) : '—'}
     </td>
     ```
   - Data is already in the API (from `db.py:get_requests()` SELECT * line 416).

2. **Sanitizer.tsx**
   - Modify `get_recent_sanitizer_events()` (db.py line 483–495) to SELECT `system_prompt_score, user_prompt_score` from the joined `requests` table:
     ```sql
     SELECT e.id, e.request_id, ..., r.system_prompt_score, r.user_prompt_score
     FROM sanitizer_events e
     JOIN requests r ON r.id = e.request_id
     ORDER BY ...
     ```
   - Add header `'Scores'` to table headers (line 39).
   - In the event row, add a cell with the same conditional rendering as above.

3. **RequestDetailDrawer.tsx** (optional)
   - Add a row to the "Model Routing" section (after line 162):
     ```tsx
     {(req.user_prompt_score != null || req.system_prompt_score != null) && (
       <div className="flex justify-between items-start">
         <dt className="text-xs text-slate-600 dark:text-slate-400">Scores</dt>
         <dd className="text-slate-900 dark:text-slate-200 text-xs font-mono">
           u:{Math.round(req.user_prompt_score ?? 0)} s:{Math.round(req.system_prompt_score ?? 0)}
           {req.routing_weighted_score != null && ` → ${Math.round(req.routing_weighted_score)}`}
         </dd>
       </div>
     )}
     ```

---

## Summary Table: What Was Shipped vs. What's Missing

| Requirement | Implementation | Gaps | Effort to Complete |
|---|---|---|---|
| Request # column across all tables | ✅ Complete | None | Done |
| s:/u: scores in all tables | ⚠️ Routing only (2/4 tables with tables) | Missing: Requests, Sanitizer. Detail drawer never shows scores | Requests: 1 line UI + 1 line header. Sanitizer: 1 DB query tweak + 1 line UI + 1 line header. Detail: 5-line optional section |
| Request detail view | ✅ Complete | None | Done |

---

## References

- **Commit 2b0bc50**: Request # column, s:/u: scores in Routing table
  - Files: `anthrouter/config.py`, `anthrouter/db.py`, `anthrouter/ui/src/views/Requests.tsx`, `Routing.tsx`, `Sanitizer.tsx`
- **Commit 52f33f4**: Join prompt_store into detail response
  - Files: `anthrouter/db.py`, `anthrouter/ui/src/components/RequestDetailDrawer.tsx`
- **ADR 0003**: Reason codes reflect post-blend outcome
- **ADR 0004**: Admin request-detail exposes full prompt content

---

## Code Locations

**DB Schema and Migrations:**
- `/Users/r.herasymenk/workspace/anthrouter/anthrouter/db.py:152–163` — Migration 2: score columns added

**API Endpoints:**
- `/Users/r.herasymenk/workspace/anthrouter/anthrouter/db.py:452–467` — `get_routing_decisions()` explicitly selects scores
- `/Users/r.herasymenk/workspace/anthrouter/anthrouter/db.py:404–437` — `get_requests()` uses SELECT *; includes scores but doesn't explicitly name them
- `/Users/r.herasymenk/workspace/anthrouter/anthrouter/db.py:483–495` — `get_recent_sanitizer_events()` does not select score columns

**UI Tables:**
- `/Users/r.herasymenk/workspace/anthrouter/anthrouter/ui/src/views/Requests.tsx:42–82` — Requests table, no scores
- `/Users/r.herasymenk/workspace/anthrouter/anthrouter/ui/src/views/Routing.tsx:54–96` — Routing table, scores rendered at line 79–80
- `/Users/r.herasymenk/workspace/anthrouter/anthrouter/ui/src/views/Sanitizer.tsx:39–73` — Sanitizer table, no scores
- `/Users/r.herasymenk/workspace/anthrouter/anthrouter/ui/src/views/Usage.tsx` — No request table (summary only)

**Detail Drawer:**
- `/Users/r.herasymenk/workspace/anthrouter/anthrouter/ui/src/components/RequestDetailDrawer.tsx:94–164` — Model Routing section, no scores displayed

**Type Definitions:**
- `/Users/r.herasymenk/workspace/anthrouter/anthrouter/ui/src/api.ts:1–38` — `RequestRow` interface includes `system_prompt_score`, `user_prompt_score`, `routing_weighted_score`
