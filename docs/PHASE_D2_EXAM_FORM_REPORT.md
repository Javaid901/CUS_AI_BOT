# Phase D2 — Student Exam Form (Exam Session Model) Implementation Report

## 1. Scope Confirmed

Phase D2 extends the Phase D **Student Exam Form** module with a provisioning-first
workflow driven by **Exam Sessions**: a super-admin opens a session per
programme/batch/semester, and students **Fill** (eligibility-gated, system-derived
subjects, server-generated form number) then **Print** (immediate PDF + historical
re-print) their own forms, with a **mock/manual payment** gate before submission.

Strict compatibility constraints honoured throughout:

- Existing legacy behaviour is fully preserved. The old admin per-student
  create/import path, the natural-key identity picker, and the legacy student
  fill/print/submit endpoints keep their exact semantics.
- Existing exam-form tests were kept green; new behaviour is additive. Only new
  tests were added to `test_student_exam_form.py`.
- Nothing unrelated was touched: no changes to results, admit-card, grievance,
  auth/JWT/RAG, or the uncommitted admit-card work elsewhere.
- The existing `GET /api/student/exam-forms/{id}/print` JSON allowlist and the
  picker payload are byte-asserted by legacy tests, so the new document/PDF/payment
  surface is exposed on **new** routes only.

## 2. New Data Model

All in `backend/app/models/demo_models.py` (additive, applied by `create_all()`
plus `_upgrade_schema()` in `backend/app/database.py`):

| Table | Purpose |
| --- | --- |
| `ExamSession` | Provisioned exam window: programme/batch/semester/exam_type/academic_year, `application_open_at`, `last_date_normal`, `last_date_late`, `base_fee`, `late_fee`, lifecycle `status` (Draft/Open/Closed/Archived), server-owned counter `form_seq` |
| `ExamApplicationSubject` | Per-form subjects inserted by the system at fill time (`source="system"`) |
| `ExamEligibility` | Per-form deterministic eligibility snapshot (rules + outcome + `passed`) |
| `ExamPayment` | Payment attempts: initiated / success / refunded, server-owned amount + gateway ref |

`StudentExamForm` gained (additions dict, `_UUID` = CHAR(32) hex on SQLite):
`exam_session_id`, `form_no`, `eligibility_snapshot`, `special_appeal`, `printed_at`,
`late_fee_charged`.

## 3. Files Added / Changed

| File | Change |
| --- | --- |
| `backend/app/student_exam_form/eligibility.py` | NEW — deterministic eligibility + `system_subjects_for` + snapshot rules |
| `backend/app/student_exam_form/payment.py` | NEW — `PaymentBackend`/`MockPaymentBackend`, initiate/confirm/refund/list/snapshot |
| `backend/app/student_exam_form/render.py` | NEW — formal HTML document + one-page A4 PDF (`Clustered University Srinagar`) |
| `backend/app/student_exam_form/exam_session.py` | Session CRUD/DTO, availability, locked `allocate_form_no` |
| `backend/app/student_exam_form/session_routes.py` | NEW — super-admin exam-session endpoints |
| `backend/app/student_exam_form/schemas.py` | `ExamSessionCreate/Update/StatusUpdate`, `StudentFillCreate` (optional `exam_session_id`), `StudentPrintBody`; `StudentSessionPick` removed |
| `backend/app/student_exam_form/service.py` | `_fill_by_session`, submit gate, `student_form_document`, `mark_form_printed`, `student_form_records`, extended `student_form_semesters`/`_admin_dto` |
| `backend/app/student_exam_form/routes.py` | Session-dispatch fill + new document/payment endpoints |
| `backend/app/config.py` | `STUDENT_EXAM_MIN_INTERNAL_PERCENT=40.0`, `STUDENT_EXAM_MIN_ATTENDANCE_PERCENT=75.0`, `STUDENT_EXAM_PAYMENT_MODE="mock"` |
| `backend/app/main.py` | Registered `session_routes` |
| `backend/app/student/gate.py` | Phase D2 event builders (sessions/actions/document/pay) |
| `backend/app/orchestrator/engine.py` | `_exam_form_events` Fill/Print/Pick/Action branching |
| `backend/app/chat/routes.py` | SSE whitelist gained `exam_form_doc`, `exam_form_pay` |
| `frontend/js/chatbot.js` | `exam_form_doc` iframe + `exam_form_pay` mock checkout + print/download/pay actions |
| `frontend/js/admin_student_exam_forms.js` | NEW Exam Sessions tab (create/edit/status/delete) beside Exam Forms |
| `backend/app/seeders/demo_data.py` | Seeds an OPEN `ExamSession` per demo cohort; reset covers session/app tables |
| `backend/tests/test_student_exam_form.py` | +15 new tests (64 total) |

