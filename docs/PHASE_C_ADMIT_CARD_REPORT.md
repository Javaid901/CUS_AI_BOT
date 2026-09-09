# Phase C — Student Admit Card Report

## 1. Executive Summary
Phase C adds the authenticated **Student Admit Card** experience to the CUS AI chatbot on top of the green Phase A (Auth + gateway + admin frameworks) and Phase B (Student Results) baselines. Admit cards are **structured records** (course-provider detail: examination centre, session, academic year, reporting time, subjects, instructions, issue date) stored in the existing `StudentAdmitCard` model — **no PDF/file/attachment storage, no status column**. Students access their own cards over a server-validated `cus_student_sid` session cookie; Super Admins manage cards (create / edit / withdraw / CSV & XLSX import with preview → confirm → atomic apply) behind `require_superadmin`. Chat integration reuses the Phase A/B gated-service infrastructure (`gated_families`, semester picker, structured detail events). Full backend regression: **222 pytest passed** (186 baseline + 36 new Phase C); prior suites untouched and green.

## 2. Scope confirmation
- Implemented: authenticated "my admit card" chat + REST for the student, structured Super-Admin management + import, semester picker, admin UI pane, audit, Phase C test battery.
- **Not implemented (by design, per §2 scope lock):** Exam Form, Datesheet/Time-table, Fees/Attendance, Certificates, Notifications, or any new auth/session/JWT/RAG architecture. No unrelated planner re-ordering, and no unrelated backend refactors. Nothing outside the admitted scope was changed.

## 3. Design decisions (carried forward)
- **Representation — structured (Option A).** A card is active by existence; "withdraw" = `DELETE` + audit. Subjects/instructions are JSON arrays of strings in Text columns (demo seeder + imports share this shape).
- **Identity / IDOR.** Student endpoints accept **no** `student_id` / `reg_no` / `roll_no` parameters. The server resolves identity exclusively from the session cookie → `resolve_session` → `student_id`; cross-student reads are structurally impossible (test-tampered params assert A still only sees A).
- **Duplicate policy (app-side).** A card is identified by `(student, semester, exam_type, academic_year, exam_session)` — allowing legitimate repeated entries (regular vs supplementary across sessions). Imports reject in-file duplicates and DB collisions; nothing is written unless the whole file validates.
- **Semester allowlist.** `settings.valid_student_semesters` ({1..8}) is the single source. API → 422; chat → safe "not available" message; admin list/import → 422/validation.

## 4. Backend — new module `app/student_admit_card/`
| File | Role |
|---|---|
| `schemas.py` | `AdmitCardCreate` / `AdmitCardUpdate` (explicit pydantic allowlists; reg_no on create), `ImportBundle` (loose confirm body, re-validated server-side) |
| `service.py` | import parse/validate/apply (csv + xlsx), `student_card_semesters` (one entry/semester, latest wins), `student_card_payload`, list/create/update/delete, `_card_or_404`, app-side duplicate matcher, explicit `_student_dto` / `_admin_dto` allowlists |
| `routes.py` | student `router` (`/api/student/admit-cards`), Super-Admin `admin_router` (`/api/admin/admit-cards`) |

Endpoints:
- `GET /api/student/admit-cards` — own cards (no identity params); optional `?semester=N` → card DTO or `card: null` + safe message; session 401; allowlist 422.
- Admin: `GET` list/search/filter/paginate · `POST` create (201, 409 dup, 404 unknown reg, 422 invalid semester/missing centre) · `PATCH /{id}` (404/409/422) · `DELETE /{id}` (withdraw) · `POST /preview` (writes nothing) · `POST /confirm` (single-transaction apply; 409 duplicate-only / 422 validation, atomic rollback).

Audit (existing `audit` util): `student_admit_card.create|update|delete|import` — filename/reg + count only; never DOB, hashes, tokens, or PII.

## 5. Chat integration (reused gated-service path)
- Planner: `_UNSUPPORTED_SERVICE_FAMILIES["admit_card"] = ("admit card", "admit_card", "admit-card")` and the Rule 2c hub chip `student_admit_card` both route to `student_service/admit_card`; the authed engine branch yields `_admit_card_events`; `_student_src` per-family response_source is applied.
- `gate.py`: `admit_card_semesters_event` (chip id `admit_card_sem-N`) and `admit_card_detail_event` (structured fields incl. enumerated subjects + instructions).
- Semester resolution: `entities.semester` (typed) → chip regex `^admit_card_sem[- ]?(\d+)$` (clicked) → semester picker. Missing semester / no cards → safe tokens, never errors or cross-student hints. Unauthenticated → existing `auth_form`.

## 6. Frontend
- `admin_students.js` — new third sub-nav "Admit Cards" + `#stPaneAdmitCards` pane wiring (`window.CUS.admitCardsInit`).
- `frontend/js/admin_student_admit_cards.js` (new) — list grid (reg/sem/centre/centre-code/reporting/subjects count/issued + Edit/Withdraw), search + semester filter + pagination, create/edit modal (reg on create), withdraw confirm, CSV/XLSX import → preview table with per-row errors → Confirm apply; registers `window.CUS.admitCardsInit`.
- `frontend/pages/admin.html` — script tag for the new module.

## 7. Test battery (`backend/tests/test_student_admit_card.py`, 36 tests)
- Authn: no/expired/revoked/inactive session → 401.
- Student API: own semesters list; card payload is an **explicit allowlist** (blob asserts no `student_id`/`reg_no`/`dob`/`hashed_password`/`token`/`password`); no-card semester safe message; allowlist 422; IDOR tampering ignored.
- Admin: 401/403 matrix; list/search/filter/pagination (422 out-of-allowlist); create 201/409/404/422; update incl. identity-collision 409 + rollback, out-of-range 422, unknown 404; withdraw + student no longer sees it.
- Import: preview writes nothing; valid csv + xlsx; unknown reg; row validation messages; missing required column; in-file duplicate; confirm atomic (bad batch → 422, nothing written); confirm DB-duplicate → 409, nothing written; 409/422 routing; audit blobs clean.
- Chat: hub → picker chips (`admit_card_sem-1/2` present) → chip click → detail; engine typed-semester unit path (detail + no-card token); no-profile safe token; unauthenticated → auth_form.

## 8. Regression
- `python -m pytest tests/test_student_admit_card.py -q` → **36 passed**.
- Phase A + B: `test_student_admin.py`, `test_student_gate.py`, `test_smart_orchestrator.py`, `test_student_results.py` → **70 passed**.
- Full suite `python -m pytest tests -q` → **222 passed** (186 baseline + 36).