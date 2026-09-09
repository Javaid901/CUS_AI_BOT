# Phase F — Production Integration Readiness Audit: Student Services (Demo → University Data)

**Date:** 2026-09-08
**Scope:** Architecture audit ONLY of the Student Services subsystem — authentication, sessions, student records, results, admit cards, exam forms, chat flow, Super Admin management, service layers, and frontend/backend contracts — to determine whether the current demo architecture can evolve into a production University-integrated Student Services system.

**Constraints honored (per instruction):**
- No connection to any external University system.
- No removal or change of demo functionality.
- No database schema change.
- No speculative integration code.
- No invented University API endpoints, tables, authentication protocols, field names, credentials, payment systems, result schemas, or exam-workflow assumptions.
- Demo data continues to work exactly as today; it is never replaced by hard-coded mock values.

---

## 0. Executive Summary — Direct Answer to the Key Question

> **"If tomorrow the University provides an official API or controlled database access containing real student data, what exact parts of the current system would need to change?"**

**Answer:**
1. **What can remain unchanged:** all HTTP/frontend contracts, the chat orchestrator gating, the SSE gate/render events, StudentSession (cookie + TTL + hashing + revocation), the Super Admin demo CRUD/import screens, the demo seeder, the DB schema, and the entire frontend.
2. **What can be reused:** the six student-facing service functions that are already the data boundary (`student_semesters`, `student_results_payload`, `student_card_semesters`, `student_card_payload`, `student_form_semesters`, `student_form_by_identity`), the exam-form fill/submit/print flow, the session cookie architecture, the SSE event shape rendering.
3. **What requires an adapter/provider (new code, not a rewrite):** a thin Student Data Provider that, for each family, serves the *same* DTO shapes from the University source instead of the local ORM tables; plus an authentication adapter that replaces the reg-no+DOB bcrypt check at `/api/student/verify` with the University's official mechanism but still mints the same application-side `StudentSession`.
4. **What requires field mapping:** all University-provided record shapes → the current canonical output shapes (semester-picker lists, subject rows with internal/external/total/max/grade/sgpa/cgpa/status, admit-card centre/instructions, exam-form status/fee fields).
5. **What depends on the University's authentication mechanism:** only the `/api/student/verify` credential check (`student/routes.py`, `student/dob.py`). Everything downstream (chat gate, Results/Admit Card/Exam Form data flows) depends solely on a valid application-side session dict and does not care how it was obtained.
6. **What depends on the University's database/API structure:** only the provider adapter and its mapping layer — never routes, orchestrator, gate, or frontend.

**Verdict:** The architecture is **integration-ready in shape**: identity is always server-side, all data reads already flow through six stable service functions, there is zero ORM in routes or the orchestrator, and no client-supplied identity is ever trusted for authorization. The future change is *additive* (new provider + adapter + mapping), not a rewrite. Two genuine design decisions (not code changes) must be settled when official University documentation arrives: **(a)** the identity-anchor strategy for `StudentSession.student_id` (FK → local `students`), and **(b)** how demo rows and University rows coexist (row provenance) — see §12.

---

## 1. Current Student Services Data Flow (Demo)

```
                      ┌────────────────────────────────────────────────────────────┐
                      │                     LOCAL DATABASE (SQLite/Postgres)        │
 SUPER ADMIN          │  students  student_results  student_admit_cards            │
 (admin UI +          │  student_exam_forms  (+ 9 dormant demo tables)             │
  demo manager) ────► │                                                            │
                      └────────────────────────────────────────────────────────────┘
                                      ▲                        ▲
                                      │ ORM (service.py only)  │ ORM (service.py only)
        REST endpoints                │                        │ Chat path
 /api/student/results                 │                        │ /api/chat/ask  ──► engine
 /api/student/admit-cards             │                        │     │ (auth gate)
 /api/student/exam-forms ─────────────┘                        │     ▼
                                      ▲                        │  _results_events
 /api/student/verify (mints session)  │                        │  _admit_card_events
                                      │                        │  _exam_form_events
   cookie cus_student_sid (HttpOnly)  │                        │     │
 resolve_session() ──► identity dict  │                        │     ▲
                                      └────────────────────────┴─────┘
                                 all identity server-side; client never supplies
                                 student_id/reg_no for authorization
```

