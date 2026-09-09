# Phase I — ADMIN STUDENT SEARCH (READ-ONLY, SERVER-SIDE)

Scope lock: this phase added **only** a read-only Student Search inside
Admin Panel → Student Services → Students. Everything else is unchanged.

## A. Feature Summary

- New server-side search: `GET /api/admin/students/search?q=...&status=...&page=...&page_size=20`
  (Super Admin only — same `require_superadmin` boundary as the whole Students area).
- New UI in the existing Students pane: a **Search Student** bar
  (one input: *Name / Class Roll No / Registration No* + **[Search]** **[Clear]**),
  a "Matching Students" results table, a per-row **Details** button that opens a
  safe 6-field details card, search pagination, and an explicit **Clear** that
  restores the normal paginated Students list.
- No-match is a normal `200` + empty list rendered as
  *"No student found matching your search."* — never a 404/stack trace.

## B. Search Behavior

Single query box, deterministic server-side matching (SQL/SQLAlchemy only):

| Field | Semantics |
|---|---|
| **Name** | case-insensitive substring (`ILIKE %q%`) — tolerant: `abid` finds `Abid Ahmad`, `Abid Hussain`, `Abid Malik`; ALL matches are returned, never an arbitrary first hit |
| **Registration number** | case-insensitive search (substring, preserving the existing list `q` convention) with the **exact reg match ranked first** (rank 0) |
| **Class/college roll number** | `Student.roll_no`, case-insensitive prefix/exact (complete roll matches exactly), **rank 1** |

Ranking (SQL `CASE`, before pagination): exact reg 0, exact roll 1, exact name 2,
partial 3 → ordered by rank then reg_no. Bounded result set via `page/page_size`
(1–100), reusing the existing pagination convention. `q` is **never** run as a
DOB lookup.

## C. Student Details

The details card (and every search JSON object) contains exactly:

- `name`
- `reg_no`
- `roll_no` (class/college roll)
- `exam_roll_no` (examination roll — may be `null`)
- `exam_roll_conflict` (bool — see E)
- `programme`
- `current_semester`

(`id` is included for row selection only; the existing Students list already
exposes it.) Allowed-key set is enforced by tests.

## D. DOB Security

- **DOB is NOT returned by the API** — verified by inspecting the actual
  browser-captured `/search` JSON (raw text has no `dob`/`date_of_birth`).
- **DOB is NOT displayed** — the search details card contains no DOB field and
  no "Hidden"/"Protected" placeholder (asserted in browser + tests).
- **DOB hash is NOT returned** — `hashed_password`/bcrypt strings absent from
  search responses (tested, incl. the live network response).
- **No plaintext DOB was added** — the schema is byte-identical and the DOB
  credential column remains write-only; `Student.hashed_password` remains
  bcrypt `$2` (tested).
- **Authentication was not modified** — `dob.py` hashing/normalization,
  `verify`, StudentSession, TTL, login/logout untouched; regression test proves
  a student can still sign in with their DOB after the feature ships.

## E. Examination Roll Number

- Source: the **existing** `StudentResult.exam_roll_no` column (the published
  examination-roll field used by the Results workflow). Never derived from
  `roll_no`/`reg_no`, never generated, never LLM-inferred.
- **Consistency policy** (no silent picking): one success insert/payload per
  student aggregates every non-null `exam_roll_no`; if there is exactly **one
  distinct** value it is displayed; **zero** → `null`; **two or more distinct**
  values → `exam_roll_no: null` **with `exam_roll_conflict: true`** so the UI
  shows "(inconsistent — see results)" instead of an arbitrary choice.
- Tested: `roll_no` and `exam_roll_no` stay separate and distinct.

## F. Authorization

`GET /api/admin/students/search` uses the **same `require_superadmin`**
dependency as every Students-management endpoint (create/edit/delete/list).
Anonymous → 401, admin/authority/student → 403 (server-side enforced, tested).
No permission boundary was changed or widened.

## G. Files Changed

- `backend/app/student_admin/service.py` — added `case` import, `_to_search_dto`
  (explicit search allowlist) and `search_students()` (SQL ranking + exam-roll
  aggregation). `list_students` untouched.
- `backend/app/student_admin/routes.py` — added `GET /search` (declared **before**
  `/{student_id}` so it can never be shadowed) + docstring line.
- `frontend/js/admin_students.js` — Search Student bar, results table, search
  pagination, safe details card, Clear + list/view switching (`runSearch`,
  `loadSearch`, `renderSearch`, `openSearchDetails`, `refreshStudents`,
  `_setSearchMode` in `load()`), new state vars. Existing list/create/edit/
  reset/delete flows unchanged.
- `backend/tests/test_student_search.py` — new 13-test battery.
- No production schema, results, admit-card, exam-form, chat, RAG or auth code
  was modified.

## H. Database

**"No database schema change."** The `Student` column set is asserted verbatim
in tests (including `hashed_password` and the write-only `dob`), and no search
index/table/column was added.

## I. Tests

- **New:** 13 in `tests/test_student_search.py` — authorization (401/403×3/200),
  name/reg/roll search (case-insensitive, multiple matches, exact-rank-first),
  no-result empty state, raw-JSON allowlist + credential-string ban, exam-roll
  ≠ class-roll, exam-roll conflict reporting, deleted-student exclusion, status
  filter, DOB-not-searchable, schema/hash/auth DOB-regression.
- **Regressions:** Student Admin + Session + Results + Admit Card + Exam Form +
  Delete + Search + Gate → **263 passed** in one batch; `test_student_admin.py`
  (incl. `test_no_hard_delete_endpoint`), `test_student_results.py`,
  `test_student_session.py` all green.
- **Full suite:** **387 passed** (374 Phase-H baseline + 13 new; 0 regressions).

## J. Browser Test

Playwright + Chrome against the live server (127.0.0.1:8001):
`browser_test_search.py` → **15/15 passed**:

1. Super Admin in → Student Services → Students; Search bar + Search/Clear visible.
2. Student (unique reg/name/roll) created via API; one `StudentResult` seeded
   with a **distinct exam roll**.
3. Search by NAME → row shows Name|Reg|Class Roll|Exam Roll|Course|Semester.
4. **Network inspection**: the raw `/search` JSON captured from the browser is
   allowlist-only and contains no `dob`/`date_of_birth`/`hashed_password`/
   `password`/`session`/`token`/`bcrypt`; JSON exam roll == the result-table
   roll and `!=` class roll.
5. Details card shows exactly the 6 allowed labels/values; **no DOB text** in DOM.
6. Search by Registration Number → found. 7. Search by Class Roll → found.
8. Non-existent value → *"No student found matching your search."*
9. **Clear** → normal Students list restored (verified visually + block toggles).
10. Cleanup: student permanently deleted via the admin API (results removed).

## K. No Other Changes

Confirmed unchanged: Results student-facing workflow, semester allowlist,
examination-roll verification, print/download, import/delete; Admit Card;
Exam Form; DOB authentication + StudentSession + 10-min TTL + logout/expiry;
chatbot/planner/orchestrator/SSE; RAG/Chroma; student permanent delete;
admin RBAC; list pagination/filters; and the delete-feature behaviour
(no live rows of the deleted student appear in search — verified as part of
deletion cleanup in the live DB).