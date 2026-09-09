# Phase E — Full Audit Report: Phases A–D (Student Exams, Student Services, Chatbot, Admin)

**Date:** 2026-09-08
**Scope:** Functional, security, backend/API, frontend/UI, chatbot, auth/session, authorization/IDOR, DB/data-integrity, regression, test-quality audit of Phases A (student auth + admin), B (results), C (admit cards), D (exam forms).
**Constraint honored:** No feature development. No Datesheet. No new student services. Architecture preserved. Every claimed "Phase complete" result was re-verified from the code, not trusted from prior reports.

---

## A. Audit Summary

- Every Phase A–D claim in prior reports was re-derived from the current code:
  - **Phase A** — `/api/student/verify|logout|session`, bcrypt DOB-as-password, opaque server-side sessions, superadmin student management.
  - **Phase B** — student-owned results lookup (per-attempt: (semester, examination roll) → `POST /view`; the semester list is capped by the stored `current_semester`; the chat renders a semester+roll form), superadmin results admin.
  - **Phase C** — student-owned admit-card lookup, superadmin admit-card admin.
  - **Phase D** — student-owned exam forms (fill/pick/status), superadmin exam-form admin incl. atomic CSV import + audit trail, chat chips.
- **1 bug fixed (Critical)** — plaintext DOB credential persisted at rest. See section B.1.
- **1 documentation defect fixed (Low)** — stale demo-export docstring misrepresenting the payload. See section B.2.
- All other security contract items verified as already satisfied (section C). No new vulnerabilities found.
- Full regression suite: **272 passed**, 14 pre-existing warnings (section G).
- Net test delta: +1 regression test (`test_plaintext_dob_column_never_stored`).

## B. Bugs Found (and Fixed)

### B.1 — CRITICAL: Student password (DOB) persisted as plaintext in `Student.dob`
- **Location:** `backend/app/models/db_models.py` (column `dob`, String(20)); writers in `backend/app/student_admin/service.py` (`create_student`, `reset_dob_password`), `backend/app/main.py` (`_seed_students`), `backend/app/seeders/demo_data.py` (`_seed_demo_students`).
- **Symptom / impact:** A student's DOB **is** their login password. It was mirrored in clear text next to its own bcrypt hash. Any database leak therefore disclosed live login credentials with no additional work.
- **Root cause:** the `dob` column predates "DOB as password" and was never repurposed; it was written on every credential-create/reset path.
- **Fix (smallest, no schema change):**
  1. `student_admin/service.py` — `create_student`: removed `dob=canonical` (credential hash now `hash_dob(canonical)`); `reset_dob_password`: removed `student.dob = canonical` (hash now `hash_dob(canonical)`).
  2. `main.py` `_seed_students`: removed `dob=s.get("dob")`.
  3. `seeders/demo_data.py` `_seed_demo_students`: removed `dob=s["dob"]`.
  - The column stays (nullable legacy field); it is now never written. The only reader, `seeders/demo_data.py` `_make_student_dict` (`"dob": student.dob or ""`), returns `""` for all new rows and is used only to shape demo service-data seeding (which reads programme/semester, never the DOB).
- **Regression test:** `test_plaintext_dob_column_never_stored` in `tests/test_student_admin.py` — asserts `student.dob is None` after superadmin create **and** after reset-dob, while login still works with the new DOB and fails with the old one.
- **Affected tests re-run:** Phase A battery (22 passed) and, later, the full suite (272 passed).

### B.2 — LOW: stale docstring on `/api/admin/demo/export`
- **Location:** `backend/app/admin/routes.py`.
- **Issue:** docstring claimed "the export includes the student's DOB". The payload demonstrably omits `dob` and `hashed_password` (verified: only non-credential demographics + `can_login`).
- **Fix:** corrected the docstring to accurately state the export is safe by construction.

