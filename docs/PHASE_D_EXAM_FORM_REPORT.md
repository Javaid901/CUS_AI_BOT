# Phase D — Student Exam Form (Report)

## 1. Scope Confirmed

Phase D adds a complete **Student Exam Form** module on top of the existing Phase A/B/C
roles, sessions, chat orchestrator and Super-Admin management. It reuses the existing
`StudentExamForm` model — **no schema change** was made and no speculative columns
(e.g. `remarks`) were added.

Out of scope (unchanged by this phase): Datesheet/DSE cards, payments gateways,
attendance/leave, certificates, notifications, auth/session/JWT/RAG, student results,
admit cards.

## 2. Workflow (as implemented)

- A Super Admin provisions exam-form records (single form or CSV/XLSX import) with
  semesters, exam types, fee/payment details and lifecycle status.
- A student, signed into Student Services, sees their own available forms, **fills** a
  missing one (semester + exam type + academic year + subjects only), **prints** their
  own form, and **submits** it (Pending → Submitted), which stamps `submission_date`.
- The Super Admin later drives the lifecycle: Approved / Rejected / Withdrawn, and
  records fee/payment data.
- Chat integration: the existing planner Rule 2c + Rule 10a gate route "student exam
  form" questions into the exam-form family. A gated picker lists the student's own
  fillable semesters; typing a chip (e.g. `exam_formregular2`) renders that form's
  detail. `_UNSUPPORTED_SERVICE_FAMILIES["exam_form"]` gained the `"exam_form"`
  (underscore) phrase so chip ids route deterministically.

## 3. Files Added / Changed

| File | Change |
| --- | --- |
| `backend/app/student_exam_form/__init__.py` | New package init |
| `backend/app/student_exam_form/schemas.py` | Exam types, import bundle, admin create/update, student fill, submit, status-change bodies |
| `backend/app/student_exam_form/service.py` | All business logic (~780 lines) |
| `backend/app/student_exam_form/routes.py` | Student + Super-Admin REST endpoints |
| `backend/app/main.py` | Registered both exam-form routers |
| `backend/app/student/gate.py` | Added `exam_form_picker_event` and `exam_form_detail_event` |
| `backend/app/orchestrator/engine.py` | `_exam_form_events` branch + `_EXAM_OPTION_ID` regex in the authed family dispatch |
| `backend/app/orchestrator/planner.py` | `"exam_form"` added to the unsupported-family phrase tuple |
| `backend/tests/test_student_exam_form.py` | Full Phase D battery (49 tests) |
| `frontend/js/admin_student_exam_forms.js` | Super-Admin UI module (new) |
| `frontend/js/admin_students.js` | Fourth sub-nav button + pane + integration |
| `frontend/pages/admin.html` | Script tag for the exam-forms module |

## 4. REST Endpoints

**Student** (prefix `/api/student/exam-forms`, session cookie must resolve; identity is
always server-derived, never a client parameter):

| Method/Route | Purpose |
| --- | --- |
| `GET ""` | Own form picker (list of own fillable forms) |
| `POST ""` (201) | Fill: create own Pending form (semester/exam_type/academic_year/subjects only) |
| `POST /{id}/submit` | Affirm → Pending → Submitted, stamps submission date |
| `GET /{id}/print` | Printable payload of the student's OWN form (403 on anyone else's) |

**Super Admin** (prefix `/api/admin/exam-forms`, `require_superadmin` on every endpoint):

| Method/Route | Purpose |
| --- | --- |
| `GET ""` | List / search (reg no or name) / filter by semester & status / paginate |
| `POST ""` (201) | Create a form record (reg_no resolves server-side) |
| `PATCH /{id}` | Edit administrative data |
| `POST /{id}/status` | Deterministic status transition |
| `POST /preview` | Upload CSV/XLSX → validate (writes nothing) |
| `POST /confirm` | Apply validated rows in ONE transaction (rollback on any error) |
| `DELETE /{id}` | Withdraw a form (audited) |

## 5. Business Rules

- **Status lifecycle:** `form_status ∈ {Pending, Submitted, Approved, Rejected, Withdrawn}`.
  The ONLY student transition is `Pending → Submitted` (`submission_date` = `%d-%b-%Y`);
  all other transitions are Super-Admin only.
- **Semesters:** 1–8, enforced server-side (`valid_student_semesters`).
- **Exam types:** `{Regular, Backlog}`.
- **Natural key / duplicates:** `(student_id, semester, exam_type, academic_year)`.
  Create, fill and import all reject duplicates with 409; imports fail atomically and
  write nothing on error.
- **Fee/payment fields** (`fee_status`, `fee_amount`, `transaction_id`,
  `submission_date`) exist ONLY in Super-Admin schemas — students structurally cannot
  forge payment state.

## 6. Security Contract

- Student identity is ALWAYS derived from the resolved `StudentSession` — no
  `student_id` / `reg_no` / `roll_no` is accepted as input (IDOR-safe).
- Print/submit re-check `form.student_id == session student_id` on every owned-row
  operation; foreign ids return 404/403.
- Student DTO allowlist: `id, semester, exam_type, academic_year, subjects,
  form_status, submission_date, fee_status, fee_amount` — never reg_no/student_id/
  transaction_id.
- Import re-validates every row inside `confirm`; duplicate-only failures → 409, other
  failures → 422; commit is atomic.
- Audit actions (`student_exam_form.create|update|status_change|delete|import|fill|submit`)
  record only safe metadata (reg/sem/filename/count) — never DOB, credentials, payment
  transaction ids or unrelated data.
- Every admin endpoint is gated by `require_superadmin`; the admin DTO expands only
  reg_no/roll_no/name for display.

## 7. Chat Integration

- Picker event keys the gate filter; chip ids `exam_form{tid}{semester}` (e.g.
  `exam_formregular2`) are parsed by `_EXAM_OPTION_ID = ^exam_form([a-z]+)(\d+)$` in the
  engine and render the owned form's detail via `student_form_by_identity` +
  `student_dto`. Unauthorized/unresolved form tokens produce a safe fallback event, and
  the authed family reverts to `hub_options` afterwards.

## 8. Test Summary

- `backend/tests/test_student_exam_form.py` — **49 tests** (authn 401 matrix,
  own-list isolation, allowlist privacy, fill→submit transitions, duplicate/IDOR/tamper
  rejection, admin authz matrix, CRUD roundtrips, CSV+XLSX preview/confirm atomicity,
  chat picker→chip detail, safe no-form tokens).
- Regression battery (student gate, orchestrator, results, admit card, student admin) —
  **106 tests** all passing.
- **Full suite: 271 passed, 14 warnings (527s)** — 222 baseline + 49 new.

## 9. Frontend

- New `frontend/js/admin_student_exam_forms.js` module: list with search/filter/
  pagination + KPI count, create/edit modal, status-transition modal, withdraw, and a
  CSV/XLSX import flow with `preview` (blocks bad rows) → `confirm` (applies once).
- Fourth sub-nav button + `stPaneExamForms` pane added to `admin_students.js`; script
  tag added to `admin.html`. All three JS files pass `node --check`.

## 10. Unresolved Business Rules (informational)

- Whether a Rejected/Withdrawn form should be re-fillable by the student (re-opens the
  natural-key space) is not decided; the current implementation keeps those statuses
  terminal and blocks re-fill until an admin changes the status or withdraw is used.
- Semester-rollover behavior (which year a form belongs to) is represented but not
  auto-computed; admins supply `academic_year`.