- **HTTP path:** every `/api/student/*` endpoint resolves the cookie → `resolve_session()` → `student_id`, then calls one of the service functions (§1 tables in the data-layers audit). Verify only path is `/verify|logout|session` (`backend/app/student/routes.py`).
- **Chat path:** `/api/chat/ask` resolves the cookie (chat/routes.py:94–112), handles pre-LLM chat logout, then calls `process(db, uid, msg, cid, student_session, student_auth_kind)`; the engine's `student_service` branch (engine.py:375–448) gates on `student_session` and dispatches to `_results_events`/`_admit_card_events`/`_exam_form_events` (engine.py:731/790/849), which call the same service functions.
- **Provisioning:** Super Admin either (a) uses the admin UI CRUD + CSV/XLSX import endpoints for `students`/`student_results`/`student_admit_cards`/`student_exam_forms`, or (b) triggers the demo manager (`/api/admin/demo/seed|reset|regenerate|export|delete`). Demo seeding writes into the **same operational tables** the student-facing paths read.

---

## 2. Current Authentication Flow

```
 Student (browser)
   │  reg_no + DOB (sent ONLY to /api/student/verify)
   ▼
 POST /api/student/verify                      backend/app/student/routes.py:79
   ├─ normalize_dob (dob.py:38) → canonical YYYY-MM-DD
   ├─ db.query(Student).filter(upper(reg_no))  (routes.py:108)      ← local-DB dep (A)
   ├─ verify_password(canonical_dob, student.hashed_password)       (routes.py:122)
   │     └─ bcrypt (auth/security.py:37/42)  [timing-equalised for unknown reg/inactive]
   ├─ is_active + status=="active" checks                          (routes.py:127)
   ├─ create_session(db, student) → raw token; DB stores SHA-256   (session.py:38)
   └─ Set-Cookie cus_student_sid (HttpOnly, 600s, /api/)          (routes.py:133–142)

 Every subsequent request (chat + /api/student/*):
   cookie ──► resolve_session(db, raw)  (session.py:54)
     ├─ StudentSession lookup by hash_token  (reject revoked/expired)
     └─ Student lookup: is_active/status     (session.py:82)       ← local-DB dep (A/B)
   returns identity dict {student_id, name, reg_no, programme, semester}
   (PII-bounded — never email/DOB/session row)
```

**Is the auth logic isolated enough to substitute the University authentication mechanism?**
- **Yes, at one seam.** The substitution point is a single endpoint branch (`/api/student/verify`): replace the "lookup local Student by reg_no + verify bcrypt DOB" with the University's official mechanism, keep everything after `create_session(...)` identical.
- **Chatbot:** unchanged — it never touches credentials; it only consumes the identity dict produced server-side (chat/routes.py:94–112) and the `auth_form`/`logout` SSE events.
- **StudentSession:** unchanged by design. `create_session`/`resolve_session`/`revoke_session`/`classify_stale_session` operate on `StudentSession` + cookie semantics that are independent of *how* the session was first minted.
- **Results / Admit Card / Exam Form:** unchanged — they receive `student_session["student_id"]` and never re-authenticate.

**Should StudentSession remain an application-side session even when authentication is delegated to a University API?**
- **Yes — strongly recommended to keep it.** The app-side session is opaque, SHA-256-hashed at rest, short-lived (fixed 10-min TTL), revocable, and server-validated on every request. It gives the application control (deactivation, logout, expiry) independent of the University's token lifetime, keeps University credentials out of the browser (HttpOnly cookie only), and preserves IDOR posture. Keep the `cus_student_sid` cookie as-is. The one point to settle (§12, risk R2) is the FK anchor: `StudentSession.student_id → students.id` currently requires a local row; a University-integrated deployment likely treats the local `students` table as a small **identity/registry store** (mapped from the University's official identifier) rather than the source of truth for records.

**Do NOT change current DOB authentication** — honored; no change made.

---

## 3. All Direct Dependencies on the Local Student DB (categorized)

Legend: file:function (line).

