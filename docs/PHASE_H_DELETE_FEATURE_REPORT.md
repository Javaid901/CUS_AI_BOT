# Phase H — PERMANENT DELETE FEATURE (Student + Result)

Scope: **Student Delete** + **Result Delete only. Everything else is frozen.**
Student model, Result model, sessions, auth (DOB sign-in), logout, results
workflow (import / semester+roll verification / print / download), exam roll,
admit card, exam form, datesheet, chat, RAG, planner, Student Services
navigation, imports and university integration were **not touched**.

---

## 1. FK / cascade findings (reported before any code change)

**Declared foreign keys (all reference `students.id`):**

| Child table | FK clause |
|---|---|
| `student_sessions` (`db_models.py:208`) | `ON DELETE CASCADE` |
| `student_results`, `student_admit_cards`, `student_exam_forms`, `fee_receipts`, `student_attendance`, `student_transcripts`, `migration_certificates`, `student_revaluations`, `xerox_requests`, `student_backlogs`, `course_registrations`, `helpdesk_tickets` (`demo_models.py:26` `_fk_col`) | `ON DELETE CASCADE` |
| `grievances.student_id` (`grievance/models.py:74-79`) | `ON DELETE SET NULL`, nullable (grievance keeps `student_name`/`roll_number` copies) |

**Enforcement is INCOMPLETE in the local setup:**

- SQLite does **not** enforce foreign keys here: there is **no
  `PRAGMA foreign_keys=ON`** anywhere in `database.py`/engine creation.
- `Student` has **no parent-side ORM `relationship`** to its children — every
  child defines only a one-way `student = relationship("Student")` without
  `cascade`. So `db.delete(student)` alone would silently leak **orphan rows in
  every child table** (including `student_sessions`).
- PostgreSQL WOULD honor the declared `CASCADE`/`SET NULL` clauses, so local
  behaviour must match it.

**Decision — minimum change, no redesign:** `delete_student` performs explicit,
ordered, transactional cleanup (delete all 12 CASCADE children + sessions,
`SET NULL` grievances, delete the student) so SQLite behaves identically to
what Postgres applies declaratively. No model/schema/engine changes, no global
pragma flip, no soft-delete/archive.

---

## 2. Files changed

| File | Change |
|---|---|
| `backend/app/student_admin/service.py` | Added `delete_student(db, student_id)` — single transaction: delete child rows (results, admit cards, exam forms, fee receipts, attendance, transcripts, migration certs, revaluations, xerox, backlogs, course registrations, helpdesk), delete sessions, `SET NULL` grievances, delete student; `rollback()` on any error; raises `ValueError("Student not found")` for unknown/malformed ids. |
| `backend/app/student_admin/routes.py` | Added `DELETE /api/admin/students/{student_id}` (superadmin-only), emulates the `_value_error` mapping (404), returns `{"deleted": True, "reg_no": ...}`, writes `student.delete` audit. |
| `backend/app/student_results/service.py` | Added `delete_result(db, result_id)` — resolves **exactly one** row by PK (404 for unknown/malformed), returns its snapshot, deletes + commits, rolls back on failure. Sibling rows / student untouched. |
| `backend/app/student_results/routes.py` | Added `DELETE /api/admin/results/{result_id}` (superadmin-only) on the existing `admin_router`, returns `{"deleted": True, "result_id": ...}`, writes `student_result.delete` audit. |
| `frontend/js/admin_students.js` | Added `del()` request helper; replaced the row "Deactivate/Activate" toggle with a **Delete** button; replaced the toggle in the detail footer; added `openDelete(id)` confirmation dialog (warns ⚠ permanent, lists removed data: profile / Results / Admit Cards / Exam Forms / Sessions, "Type **DELETE** to continue", danger submit). |
| `frontend/js/admin_student_results.js` | Added `del()` helper; added an action column with a per-row **Delete** button; added `openDeleteResult(id, row)` dialog showing Student / Semester / Subject, `This action is permanent.`, Cancel/Delete. |
| `backend/tests/test_student_delete.py` | **New** — 8-test battery (see §5). |
| `backend/tests/test_student_admin.py` | Replaced obsolete `test_no_hard_delete_endpoint` (asserted 405) with the Phase H role-guard assertions (admin 403 / student 403 / anon 401). |

Front-end JS verified with `node --check` (both files parse). No lint/build
pipeline exists for the static frontend.

---

## 3. APIs added / modified

| Method | Path | Auth | Behaviour |
|---|---|---|---|
| `DELETE` | `/api/admin/students/{student_id}` | **superadmin only** | Permanent DB delete of the student + all linked rows (cascade table above) + grievance FK nulling. `admin`/`authority`/`student` → **403**, anonymous → **401**, unknown/malformed id → **404**. Response: `{"deleted": true, "reg_no": "<REG>"}`. Atomic (rollback on failure). |
| `DELETE` | `/api/admin/results/{result_id}` | **superadmin only** | Deletes **exactly one** result row — never bulk, never semester-wide, never all-of-student. `403/401/404` as above. Response: `{"deleted": true, "result_id": "<uuid>"}`. |