### B.3 — HIGH: "Admin → Student Services" surfaced HTTP 404 (root cause = stale server process + latent empty-`semester` bug)
- **Reproduction (against the running app):** login as superadmin → open Student Services. Every initial pane request (`GET /api/admin/students`, `.../results`, `.../admit-cards`, `.../exam-forms`) returned `404 {"error":{"code":"NOT_FOUND","message":"Not Found"}}`. The pane partially rendered (header + "Loading…") before the error surfaced.
- **Cause A (the observed 404):** the server process on port 8001 (old PID 8328, started 2026-09-07 08:21) predated the Phase A–D code (student routers written 09-07 09:11–09-07 23:13; `main.py` registration 09-08 07:05). It was running a pre-Phase A–D route table, so none of `/api/admin/students|results|admit-cards|exam-forms` or `/api/student/*` existed. Reproduced against the live process; the current code table (`from app.main import app` → 189 routes) contains all of them; a fresh instance serves them.
- **Cause B (latent bug, verified on fresh code):** the Results / Admit Cards / Exam Forms admin panes always append `&semester=` (an empty value) to the list URL even when no filter is chosen, while the backend types `semester: int | None = Query(None, ge=1)`. FastAPI rejects the empty string → `422` on pane open. (`/api/admin/students` has no int query params, so the Students pane was unaffected on fresh code.)
- **Fix:**
  - Operational: restarted the backend with the current code via the canonical `start_server.ps1` (kills the stale port owner, starts one fresh instance). Old PID 8328 → new PID 7596.
  - Code (shared root cause, one pattern in all three affected loaders): omit `&semester=` when the filter is empty — `admin_student_results.js`, `admin_student_admit_cards.js`, `admin_student_exam_forms.js`; bumped each script's cache-buster `?v=1` → `?v=2` in `admin.html` so browsers drop the cached bundle. Filter semantics unchanged when a semester is selected.
- **Verification (live, port 8001, current code):** all four pane requests now return `200`; filtered variants (`semester=1|2|3`) return `200`; `POST /api/student/verify` now validates (route live). `node --check` clean on all three edited files. Backend regression battery (student_admin + gate + results + admit_card + exam_form + orchestrator) re-ran: **156 passed**. No business logic, authn, authz, schema, or audit behavior changed.

## C. Security Findings (verified, no change required)

Authentication & credentials
- reg_no + DOB is the **only** student credential flow and happens **only** at `POST /api/student/verify` — `/api/chat/ask` never receives reg/DOB; it resolves the cookie server-side (`chat/routes.py`, `resolve_session`).
- DOB normalisation to canonical `YYYY-MM-DD` is single-sourced in `student/dob.py` (`normalize_dob`, `hash_dob`) and used identically at create, reset and verify — two directions cannot drift.
- `verify` returns one generic 401 for unknown reg, wrong DOB, unparseable DOB and inactive students; unknown-reg and unparseable paths run a dummy bcrypt verify to equalise timing (`student/routes.py`, `_dummy_hash`).
- Rate limit: **5 req/min/IP** on `/verify` in a dedicated `student_verify` bucket (`endpoint_rate_limit`), separate from the chat limiter.

Sessions
- Tokens are `secrets.token_urlsafe(32)`; **only the SHA-256 hash** is stored (`student/session.py`); a DB leak yields no usable sessions.
- Cookie is HttpOnly, path `/api/`, SameSite=lax, `secure` honours `settings.cookie_secure`; 60-min TTL enforced server-side.
- Expiry/revocation/inactivity all reject server-side on every chat request and on every student-results/admin session endpoint (`resolve_session`); the browser is never trusted.

Authorization / IDOR
- Student results, admit cards and exam forms all derive the owning `student_id` **only** from the resolved session — no client-supplied id enters any WHERE clause. IDOR-safe by construction.
- Semester/exam-type chips are re-validated against per-service allowlists; unknown values produce safe "no data" text, never errors or hints about other students.
- Student DTOs are explicit allowlists; `dob`, `hashed_password`, `transaction_id`, session material and `student_id` are excluded everywhere (incl. `detail`/`options` SSE payloads and admin list/detail responses).
- All student-results / admit-card / exam-form / student-manage admin routes use `require_superadmin`. Ordinary `admin` gets 403 on every destructive/credential surface (asserted by tests).
- Demo endpoints: `seed`/`reset`/`regenerate`/`export`/`delete` are `require_superadmin`; `demo/status` (counts only) is `require_admin`. Export omits DOB + hash.
- `hashed_password` is referenced only by login/profile-password logic — never returned by any DTO, export or audit row (grep-verified across `app/`).