| # | Category | Location / function | Dependency |
|---|----------|--------------------|-------------|
| 1 | **A. Authentication** | `student/routes.py:108` `verify()` | `db.query(Student).filter(upper(reg_no))` — the credential lookup |
| 2 | **A. Authentication** | `student/routes.py:122` `verify()` | `student.hashed_password` (bcrypt DOB) |
| 3 | **A. Authentication** | `student/routes.py:127` `verify()` | `student.is_active` / `student.status` |
| 4 | **A. Authentication** | `student/session.py:82` `resolve_session()` | `db.get(Student, row.student_id)` — validity re-check on every request (2-table operation) |
| 5 | **B. Student profile** | `student/session.py:85–91` `resolve_session()` | returns `{student_id, name, reg_no, programme, semester}` from the `Student` row |
| 6 | **B. Student profile** | `student/gate.py:68` `hub_options()` | authenticated hub uses session profile fields |
| 7 | **B. Student profile** | `student_admin/service.py:94–135` `create_student`; `:172` `update_student`; `:222` `toggle_active`; `:214` `reset_dob_password` | writes `students` (demo provisioning — must be preserved) |
| 8 | **C. Results** | `student_results/service.py:455` `student_semesters()` | `db.query(StudentResult).filter(student_id==…, semester <= current_semester)` |
| 9 | **C. Results** | `student_results/service.py:500` `student_results_payload()` | `db.query(StudentResult).filter(student_id, semester)` |
| 10 | **C. Results** | `student_results/service.py:541` `list_results()` | admin list (demo-management) |
| 11 | **C. Results** | `orchestrator/engine.py:759` `_results_events()` | calls the two service functions above |
| 12 | **D. Admit Card** | `student_admit_card/service.py:461` `student_card_semesters()` | `db.query(StudentAdmitCard)` |
| 13 | **D. Admit Card** | `student_admit_card/service.py:497` `student_card_payload()` | `db.query(StudentAdmitCard)` |
| 14 | **D. Admit Card** | `student_admit_card/service.py:534/564/619` admin list, `_card_or_404`, `_duplicate_exists` | admin CRUD (demo-management) |
| 15 | **E. Exam Form** | `student_exam_form/service.py:454` `student_form_semesters()` | `db.query(StudentExamForm)` |
| 16 | **E. Exam Form** | `student_exam_form/service.py:483` `student_form_by_identity()` | `db.query(StudentExamForm)` |
| 17 | **E. Exam Form** | `student_exam_form/service.py:507/542` `student_fill` / `student_submit` | create/update `StudentExamForm` rows (write) |
| 18 | **E. Exam Form** | `student_exam_form/service.py:605/643/496` admin list, `_form_or_404`, `_duplicate_exists` | admin CRUD + admin import (demo-management) |
| 19 | **F. Programme/semester** | `student/session.py:89–90` resolve_session carries `programme`, `semester` | used by hub/greeting only — pickers do NOT use it |
| 20 | **F. Programme/semester** | `config.py:272–284` `valid_student_semesters` allowlist | enforced in all three services + routes as outer bounds |
| 21 | **G. Other — seeding** | `main.py:314` `_seed_students`, `main.py:364` `_seed_demo_service_data`, `seeders/demo_data.py` | writes operational tables (demo content, must be preserved) |
| 22 | **G. Other — demo manager** | `admin/routes.py:714–830` `/api/admin/demo/*` | seed/reset/regenerate/export/delete (superadmin) |
| 23 | **G. Other — import admin** | `student_results/service.py:161–435`, `student_admit_card/service.py:391–413`, `student_exam_form/service.py:417` | CSV/XLSX → operational tables (demo-management) |
| 24 | **G. Other — 9 dormant tables** | `demo_models.py` (FeeReceipt, StudentAttendance, StudentTranscript, MigrationCertificate, Revaluation, XeroxRequest, BacklogStatus, CourseRegistration, HelpdeskTicket) | written by seeder only; read by admin demo-status counters only; no student-facing path |

**Generated by:** all three domains have ≈21 student/admin ORM query sites total, **every one inside a `service.py`** — zero ORM in routes, zero in the orchestrator, and no raw SQL in the data path.

---

## 4. Is the Current Architecture Integration-Ready?

**Yes, structurally.** Evidence:

1. **Identity is always server-side.** No endpoint accepts `student_id`/`reg_no`/`roll_no` from the client for authorization. Every data path derives identity from `resolve_session()` on the HttpOnly cookie (`_require_student_snapshot` at results/routes.py:49, admit-card/routes.py:54, exam-form/routes.py:68; chat/routes.py:94–96). IDOR is closed by construction.
2. **Clean function boundary already exists.** The six student-facing reader functions are the de-facto data seam:
   - `student_results.service.student_semesters(db, student_id, current_semester)`
   - `student_results.service.student_results_payload(db, student_id, semester)`
   - `student_admit_card.service.student_card_semesters(db, student_id)`
   - `student_admit_card.service.student_card_payload(db, student_id, semester)`
   - `student_exam_form.service.student_form_semesters(db, student_id)`
   - `student_exam_form.service.student_form_by_identity(db, student_id, semester, exam_type)`
   - plus exam-form writes `student_fill` / `student_submit` / print DTO path.
3. **Routes, engine, gate, schemas, and frontend never touch ORM.** Replacing the data source means changing only what is *behind* those functions (or adding a provider ahead of them).
4. **Output shapes are the contract.** The canonical DTO shapes (semester picker lists, subject-row label/value fields, admit-card centre fields, exam-form detail fields incl. admin-withheld `transaction_id`) are fully decidable from `student/gate.py` and the service DTOs — these are the mapping targets.
5. **Authentication is single-seam.** Only `verify()` needs an adapter; the session system downstream is mechanism-agnostic.
6. **Demo manager is isolated.** Super Admin CRUD/import and the `/api/admin/demo/*` endpoints live behind `require_superadmin` and do not gate any student-facing read path.

**Not integration-ready in two respects (design decisions, not code defects):**
- **R1 — storage conflation:** demo and future data share the same `students` + three operational tables; rows carry no provenance marker. A University feed cannot safely coexist in the same tables without a documented coexistence strategy (§12).
- **R2 — identity anchor:** `student_sessions.student_id` is an FK to `students.id`, and `resolve_session` re-checks the local `Student` row on every request. A University source-of-truth deployment must decide how the local `students` table (registry) is kept authoritative for session validity (§12).

---

## 5. What Can Remain Unchanged During Future Integration

| Layer | Component | Why unchanged |
|-------|-----------|---------------|
| Frontend | `chatbot.js` SSF widget, `auth_form`/`logout` handlers, chip flows | Contracts stay identical (`auth_form.payload.family`, `options`, `detail`, `logout`, `done`) |
| Frontend | admin student/results/admit-card/exam-form screens | These remain the **demo** data-management UI |
| HTTP | `/api/student/results`, `/api/student/admit-cards`, `/api/student/exam-forms` (+ fill/submit/print), `/api/student/session`, `/api/student/logout` | Identity + DTO shapes unchanged |
| Session | `StudentSession` table semantics, `cus_student_sid` cookie, TTL, hashing, revocation, `classify_stale_session` | Mechanism-agnostic |
| Orchestrator | `engine.py` gate + `_results_events`/`_admit_card_events`/`_exam_form_events` | They call the same service functions with the same `student_session` dict |
| Gate/render | `student/gate.py` events | Pure renderers; output shapes are the mapping contract |
| Schemas | all `schemas.py` (student-facing wire types) | Unchanged |
| Admin CRUD/import | `student_admin/`, admin routes in the three domains, demo manager, seeder | Demo provisioning must keep working; no change |
| DB schema | `db_models.py`, `demo_models.py` | Per instruction — no schema change |
| RAG / chatbot / admin auth / grievance | unrelated subsystems | Untouched |

---

## 6. What Will Likely Require Modification (when official docs arrive)

1. **`student/routes.py` `verify()`** — add/route an adapter that authenticates via the University's official mechanism and mints the same session. (The reg-no+DOB path stays functional for the demo family/environment.)
2. **New provider/adapter + mapping modules** (new code, see §10) that serve the six reader functions + exam-form writes from the University source, converting University structures into the current DTO shapes.
3. **`student/session.py` `resolve_session()`** — the identity-anchor handling for the local `students` registry in a University-sourced deployment (validity/deactivation semantics delegated per official status feed). The session primitives themselves are untouched.
4. **`config.py`** — new settings only (provider selection per family/environment, University endpoint/TLS/timeout/credential storage from official docs). Existing settings untouched.
5. **Possibly** `student_exam_form/service.py` `student_form_by_identity`/`_form_or_404` — DTO-ize the two spots that currently leak ORM instances across the service boundary, so the provider has a non-ORM contract to target (this is also a pre-existing service-boundary smell, §12 R3).