Existing endpoints (list/search/pagination, create, edit, toggle, reset-dob,
results preview/confirm, demo endpoints, student sign-in/session/logout) are
**unchanged** and were deliberately frozen.

**Deleted-student guarantees (verified):**

- Sign-in (`POST /api/student/verify`): student row gone → generic 401. Inactive
  de-listing check at `student/routes.py:127` also fails closed for any
  remaining edge state.
- Existing cookie: `resolve_session()` re-queries the student
  (`student/session.py:82`) → returns `None` immediately; the session row is
  also deleted. No ghost sessions (13/14 satisfied).
- `/api/student/results/view` → 401 without a session; `/api/student/session`
  → `{"authenticated": false}`.

---

## 4. Audit actions

Written with the existing `audit(db, action, actor_id, actor_role, target, detail, ip)`
helper (own session, swallows errors; exactly the existing pattern).

| Action | target | detail |
|---|---|---|
| `student.delete` | `reg_no` | `Permanently deleted student <reg_no> (student_id=<uuid>) outcome=success` |
| `student_result.delete` | `result_id` | `Deleted result <result_id> (student_id=<uuid>, reg_no=<REG>) outcome=success` |

**Credential hygiene:** DOB, hashes, tokens, and sessions are **never** written
to `detail`/`target` (asserted in tests — banned strings include the DOB,
`2004`, `hashed_password`, `token=`, bcrypt prefix).

---

## 5. Tests added (`backend/tests/test_student_delete.py` — 8 tests)

1. `test_permanent_delete_removes_student_and_all_linked_rows` — seeds one of
   every child (results ×2, admit card, exam form, fee receipt, attendance,
   transcript, migration cert, revaluation, xerox, backlog, course
   registration, helpdesk ticket, a session, a grievance). Delete → student
   gone, **every** child count 0, grievance survives with `student_id=None`
   and preserved `student_name`.
2. `test_delete_student_role_guards_are_server_side` — admin 403, authority
   403, student 403, anonymous 401; attacker call leaves the row intact;
   superadmin delete succeeds.
3. `test_delete_unknown_student_404` — random UUID and garbage id → 404.
4. `test_deleted_student_cannot_login_and_cookie_is_invalid` — sign-in OK, then
   delete → `resolve_session` returns `None` and re-sign-in → 401.
5. `test_result_delete_removes_only_target_row` — after deleting one result the
   sibling result AND the student still exist.
6. `test_result_delete_role_guards_are_server_side` — 403/401 guard matrix;
   row survives attacker calls.
7. `test_delete_unknown_result_404` — random UUID / garbage → 404.
8. `test_deletes_are_audited_without_credentials` — both audit actions present
   with correct actor/role/target/detail and zero credential material.

---

## 6. Full-suite results

| Run | Result |
|---|---|
| `tests/test_student_delete.py` | **8 passed** |
| `tests/test_student_admin.py` + `test_student_results.py` + `test_student_delete.py` | **71 passed** |
| `tests/` **full suite** | **374 passed** (baseline 366 + 8 new; 0 regressions) |

`test_student_admin.py` (student mgmt), `test_student_results.py`,
`test_student_session.py`, exam-form / admit-card / datesheet suites all green.

---

## 7. Browser test (real frontend, Playwright + Chrome, live server :8001)

Script: `C:\Users\LENOVO\AppData\Local\Temp\opencode\browser_test_delete.py`
(no in-tree browser test exists). **15/15 passed**:

1. Super Admin signs in; Student Services mounts.
2. Students → "+ Add Student" creates a unique reg-no student.
3. Results → Import 2-row CSV (Sem 1 "Delete Math", Sem 2 "Delete Science");
   preview reports 2 valid / 0 blocked; list shows exactly 2 rows.
4. Delete the Sem 2 result → dialog shows Student / Semester / Subject +
   "This action is permanent."; after confirm exactly **1 row remains** (Sem1).
5. Students → Delete → dialog lists permanent warnings + affected data
   ("Student profile / Results / Admit Cards / Exam Forms / Sessions");
   a non-`DELETE` value is rejected; typing `DELETE` deletes the student.
6. Admin lists show the student and results gone ("No students found." /
   "No results found."); admit-card and exam-form panes show nothing.
7. Chat widget: sign-in with the deleted reg+dob renders the generic
   "We couldn't verify those details" failure; in-page probes confirm
   `/api/student/verify` → 401, `/api/student/results/view` → 401,
   `/api/student/session` → `{"authenticated": false}`.

Leftover rows from intermediate runs were cleaned from the live DB.