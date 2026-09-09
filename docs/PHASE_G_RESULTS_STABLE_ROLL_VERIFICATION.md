# Phase G - Student Results: Server-side Semester Cap (G1) + Stable Examination Roll (G2) - Verification Report

**Date:** 2026-09-08
**Scope:** The approved Student Results workflow change only. Everything else is frozen (auth, StudentSession cookie/TTL/revocation, logout, hub, Admit Card, Exam Form, admin auth, RAG, DB architecture) except `exam_roll_no`.

---

## A. Change Summary

Two coordinated changes close the open gap from the approved results plan:

- **G1 - Server-side semester cap.** The *authoritative* `student.current_semester` now limits (a) the available-semester dropdown offered by the chat and (b) every `POST /api/student/results/view` attempt. Both were previously driven only by what was published, so a published-but-out-of-reach semester could appear in the picker and be attempted.
- **G2 - Stable examination roll number.** The roll the student types is now **stable per student across semesters** (assigned at semester 1, reused later), instead of a per-semester number. This matches the approved prompt contract: exam_roll_no != reg_no != `Student.roll_no`, charset `^[A-Za-z0-9][A-Za-z0-9-]{1,49}$`, travels only in POST bodies.

No DB schema change; no frontend contract change; no new endpoints.

## B. Impacted Files

| File | Change |
| --- | --- |
| `backend/app/student_results/service.py` | `student_semesters(db, student_id, current_semester=None)` and `student_result_view(..., current_semester=None)` now apply the current-semester cap (return `[]` / `None` when the cap is missing or the semester is beyond it). |
| `backend/app/student_results/routes.py` | GET `/results`, POST `/view`, POST `/view/print` resolve the identity's `semester` server-side and pass it into the service functions. |
| `backend/app/orchestrator/engine.py` | `_results_events` threads `(student_session or {}).get("semester")` from the identity dict — the same dict that `student/verify` minted. |
| `backend/app/seeders/demo_data.py` | Seeder assigns **stable** rolls via `f"{yy}{idx:03d}"` (5 digits) at semester 1, reused for later semesters. |
| `backend/app/database.py` | Backfill for existing DBs rewrites `exam_roll_no` to the stable 5-digit form; **NULL-only** (never clobbers already-stable rows). |
| `frontend/js/chatbot.js` | No functional change (contract preserved); the results renderer re-renders in place on "no result", and the post-sign-in auto message resumes the pending family (`engine.py` 402-413). |
| `backend/tests/test_student_results.py` | Rolls updated to stable values; engine direct-call dicts now carry `"semester": 2`; **+3 new G1 tests** (available-list filter; sem-beyond-current rejected even when published; current_semester=1 boundary on a dedicated `client_g`). |
| `backend/tests/test_student_session.py` | Session/roll updates to match the stable contract. |

## C. G1 - Server-side Semester Cap (authoritative)

Invariant: the browser is never trusted with the cap.

- `student_semesters` returns `[]` when `current_semester is None`, else applies `semester <= current_semester` in SQL — the picker can only offer semesters the server says are reachable.
- `student_result_view` returns `None` when `semester > current_semester`, surfacing the exact same safe "No result was found for the selected semester and examination roll number." message as a genuine empty lookup — a bounds probe is indistinguishable from a nothing-published answer.
- The cap travels: `student/verify` (mints identity) -> `identity["semester"]` -> chat `resolve_session` -> engine `_results_events` -> route handlers -> service functions. An attacker toggling `current_semester` in the DB cannot widen the cap (it shuts everything down), and tampering with the cookie/JWT is rejected.

## D. G2 - Stable Examination Roll Number

- Same formula in seeder and backfill: `yy` (last two digits of entry year) + 3-digit student index = **5 digits** (e.g. `23005`, `27001`). Reused by the student in every published semester, matching the approved prompt's stable-roll intent.
- Evidence on the live demo DB: all **455** `student_result` rows rewritten; **0 students** have more than one distinct roll. Example: CUS-2023-0001 -> `23005` used for semesters 1 and 2 (previously a different number per semester).

## E. Regression Suite

Commands and results:

```
cd backend && python -m pytest tests/test_student_results.py -q   -> 40 passed
cd backend && python -m pytest tests/test_student_session.py -q   -> 79 passed
cd backend && python -m pytest tests -q                           -> 366 passed (full suite, ~609s)
```

All 366 remain green after every G1/G2 edit.