Nothing in the chat orchestrator, gate events, frontend, schemas, or session cookie code needs to change for the *data and auth* integration by itself.

---

## 7. Recommended Integration Boundary

```
┌────────────────────────────────────────────────────────────────────┐
│ STUDENT SERVICES (unchanged)                                      │
│  frontend/chatbot.js · chat/routes.py · engine.py · gate.py       │
│  student/*routes.py (results/admit/exam)                          │
└───────────────┬────────────────────────────────────────────────────┘
                │ server-side identity dict {student_id, name, reg_no, programme, semester}
                ▼
┌────────────────────────────────────────────────────────────────────┐
│ STUDENT SERVICE LAYER (existing service.py functions)             │
│   student_semesters · student_results_payload                     │
│   student_card_semesters · student_card_payload                   │
│   student_form_semesters · student_form_by_identity              │
│   student_fill · student_submit                                  │
└───────────────┬────────────────────────────────────────────────────┘
                │ same signatures, same DTO shapes
                ▼
┌────────────────────────────────────────────────────────────────────┐
│ STUDENT DATA PROVIDER (new)  ← the ONLY new layer                  │
│   registry: family → provider selection (per environment)         │
│   ├── Demo Provider      → local ORM tables (current code path)   │
│   └── University Provider → official API/DB (adapter + field map) │
│   auth adapter  ← replaces credential check at /api/student/verify│
└────────────────────────────────────────────────────────────────────┘
```

- **Boundary is real and will not require touching higher layers** because the six function names/signatures and the event/DTO shapes already are the contract.
- **Keep it minimal and backward-compatible**: the recommended change is *not* a new abstract interface wrapping the whole app. It is: (a) one new provider module implementing the *same* functions the services currently call, and (b) selection logic in config or a registry mapping each family to Demo vs University. When the University provider is absent/not configured, behavior is byte-for-byte today's demo.

---

## 8. Is an Adapter/Provider Layer Needed?

**Yes — but a thin one, and only at the two seams already proven viable:**

- **Data seam:** implementing the six reader functions (and exam-form writes) against the University source, with a mapping layer normalizing University structures into the current shapes. This is O(1) query-surface — every student-facing read currently funnels through these functions (evidence: zero ORM elsewhere).
- **Auth seam:** an adapter at `/api/student/verify`.
- **Why it is genuinely necessary:** the University source will structurally differ (identifier keys, field names, pagination, result/exam schemas), and a field-mapping/adapter layer is the only way to keep the 6 functions + 4 SSE shapes byte-stable. The alternative (rewriting routes/engine/frontend per University schema) is a hard fork of the demo — rejected.
- **Not needed:** new orchestration, new session types, new endpoints, new DB tables, any change to gate events or frontend contracts. The service functions *are* the abstraction; the provider formalizes the seam that already exists.

---

## 9. Security Considerations for University Integration

All of the following invariants must be preserved (the current code already satisfies each; see Phase E report §I/J and `test_student_session.py`):

1. **Server-side student identity** — identity derived only from cookie → `resolve_session()`; never from the client.
2. **IDOR protection** — every data lookup scoped by the server-resolved `student_id`; exam-form print/submit do an explicit ownership re-check (`exam_form/routes.py:157`, `service.py:547`).
3. **StudentSession security** — opaque tokens hashed (SHA-256) at rest, 10-min fixed TTL, revocation, HttpOnly `Secure`-aware cookie on `/api/`.
4. **No credentials in chat** — credentials touch only `/api/student/verify`; never in messages, SSE streams, history, or events.
5. **No credentials in LLM prompts** — the engine only receives the identity dict; nothing hashed/DOB/cookie ever reaches the model.
6. **No credentials in analytics / audit logs** — audit records outcome-only (`student/routes.py`, `chat/routes.py`).
7. **No client-supplied identity as authorization** — reg_no appears only as *input to the auth step*, never as a selector.
8. **Least-privilege University access** — the University provider must use scoped read-only credentials/keys (`SELECT`-level or scoped API grants) with no write capability against the University system; credentials stored only on the server (env/vault), never in the browser.
9. **No direct production DB exposure to the frontend** — frontend continues to call only the application API; University DB/API is reachable only by the provider layer, server-side.
10. **New for University integrations** (to be specified against official docs, not implemented now): outbound TLS, short-lived provider credentials, Uni-ID ↔ local-id mapping safeguards, deactivation propagation (see R2), and rate-limit/quota handling on the University side so a noisy chat flow cannot abuse it.