Audit & analytics hygiene
- `audit()` rows record outcome only — never reg number, DOB, token or hash; banned-token assertions exist in the Phase A–D tests and pass.
- Chat SSE events for exam forms withhold `transaction_id` from the student-facing payload.

Not fixed (documented design/Low items)
- **Low:** `frontend/js/chatbot.js` persists an auto-issued **guest** credential (`cus_auth` = `{user, pass, token}`) in `localStorage` for transparent re-auth after 401/refresh. The credential is a machine-generated random guest account — not student PII, not the DOB. Inherent XSS-exfiltratable-by-design, but scoped to guest chat. Not a confirmed bug; left to future hardening (token-only storage + re-register on 401).
- **Low/informational:** `frontend/js/admin_students.js` edit modal renders a disabled, always-empty "Date of Birth" field because the detail DTO intentionally withholds DOB; the modal's own explanatory note covers this. Design decision, unchanged.

## D. Functional Findings (per service + chatbot)

- **Student verify/session (Phase A):** behaviour matches the approved Step-1 contract. Case-insensitive reg lookup; any accepted DOB input format verifies; old `{reg_no, password}` contract returns 422. Verified by tests.
- **Results (Phase B):** semester resolution order (typed → chip → picker), all personal data serviced from the session; no cross-student queries. Admin import preview is read-only; confirm is atomic.
- **Admit card (Phase C):** same IDOR-safe shape; structured card with centre/exam-session fields.
- **Exam forms (Phase D):** statuses `Pending→Submitted→Approved→Rejected→Withdrawn`; student transition is `Pending→Submitted` only and stamps `submission_date`; natural keys enforced app-side (`student, semester, exam_type, academic_year`); student fill allowlist excludes fee/payment/transaction fields; admin preview writes nothing, confirm atomic; audit actions (`create|update|status_change|delete|import|fill|submit`) present.
- **Chat (engine/planner):** gated families route to `student_service` only; `exam_form{tid}{semester}` chips parse back via `_EXAM_OPTION_ID`; unsupported personal services that must not look fake return a plain "unavailable" text or the auth gate — never a fake lookup. Verified by `test_smart_orchestrator` + `test_intelligence`-style gating assertions.
- **Frontend:** all JS files pass `node --check`. Chat sign-in gate posts credentials solely to `/api/student/verify`; no credential material enters chat history, analytics or `localStorage`. Exam-form admin module (list/KPI, create/edit, status modal, withdraw, import preview→confirm) matches `_admin_dto` and list/preview/confirm response contracts.

## E. Files Changed

| File | Change |
|---|---|
| `backend/app/student_admin/service.py` | Stop writing plaintext `Student.dob` on create + DOB reset; hash via `hash_dob(canonical)` |
| `backend/app/main.py` | `_seed_students` no longer writes `dob` |
| `backend/app/seeders/demo_data.py` | `_seed_demo_students` no longer writes `dob` |
| `backend/app/admin/routes.py` | Corrected demo-export docstring |
| `backend/tests/test_student_admin.py` | Added `test_plaintext_dob_column_never_stored` regression test |

## F. Database Changes

- **None.** No migration, no DDL change. `Student.dob` remains as a nullable legacy column but is no longer written on any path.
- **Operational follow-up (see H):** one-time scrub of any historical plaintext values: `UPDATE students SET dob = NULL WHERE dob IS NOT NULL;` (recommended at deployment/maintenance window).

## G. Tests