## F. Live API Verification (16/16)

Server restarted on 127.0.0.1:8001 (post-change code). Verified against the live `backend/cus_ai.db` via a `requests.Session` (verify + chat share the cookie):

- Admin list shows aarav (CUS-2023-0001) with stable roll 23005.
- Available semesters for current_semester=4: `[1,2,3,4]`.
- `POST /view` sem=1 + roll 23005 -> found, 5 subjects, SGPA 6.60.
- sem=5 -> rejected (same safe unfound answer); wrong roll -> safe unfound.
- Tampered/token-less identity -> 401; print endpoint noindex/no-store/CSP + `Result_Semester_1.html` attachment; print for sem=5 -> 404.
- Chat SSE `results_form` semesters `[1,2,3,4]` (aarav) and `[1,2]` (anjali, current 2); anjali sem=2 roll 24017 found; sem=4 rejected.

## G. Real-Browser Test (Section 32) - 25/25

`C:\Users\LENOVO\AppData\Local\Temp\opencode\browser_test_results.py` (Playwright + system Chrome):

1. Open widget on `/`, anonymous "show my results" -> sign-in gate, **no** result card, **no** roll visible.
2. Sign in through the widget (reg CUS-2023-0001, DOB 15-Apr-2005).
3. Engine **resumes** the pending results family -> single results form auto-rendered, dropdown capped at `[1,2,3,4]` (no 5), roll input empty, no marks leaked.
4. Fill roll 23005 -> submit -> card: Aarav Sharma, CUS-2023-0001, 23005, Semester 1, SGPA 6.60, 5 subjects.
5. "show my semester 5 results" -> safe token "No result is published for Semester 5 yet." (server-side), **no** new marks.
6. Download chip -> `Result_Semester_1.html` attachment with correct filename.
7. Print chip -> popup/endpoint doc, "Statement of Marks", clean of `student_id` / `cus_student_sid` / `hashed_password`.
8. Roll number **never** appears in any URL.

Root cause of the earlier duplicate-form flakiness: the post-sign-in auto "Student Services" message legitimately resumes the pre-login pending family (`engine.py:402-413`), plus the user's own repeated "show my results". Not a bug; the test now uses the single auto-resumed form.

## H. Boundary & Security Invariants (all verified)

- Roll in POST body only - never a URL, analytics, or audit path; identity only from HttpOnly `cus_student_sid` (path `/api/`).
- Legacy GET `?semester=N` -> 422. Roll charset enforced. Safe unfound message identical whether semester is unpublished or beyond current.
- Print headers `X-Robots-Tag: noindex, nofollow`, `Cache-Control: no-store`, `Content-Security-Policy: default-src 'none'; style-src 'unsafe-inline'`.
- Chat endpoint requires a valid student cookie **and** Bearer JWT; 401 without either.

## I. Live DB Data Repair

One-off `fix_live_rolls.py` (already applied): 455 rows rewritten to the stable 5-digit form; 0 students with >1 distinct roll. Examples: CUS-2023-0001/sem1+sem2 = 23005; CUS-2024-0004 (anjali, current 2) = 24017.

## J. Performance / UX Notes

- Cap applied as a cheap SQL predicate / guard - no measurable perf impact.
- Re-opening the results form after a "no result" reuses the same payload (in-place re-render), so users keep the form for retries rather than re-asking.
- Duplicate forms are impossible in the normal single flow; the drop-down appears exactly once after sign-in.

## K. Known Gaps / Deferred (out of scope, unchanged)

- Exam Form / Admit Card still use their own selectors (no change requested).
- `student_semesters(..., None)` deliberately returns `[]` - safe-by-default if a session ever lacks a semester.
- All other families frozen per the instruction.

## L. Reproduce

```
uvicorn app.main:app --port 8001                       # backend (live DB)
python "C:\Users\LENOVO\AppData\Local\Temp\opencode\browser_test_results.py"
python "C:\Users\LENOVO\AppData\Local\Temp\opencode\live_verify_master.py"
cd backend && python -m pytest tests -q
```

## M. Verdict

The Results workflow now enforces the semester cap **server-side** and the examination roll is **stable per student** across semesters, demonstrated end-to-end in a real browser (25/25), on the live API (16/16), and in the full regression suite (366 passed). Every approved promise (identity server-side, roll POST-only, safe unfound message, print hygiene, no URL leakage) holds on the live demo.