---

## 10. Exact Files That Would Likely Change During Future Integration

**New files (additive, recommended layout when the time comes):**
- `backend/app/student_provider/__init__.py`
- `backend/app/student_provider/registry.py` — per-family, per-environment provider selection
- `backend/app/student_provider/demo.py` — the current ORM code path (today's service functions, relocated/mirrored or referenced)
- `backend/app/student_provider/mapping.py` — University structure → current DTO shapes (from official docs)
- `backend/app/student_provider/auth.py` — University auth adapter for `/api/student/verify`
- (University-specific transport, e.g. `university_client.py`, written only once official API details exist)

**Existing files that may need edits:**
| File | Change |
|------|--------|
| `backend/app/student_results/service.py` | `student_semesters`/`student_results_payload` delegate to provider |
| `backend/app/student_admit_card/service.py` | `student_card_semesters`/`student_card_payload` delegate to provider |
| `backend/app/student_exam_form/service.py` | `student_form_semesters`/`student_form_by_identity`/`student_fill`/`student_submit` delegate to provider; DTO-ize `_form_or_404`/`student_form_by_identity` returns |
| `backend/app/student/routes.py` | `verify()` gains the University-auth branch |
| `backend/app/student/session.py` | `resolve_session()` identity-anchor/registry handling (semantics only) |
| `backend/app/config.py` | new provider + University-connection settings (additive only) |

**Files that should remain untouched** despite integration: `orchestrator/engine.py`, `chat/routes.py`, `student/gate.py`, all `routes.py` in the three domains, all `schemas.py`, `student/session.py` session primitives, `student/logout.py`, `student/dob.py`, `models/*`, `seeders/demo_data.py`, `admin/routes.py`, `student_admin/*`, all frontend JS.

---

## 11. What Should NOT Be Changed Now

1. **Database schema** (`db_models.py`, `demo_models.py`) — per instruction.
2. **Current DOB authentication** — per instruction.
3. **Demo functionality** — Super Admin CRUD/import, demo manager, seeder — unchanged.
4. **`StudentSession` + cookie semantics** — do not replace the app-side session before the University mechanism is officially specified; it remains the security backbone.
5. **Frontend contracts** — do not alter SSE event shapes or the sign-in widget.
6. **Do NOT implement the provider/adapter** before official University technical documentation and access are provided — building interface code against invented endpoints/fields would be speculative (prohibited).
7. **Do not "clean up" the 9 dormant demo tables or preview-schemas** — out of scope; they are demo-manager surface only.
8. **Unrelated subsystems** (RAG, admin auth, grievance, catalogue/curriculum, website sync) — untouched.

---

## 12. Architectural Risks Discovered

- **R1 — Demo/production storage conflation (medium).** Demo and future University data would share the same `students` + three operational tables, with no per-row provenance. A University feed cannot safely load into these tables alongside demo rows (reg_no unique on `students`; the three service tables have no DB unique constraints and rely on app-side dedup). **Decision deferred to integration time (not now):** treat the local tables as the Demo family only and serve the University family exclusively through the provider (no writes to these tables), *or* establish a registry/external-id mapping (see R2). No schema change is implied now.
- **R2 — Identity anchor of `resolve_session` (medium).** `student_sessions.student_id → students.id` (FK, ON DELETE CASCADE) and the per-request `db.get(Student, …)` validity re-check mean a University-sourced deployment must keep some authoritative local record set for session validity and deactivation. Recommended direction (not implemented): the local `students` table becomes a minimal identity/registry store populated from the University's official feed (or mapped on-demand by the auth adapter), while data families resolve through the provider. The alternative — repointing `StudentSession.student_id` to a University identifier — is a schema change and is explicitly out of scope today.
- **R3 — ORM leaking across a service boundary (low, pre-existing).** `student_exam_form/service.py` `student_form_by_identity()` and `_form_or_404()` return ORM instances (the print route even compares `str(form.student_id)` at the route layer). This is fine for the demo but forces SQLAlchemy objects into the provider contract; DTO-ize when the provider layer is built.
- **R4 — Pre-existing frontend/backend mismatch (low, outside integration scope, NOT fixed here).** `frontend/js/admin_students.js:380` sends HTTP `PUT` for the edit-student update while the backend declares `@router.patch("/{student_id}")` (`student_admin/routes.py:96`). Under FastAPI a PUT to a PATCH-only route returns 405, so the admin edit-student screen is at risk until aligned. Flagged as a demo-side defect to be handled separately, not part of this audit's mandate.
- **R5 — Provider latency/availability (future).** Switching student reads to a University API puts latency and availability under the chat flow's real-time SSE render; mitigate later with an agreed timeout/quota policy and cache strategy specified against the official API contract.
- **R6 — Field-name rigidity (low).** The three output shapes carry University-vendor-agnostic field names (semester, internal/external/total, max_marks, grade, sgpa/cgpa, status, exam_type, academic_year, centre_name, fee_status …). These are the mapping contract; renaming them would ripple into `gate.py` and the frontend — keep them as the canonical wire vocabulary.

---

## Appendix A — Super Admin: Demo vs Production Separation (per the audit question)

- **The recommended conceptual split is supported by the current code:**
  - *Demo:* Super Admin → admin UI/demo manager → local DB → Student Services. This path is intact and isolated behind `require_superadmin`; it must remain.
  - *Production (future):* University System → Integration/Provider Layer → Student Services. Nothing in the current student-facing read path prevents inserting that layer behind the six service functions (§7).
- **Caveat:** the split is clean at the *code boundary* but not yet at the *storage layer* (R1). "Super Admin = demo mechanism / provider = production source of truth" requires the docision in §12 R1/R2, because today both would write/read the same tables.
- **Demo manager status:** the `/api/admin/demo/*` endpoints (seed/reset/regenerate/export/delete — superadmin) are backend-complete and test-covered, but currently have no wired frontend consumer; demo data is provisioned today through the admin CRUD/import screens (`admin_students.js`, `admin_student_results.js`, `admin_student_admit_cards.js`, `admin_student_exam_forms.js`).

## Appendix B — Frontend/Backend Contract Inventory (Student Services)

| Endpoint | Method | Frontend caller | Identity source | Wire payload example |
|----------|--------|-----------------|-----------------|----------------------|
| `/api/student/verify` | POST | `chatbot.js` SSF submit | creds (reg_no+DOB) | req `{reg_no,dob}` → `{verified, name}` + Set-Cookie |
| `/api/student/session` | GET | probe | cookie | `{authenticated}` |
| `/api/student/logout` | POST | — | cookie | `{logged_out}` |
| `/api/student/results` | GET | `chatbot.js` chips / `studentAdmin`-adjacent | cookie | `{semesters:[…]}` / `{result:{…}}` |
| `/api/student/admit-cards` | GET | chips | cookie | `{semesters:[…]}` / `{card:{…}}` |
| `/api/student/exam-forms` | GET/POST | chips / form | cookie | `{semesters:[…]}` / `{form:{…}}` |
| `/api/student/exam-forms/{id}/submit` | POST | form UI | cookie | `{confirm:true}` |
| `/api/student/exam-forms/{id}/print` | GET | form UI | cookie | DTO (no `transaction_id`) |

SSE events consumed by `chatbot.js`: `token`, `options`, `detail`, `auth_form` (payload.family), `results_form` (semesters + roll prefill), `logout`, `done`, `error`. `options.payload.options[].id` vocabulary: `admit_card_sem-N`, `exam_form{type}{sem}`, `student_results`/`student_admit_card`/`student_exam_form` (hub). Structured card fields: `fields[]` with `{label, value}`. Result detail is a client-rendered card fed ONLY by `POST /api/student/results/view` (semester + examination roll in the POST body — never a URL); `results-sem-N` ids are legacy preselect hints only (the engine folds them into the `results_form` payload).

_Security note:_ the only client-supplied identity input is `reg_no` inside the sign-in payload — and it authenticates rather than authorizes; every data read is scoped by the server-resolved `student_id`.

## Appendix C — Verification Baseline (evidence the current contract is coherent)

- Full suite: **346 passed** (pytest tests, 446s, 14 pre-existing warnings) — Phase A–D + session/logout/UX batteries all green on the code audited here.
- `node --check frontend/js/*.js` — clean.
- The audit itself made **no code changes.**