## 4. REST Endpoints

**Student** (identity always from the resolved HttpOnly session cookie):

| Method/Route | Purpose |
| --- | --- |
| `POST /api/student/exam-forms` | Fill — dispatch: legacy identity body OR `{"exam_session_id": …}` |
| `GET /api/student/exam-forms/sessions` | The student's own OPEN sessions picker |
| `GET /api/student/exam-forms/{id}/document` | Formal HTML document render (new) |
| `POST /api/student/exam-forms/{id}/print` | PDF (inline print / attachment), stamps `printed_at` (new) |
| `POST /api/student/exam-forms/{id}/payments/initiate` | Create mock payment (new) |
| `POST /api/student/exam-forms/{id}/payments/{pid}/confirm` | Server-side confirm → `fee_status=Paid` (new) |
| `GET /api/student/exam-forms/{id}/payments` | Own payment history (new) |
| `POST /api/student/exam-forms/{id}/submit` | Payment-gated submission (session forms) |

**Super Admin** (`require_superadmin`, prefix `/api/admin/exam-sessions`):

| Method/Route | Purpose |
| --- | --- |
| `GET ""` | List/search/filter (programme, semester, status) + pagination |
| `POST ""` (201) | Create a provisioned session |
| `GET /{id}` | Session detail + counts |
| `PATCH /{id}` | Edit provisioning data |
| `POST /{id}/status` | Deterministic Draft → Open → Closed → Archived transition |
| `DELETE /{id}` | Delete only when no forms exist (else 409 → archive) |

## 5. Business Rules

- **Session lifecycle:** `Draft | Open | Closed | Archived`; transitions are
  deterministic and super-admin only. Students only ever *read* OPEN sessions that
  match their own programme + current semester + batch.
- **Eligibility is deterministic (no LLM):** programme/batch/semester match, window
  open (`_ensure_utc` safe), internal average ≥ 40%, attendance average ≥ 75%, no
  session duplicate. Evidence-missing rules evaluate to `not_verified` (pass). The
  full rule list + outcome is snapshotted on the form as `eligibility_snapshot`.
- **Subjects are system-derived:** catalogue-first by programme+semester, falling
  back to the student's own `StudentResult` rows; stored as JSON + `ExamApplicationSubject`
  rows with `source="system"`.
- **Form numbers:** `f"{session.code}-{session.semester}-{form_seq:05d}"` (e.g.
  `EXMPG26-3-00539`) allocated under a session row lock, atomically with the form
  write. Students never express a number.
- **Fees are server-owned:** `base_fee` from the session; late fee added when
  `now > last_date_normal`. A zero-fee session auto-pays `ZERO-{form_no}` so it is
  directly submittable.
- **Submit gate uses the snapshot:** session must be Open, eligibility snapshot
  `eligible`, and `fee_status == Paid` when the payable amount > 0. Payment is
  confirmed **server-side** (initiate → confirm); the browser never asserts success.
- **Duplicates:** a student may hold one form per (student, session). The legacy
  identity natural key still applies only to session-less rows, so a session form
  and the legacy flow do not collide.

## 6. Security Contract

- Student identity is always derived from the cookie; foreign ids on document/payment
  endpoints return 403; malformed/unknown sessions and forms → 404.
- Mutation-first rendering (`exam_form_pay`/`submit`) re-checks ownership and state
  server-side; the chat engine wraps mutations in `except ValueError` → safe token.
- Super-admin audit logs record only code/programme/count, never payments or DOB.
- Fees, form numbers and status transitions are functionally unmodifiable by students.