- Phase A (`test_student_admin`): **22 passed** (incl. new regression).
- Phase gate (`test_student_gate`), Phase B (`test_student_results`), Phase C (`test_student_admit_card`), Phase D (`test_student_exam_form`), orchestrator regression (`test_smart_orchestrator`): green.
- Combined Phase A–D + regression battery: **156 passed** in 67s.
- **Full suite (`pytest tests`): 272 passed, 14 warnings, 447.76s.**
- Syntax: `python -m py_compile` over all 171 app/tests files — OK.
- Frontend: `node --check` over all of `frontend/js/*.js` — OK.
- Lint: `ruff check app tests` → 2600 errors, **all pre-existing** (import sorting, function-local imports, unused def-args, docstring rules from the repo's `ALL` selector). The audit's edits introduced no new rule category (only line deletions and a `canonical` re-use). Ruff is configured but not enforced by the suite.
- The 14 warnings are the known pre-existing set (FastAPI `on_event`, starlette/httpx `TestClient` deprecation, pydantic class-based `config`, chromadb telemetry, per-request-cookies deprecation) — unchanged.

## H. Remaining Issues / Follow-ups

- **Business rule (already known, not a bug):** Rejected/Withdrawn exam forms cannot be re-filled by the student — policy decision, deliberately untouched.
- **Operational:** run the `Student.dob` scrub SQL (section F) against any production DB that predates this fix.
- **Low/design (unchanged):** guest chat credential in `localStorage` (`cus_auth`); empty disabled DOB field in the admin edit modal.
- **Pre-existing debt (unchanged):** ruff config not enforced (2600 findings); deprecation warnings listed above; per-service natural-key uniqueness is enforced app-side only (no DB unique constraints) — a deliberate Phase A–D tradeoff, not a defect.
- **Out of scope:** Datesheet and any new student services (as instructed).

---

## I. Follow-up — Fixed 10-minute Student Services Session + Deterministic Chat Logout

**Date:** 2026-09-08
**Requirement:** A Student Services sign-in must never last longer than 10 minutes (server-authoritative), and any of "logout / log out / sign out / sign me out / log me out" typed in chat must log the student out immediately — deterministically, before the LLM.

### Required-item report

| # | Item | Answer |
|---|------|--------|
| 1 | TTL before change | `STUDENT_SESSION_TTL_MINUTES = 60` (config was set at 60 since Step-1) |
| 2 | Final TTL | `10` minutes (`backend/app/config.py`, single setting; the `cus_student_sid` cookie `Max-Age` = 600s follows automatically) |
| 3 | Fixed vs sliding | **Fixed.** `expires_at` is written exactly once at login (`create_session`); nothing on any path extends it — verified by tests that re-resolve and probe over the real chat + `/api/student/session` paths and assert the DB `expires_at` is byte-identical afterward |
| 4 | Exact logout detection mechanism | New pure-text detector `backend/app/student/logout.py`: lowercases, collapses punctuation, normalizes whitespace, strips an optional leading "please"/"kindly" and trailing "from/of student services", then full-string membership in `{logout, log out, sign out, sign me out, log me out}`. No substring matching → `"what does logout mean"`, `"how do i log out"` etc. never log anyone out. Runs in `chat/routes.py` **before** the Admission Controller and the LLM |
| 5 | Exact logout endpoint | Reuses the existing Student Services mechanism: `revoke_session()` + `response.delete_cookie(cus_student_sid, path="/api/")` — the same code the existing `POST /api/student/logout` uses. For a typed chat command the same two operations happen server-side in `POST /api/chat/ask` and the cookie is cleared on the SSE response. No second/new logout endpoint |
| 6 | Server revokes session? | **Yes.** The `StudentSession` row is set `revoked=True` (matched by SHA-256 token hash) before any streamed content; a revoked/expired row can never re-resolve even if the browser re-sends the cookie. The chat logout is idempotent when not signed in |
| 7 | Cookie behavior | Logout sets `Set-Cookie: cus_student_sid=""; Max-Age=0; Path=/api/` (mirrors the logout endpoint). The cookie is HttpOnly, `Secure` honors `cookie_secure`, `SameSite=lax`, path `/api/`. After logout the browser holds no usable session; the frontend also drops its transient sign-in widget state on the new `logout` SSE event |
| 8 | Files changed | `backend/app/config.py`, `backend/app/student/logout.py` (new), `backend/app/student/gate.py`, `backend/app/orchestrator/engine.py`, `backend/app/chat/routes.py`, `frontend/js/chatbot.js`, `backend/tests/test_student_session.py` (new), `docs/PHASE_E_AUDIT_REPORT.md` |
| 9 | Tests added | New `test_student_session.py` battery — **60 tests**: config=10min/600s cookie; login creates a ~600s session; fixed TTL not extended by resolve, chat, or `/session` probes; server-side expiry (stale cookie → 401 + "Your Student Services session has expired..." gate, then re-login restores access); logout endpoint revokes + clears cookie; chat logout command + 11 variants revoke + confirm + clear + `logout` event; not-signed-in logout is idempotent and leaves the chat JWT/guest identity intact; normal chat never revokes; detector accept/reject unit matrix; audit records outcome only |
| 10 | Full regression result | `pytest tests` → **332 passed** (272 pre-existing + 60 new), 14 warnings, 535s. student gate/results/admit-card/exam-form suites 186 passed; orchestrator/UX suites 34 passed. `py_compile` clean; `node --check` on `chatbot.js` clean. **Live smoke on port 8001** (restarted server, now PID 11228): verify → `Max-Age=600` HttpOnly `/api/` cookie; pre-logout `session` authed; chat "logout" → confirmation + `event: logout` + `event: done` + cookie `Max-Age=0`; post-logout `session` unauthenticated; `results` → 401; "sign me out" while signed out → idempotent message; "what does logout mean" → no logout event |

### Security notes (unchanged invariants preserved)

- **No new session table, token system, JWT, or localStorage student state.** Still only `StudentSession` + the HttpOnly `cus_student_sid` cookie; sessions remain opaque, SHA-256-hashed at rest, short-lived, revoked server-side.
- Verification still happens **only** at `POST /api/student/verify`; the chat path only ever re-resolves the cookie against the DB (browser never trusted).
- Chat logout revokes **only** the Student Services session; the admin JWT and the guest `cus_auth` credentials are untouched (tested).
- The expired-session case uses the existing sign-in gate (`auth_form`) with an explicit "Your Student Services session has expired. Please sign in again." message; no credential material in chat streams, analytics, or audit detail (tested).

---

## J. Follow-up — Logout / Session-Expiry UX Fix

**Date:** 2026-09-08
**Requirement:** User-facing logout and expired-session behavior (no auth-architecture redesign): a manual logout shows exactly "Logged out successfully." and never opens the sign-in window immediately; an expired session shows exactly "Your Student Services session has expired. Please log in again." followed by the existing login form; the two cases are distinct ("…has ended. Please log in again." after an explicit logout — never a false "expired"); access after logout/expiry re-prompts with the auth form; the original request resumes after a successful re-login where practical; the old revoked/expired session can never be reused.

### What changed (smallest surface, no redesign)

- **Messages now exact and distinct** (`backend/app/student/gate.py`):
  - `expired_gate_message()` → `"Your Student Services session has expired. Please log in again."`
  - `logged_out_gate_message()` → `"Your Student Services session has ended. Please log in again."`
  - `auth_gate_message()` (first-login) unchanged — no family-name suffixes on any of them.
- **Chat logout confirmation** is now exactly `"Logged out successfully."` (`chat/routes.py`); not-signed-in logout still says `"You are not signed in to Student Services."` The logout response revokes the session, clears `cus_student_sid`, emits the `logout` SSE event, and (new) sets a **non-credential UX marker cookie** `cus_student_out` (`Max-Age=3600`, HttpOnly, `Secure` honors `cookie_secure`, `SameSite=lax`, path `/api/`) so the next gated request can say "ended" rather than falsely claiming expiry. The marker grants nothing — access still requires a valid session cookie.
- **Expiry vs logout vs fresh, server-side** (`chat/routes.py` + new `classify_stale_session()` in `session.py`): with no cookie the gate uses the generic first-login ask; with a cookie that does not resolve it classifies *wording only* — `revoked` (row exists & revoked) → "ended", otherwise → "expired"; access enforcement is unchanged (`resolve_session` is the sole authority). `POST /api/student/verify` success clears the marker; `POST /api/student/logout` sets it.
- **Resume original request** (`engine.py`): when an already-authenticated `student_service` turn would show the bare hub, a previous gate that stored `stage=auth` with `family ∈ {results, semester_result, admit_card, exam_form}` is honored — so after re-login the frontend's "Student Services" call continues into the originally requested family (data always resolved against the fresh session; credentials still never flow through chat).
- `frontend/js/chatbot.js` unchanged — its existing `logout` event handler (reset `SSF.mode/family`, remove panel) and `auth_form` handler (open `ssfOpen`, dedupe) already meet the requirements.

### Required-item report

| # | Item | Answer |
|---|------|--------|
| 1 | Direct logout message | Exactly "**Logged out successfully.**" — test asserts `_token_text == "Logged out successfully."` |
| 2 | Expired message | Exactly "**Your Student Services session has expired. Please log in again.**" — asserted verbatim (no family suffix) |
| 3 | Same login widget | Reuses the existing `auth_form` → `ssfOpen` → `POST /api/student/verify` flow; no new "login" message/type |
| 4 | No immediate login on logout | Logout response contains the confirmation + `logout` event + `done` only; `auth_form` never appears in the same response (tested) |
| 5 | Expiry authoritative | `resolve_session()` on every `/api/chat/ask`; a time-expired row is rejected even with the cookie; an HTTP 401/error is never the chat response — the gate flow is used instead |
| 6 | Distinct logout vs expiry | `"…has ended. Please log in again."` (logout/revoked) vs `"…has expired. Please log in again."` (lapsed) vs first-login ask — asserted mutually exclusive; internal reasons (revoked vs invalid) never exposed |
| 7 | After-logout chat state | No student identity/credentials/token retained in chat state; transient widget state cleared by the `logout` event; conversation state never carries credentials |
| 8 | After expiry | Stale service state reset, auth form re-opens, successful re-login mints a NEW `StudentSession` (row count +1, prior row revoked/expired unusable — tested) and the student can use the services again |
| 9 | Resume original request | Implemented without orchestrator redesign: post-login "Student Services" (same `chat_id`) continues the previously gated family (results/admit card/exam form); verified by a session-level test asserting the pending service actually opens (Results → `results_form`, admit card → `admit_card_sem-N`, exam form → `exam_form{type}{sem}`) |
| 10 | Tests added | 74 tests in `test_student_session.py` (60 prior + **14 new**: logout success event/revocation/cookie-clear/marker/no-auth-form; idempotent no-marker logout; per-service ended-message+login-window after logout for results/admit-card/exam-form; exact expired message; fresh browser never claims expiry; re-login mints new session + resumes request; marker cleared by re-login; no marker from pure expiry; no data/token leak on gated requests; revoked-but-present cookie → ended) |
| 11 | Security | Marker is non-credential (grants nothing); streams/audit never contain credentials, tokens, DOB, or `cus_student_sid`; revoked/expired rows can never re-resolve; no changes to admin auth, RAG, DB schema, or business logic |

### Verification (exact results)

- `py_compile` clean over all edited files.
- `pytest tests/test_student_session.py` → **74 passed** (+14 new).
- Related suites (`test_student_gate.py`, `test_student_results.py`, `test_student_admit_card.py`, `test_student_exam_form.py`, `test_student_admin.py`, `test_smart_orchestrator.py`, `test_intelligence.py`, `test_conversational_ux.py`) → **173 passed**.
- **Full suite** `pytest tests` → **346 passed** (272 Phase A–D baseline + 74 session/logout/UX), 14 pre-existing warnings, 446s.
- `node --check frontend/js/chatbot.js` → OK.
- **Live smoke on port 8001** (server restarted to PID 10932): authed `show my results` → options, no gate; chat `logout` → "Logged out successfully." + `event: logout` + `Set-Cookie: cus_student_sid Max-Age=0` + `cus_student_out Max-Age=3600`, no `auth_form`; `show my results` after logout → "Your Student Services session has ended." + `event: auth_form`, no picker/fields leaked; re-verify → marker cleared, same conversation "Student Services" accepted without re-gate. Throwaway data purged after the run (student count back to 28).
- **Limitation (reported per instruction):** resume is supported for results / semester-result / admit card / exam form. If the pre-login request is something else (e.g. the bare hub or future service), re-login lands on the authenticated hub instead. No orchestrator redesign was performed.