## 7. Chat Integration

New chip ids all keep the `"exam_form"` routing substring so the planner
deterministically lands in `student_service/exam_form`:

- `exam_form_fill` → OPEN-session picker for the student's own profile
- `exam_form_pick{code}` → eligibility check → `student_fill({"exam_session_id"})`
  → detail + actions
- `exam_form_view{dl}{id}` / `exam_form_dl{id}` → `exam_form_doc` event (iframe doc)
- `exam_form_pay{id}` → `exam_form_pay` event (mock checkout with server amount)
- `exam_form_submit{id}` → payment-gated submit
- `exam_form_print` → own numbered-forms picker (immediate + historical print)
- `student_exam_form` (hub) → legacy picker when provisioned forms exist, otherwise
  the Fill/Print landing

`app/chat/routes.py` SSE whitelist now forwards `exam_form_doc` / `exam_form_pay`
frames; `chatbot.js` renders them (mirroring the admit-card document UI) plus a mock
checkout panel that calls REST initiate → confirm → view.

## 8. Test Summary

`backend/tests/test_student_exam_form.py` — **64 tests, all passing**:

- 49 legacy Phase D tests preserved byte-for-byte.
- 12 Phase D2 tests added earlier in the session (session CRUD/status-gate/IDOR,
  session fill derives subjects+fee+snapshot, eligibility blocked on mismatch,
  unpaid submit blocked, initiate→confirm→submit, payment history + IDOR, zero-fee
  auto-paid, draft session hidden, delete guard 409).
- 3 chat flow tests added this session: hub Fill/Print landing (fresh profile),
  `exam_form_fill` → `exam_form_pick` → `exam_form_pay` event (amount 1500),
  `exam_form_view` → `exam_form_doc` event with rendered `EXAMINATION FORM` document.

Regression runs (all green):

- `test_student_gate.py`, `test_student_admin.py`, `test_student_admit_card.py`,
  `test_conversational_ux.py`, `test_conversation_workflow_isolation.py` — **99 passed**
- `test_student_session.py`, `test_intent_matrix.py`, `test_smart_orchestrator.py` — **94 passed**
- Admit-card / results / planner routing untouched and passing.

## 9. Frontend

- `chatbot.js`: `exam_form_doc` renders the formal document in a sandboxed iframe with
  Print / Download (POST `/print`, `as_attachment` flag, stamps `printed_at`);
  `exam_form_pay` renders a mock checkout showing the server-derived fee (base + late)
  and auto-fills zero-fee amounts from the session payload. All JS passes `node --check`.
- `admin_student_exam_forms.js`: new **Exam Sessions** tab beside Exam Forms —
  KPI count, search/status filter + paging, create/edit modal (name, code, programme,
  batch, semester, exam type, academic year, open/normal/late dates, base/late fee,
  status), inline status transitions, guarded delete.

## 10. Demo Data / How to Demo

- `backend/app/seeders/demo_data.py` seeds an **OPEN** `ExamSession` (`DEMO{PROG}{SEM}`)
  per demo cohort (programme + current semester + batch), window now−10d → now+30d,
  base fee `1500 + semester*500`, late fee 300. Reset now also clears
  `ExamSession`/`ExamApplicationSubject`/`ExamEligibility`/`ExamPayment`.
- Flow: Super Admin → Exam Forms → **Exam Sessions** (provision/Open) → student in
  Student Services chat → `exam_form_fill` → `exam_form_pick{code}` → detail →
  `exam_form_pay{id}` (mock checkout) → pay → `exam_form_submit{id}` →
  `exam_form_print` → `exam_form_view{dl}{id}` (PDF).

## 11. Notes / Decisions

- `GET /{id}/print` (legacy JSON) stays byte-identical; the PDF lives at
  `POST /{id}/print` with `StudentPrintBody.as_attachment`.
- Eligibility snapshot stores each rule as `{rule, outcome, passed, message}` and a
  top-level `passed`; browser checks never replace the server-side gate.
- Session picker chips and action chips use lower-cased ids to match the planner's
  lower-cased phrase matching.