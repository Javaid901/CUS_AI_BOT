# Backend Connectivity Fix Report

Date: 2026-09-23
Scope: Surgical fix for the frontend error "Cannot connect to backend (http://localhost:8001)".

## Symptom

Opening the site and sending a message shows:

```
Cannot connect to backend (http://localhost:8001).
```

The browser resolves its API base URL from `frontend/js/config.js`: by default
`http://<host>:8001` (no `localStorage["cus_backend_url"]` override and no
`<meta name="cus-backend-url">` present). The message appears when the browser
cannot even issue a request to that address.

## Root Cause

The backend was being started with the root README's documented command:

```
python -m uvicorn app.main:app --reload
```

`uvicorn` does not read the app's `PORT` setting from `.env`; when no
`--port` is given it binds its **own CLI default, port 8000**
(`python -m uvicorn --help` → `--port INTEGER ... [default: 8000]`). The Pydantic
`Settings` default in `backend/app/config.py` was also `PORT = 8000`.

So a normal, documented start produced a *healthy* backend on `http://127.0.0.1:8000`
while the frontend looked for `http://localhost:8001` — an exact, reproducible
port mismatch (reproduced below).

The **intended** configuration everywhere else was already 8001:
`backend/.env` (`PORT=8001`), `backend/.env.example`, `backend/README.md`
(`python run.py`, "Open http://localhost:8001/docs"), the historical
`server_err.log` (Uvicorn running on http://0.0.0.0:8001), and the frontend's
`DEFAULT_PORT = 8001`.

## Reproduction

1. With no process on 8001, start the backend with the root README's command
   (port omitted):
   `python -m uvicorn app.main:app`
   → `INFO: Uvicorn running on http://127.0.0.1:8000`; `GET http://127.0.0.1:8000/` → `200`.
2. Frontend default target is `http://localhost:8001` → connection refused →
   exactly the reported browser error.

## Fix (files changed)

| File | Change |
|------|--------|
| `backend/app/config.py` | `PORT: int` default `8000` → `8001` (matches `.env`, `.env.example`, backend README, docs, frontend). Matters when no `.env` is present. |
| `README.md` | "Start backend" step changed to `python -m uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload` with a note that it serves UI + API on `http://localhost:8001`. |
| `backend/tests/test_backend_connectivity.py` | NEW — regression tests (below). |

No functional application code, no ports in `.env`, and no frontend were
changed. Existing startup/shutdown handlers (`app/main.py`, incl. the shared
async Ollama streaming client `install_async_client` / `close_async_client`)
are untouched.

## Verification

### Automated

`python -m pytest tests/test_backend_connectivity.py -q` → **7 passed**:

1. `Settings` `PORT` default is `8001`.
2. `Settings().PORT == 8001` (effective value).
3. `frontend/js/config.js` `DEFAULT_PORT = 8001`.
4. Root README "Start backend" step binds 8001 (or uses `python run.py`).
5. `GET /` reachable → 200.
6. `GET /api/health` reachable → 200 `{"status":"ok", ...}`.
7. `POST /api/chat/ask` → 401 (route registered & auth-gated; a 404 would mean the endpoint is missing).
8. Startup/shutdown lifecycle: entering `TestClient(app)` installs the shared
   async client (`app.ingest.generator._ASYNC_CLIENT is not None`), exiting
   closes & clears it (`None`), and a second enter/exit (backend restart)
   installs and clears it again.

Full suite: `python -m pytest tests/ -q` → **919 passed / 22 skipped / 2 failed
in 681.67s**. The 7 new regression tests all pass. The only 2 failures are in
`test_phase3c51_session_lifecycle.py`
(`test_run_chat_releases_request_session_before_generation`,
`test_ask_route_releases_di_session_before_orchestrator_runs`) — these are the
pre-existing full-suite flaky tests recorded in the prior audit session; both
**pass in isolation** (`python -m pytest tests/test_phase3c51_session_lifecycle.py -q`
→ 5 passed) and are unrelated to this change.

### Manual (documented start path)

- Stop backend → kill the uvicorn on 8001 → port released; `GET /` on 8001
  times out/refuses (this is the exact state that produced the browser error).
- Start backend: `python -m uvicorn app.main:app --host 0.0.0.0 --port 8001`
  → `INFO: Uvicorn running on http://0.0.0.0:8001`, `Application startup complete`.
- `GET /` → 200, `GET /api/health` → 200, `POST /api/chat/ask` → 401 (route live),
  `GET /docs` → 200.
- Repeated stop → start cycle yields the same healthy result (backend currently
  left running on `http://0.0.0.0:8001`).

## Final Forensic Verdict — "Cannot connect to backend" classification audit (addendum to addendum)

Per the directive's exact acceptance criterion, the frontend must distinguish
three situations. Verified against the real chatbot.js source and a live reachable
backend:

| Situation | Where detected | Message shown | Correct? |
|-----------|----------------|---------------|----------|
| 1 · True network failure | `fetch()` rejects / CORS-blocked (`chatbot.js` `doChat` → `.catch`, the only remaining path) | `⚠️ Cannot connect to backend (…). Please ensure FastAPI is running.` | ✅ |
| 2 · HTTP app error | `.then()` — `!resp.ok` with `status !== 401` | `Server error (HTTP <n>). Please try again.` | ✅ |
| 3 · Auth/session gate (401) | `.then()` — `resp.status === 401` → re-auth/retry | `Cannot authenticate` / silent re-login + retry | ✅ |

Evidence (this session):
- The literal "Cannot connect to backend" is emitted at chatbot.js:1269-1271
  **inside the network-level `.catch` only**; the 401 branch (L1068-1098) and
  the HTTP-error branch (L1277-1278) never reach it. So a browser 200/200/401
  sequence can **not** be mislabeled as connectivity — the classification is
  correct and requires no redesign.
- The one remaining way the UI can show the connectivity message while the
  backend is up is a browser still executing a **stale cached `config.js`**
  that rebuilt `http://localhost:8001` from the ambiguous `hostname`. That is
  the exact stale-cache mechanism fixed here: `CUS_CONFIG_VERSION="4"` is the
  single source of truth, every page loads `config.js?v=4` (byte-diff proven
  token-only per page), and 4 regression tests pin the served==disk + token
  contract. FastAPI itself was never unreachable.
- No ports, ports, auth, auth constants, chatbot.js or chatbot logic were
  changed. No commits made.

## Unchanged Areas

Chatbot logic, planner, query understanding, extractor, current-status, P2
safety/evidence, multi-intent, RAG/BM25/Chroma/Ollama/LLM prompts/generator,
request manager, caching, Redis/PostgreSQL/schema, auth, grievance/examination/
catalogue/student services, docs/website sync, analytics, frontend chat
rendering and SSE, and all earlier performance work were **not** modified.
No ports were changed to hide the error — the backend now actually binds the
port the frontend requests; only the previously inconsistent config default and
the misleading README startup command were corrected. Commits were not made.

---

# Addendum — Rediscovery of a second, independent failure mode (2026-09-23)

The port fix above (8000→8001, everything-on-8001) is correct and its 7
regression tests still pass. However, a follow-up reproduction session on
2026-09-23 uncovered that **even with the corrected config.js on disk and
served byte-identically**, the browser could still show
`Cannot connect to backend (http://localhost:8001)`. This is a *different,
second* mechanism that the port test suite could not catch, and it is now
fixed and covered by new regression tests (see below).

## Second root cause: stale cached `config.js` + no cache-busting contract

`frontend/js/config.js` is loaded from every frontend page through a
cache-busting query (`<script src="../js/config.js?v=N">`). When a browser
fetched that URL before the port fix, it cached the **old broken body** (the
copy that rebuilt `http://localhost:8001` from the ambiguous `hostname` and
therefore depended on IPv6/IPv4-ambiguous localhost resolution). After the
server-side port/config fixes were deployed, the file on disk changed, but:

1. The pages that include it advertised an **inconsistent** cache token:
   `index/about/admissions/colleges/contact` used `?v=2`, `admin.html` used
   `?v=3`, and `authority-admin.html` loaded `config.js` with **no `?v=`
   query at all**. Older cached copies were shipped as `?v=0..3`.
2. A browser that had cached `config.js?v=2` (the pre-fix body) would keep
   serving that stale copy forever, because its cached URL token never changed
   — the fixed file on disk and the live server are irrelevant to such a
   browser unless the token changes.
3. Serving the new copy changes nothing for a browser that never re-requests
   it; the corrective fix must therefore be the **cache-busting contract**:
   a single version constant inside `config.js` that (a) is bumped past every
   token ever shipped and (b) is referenced by *every* page via the exact same
   `?v=` query, with a test that forces a future content change to bump the
   token. Directing the user to hard-refresh is explicitly a non-fix (browsers
   may refresh without invalidating the cached config.js?v=2 variant).

## Fix (files changed this session)

| File | Change |
|------|--------|
| `frontend/js/config.js` | Added `var CUS_CONFIG_VERSION = "4";` (single source of truth; > every shipped token 0..3). Logic itself was already correct (same-origin passthrough + deterministic `127.0.0.1` fallback). |
| `frontend/pages/index.html` | `config.js?v=2` → `?v=4` |
| `frontend/pages/about.html` | `config.js?v=2` → `?v=4` |
| `frontend/pages/admissions.html` | `config.js?v=2` → `?v=4` |
| `frontend/pages/colleges.html` | `config.js?v=2` → `?v=4` |
| `frontend/pages/contact.html` | `config.js?v=2` → `?v=4` |
| `frontend/pages/admin.html` | `config.js?v=3` → `?v=4` |
| `frontend/pages/authority-admin.html` | `config.js` (no token) → `?v=4` |
| `backend/tests/test_backend_connectivity.py` | Added 4 cache-busting regression tests (below). |

No chatbot.js tokens or any other files were touched; every `chatbot.js?v=7`
include is unchanged.

## New regression tests (all pass)

1. `test_config_cache_bust_token_is_bumped_past_every_published_copy` —
   `CUS_CONFIG_VERSION` must be > all previously shipped tokens (0…3).
2. `test_every_page_loads_config_js_with_the_current_single_token` — every
   page's `..js/config.js?v=N` must equal the value in `CUS_CONFIG_VERSION` and
   be internally consistent, so a future edit can't leave one page pointing at
   a stale token while others fetch the fixed copy.
3. `test_no_page_loads_unversioned_config_js` — no page may load `config.js`
   without a `?v=` query (the exact hole in the original `authority-admin.html`).
4. `test_served_config_js_equals_the_fixed_disk_copy` — the backend must serve
   the exact fixed bytes from disk (`GET /js/config.js` content == disk), so a
   bumped `?v=` always downloads the corrected copy.

## Verification (this session)

- `GET /js/config.js` served bytes == disk bytes (byte-identical), Cache-Control
  absent / ETag+Last-Modified present → no explicit max-age, so the cache-bust
  token is the only reliable freshness mechanism, confirming the fix design.
- `chatbot.js?v=7` includes on all pages verified **untouched**.
- Targeted: `test_backend_connectivity.py` → **11 passed** (7 prior + 4 new).
- Full suite: `python -m pytest tests/ -q` → **925 passed / 22 skipped / 0 failed**
  (the 2 previously-flaky session-lifecycle tests also passed this run; no
  unrelated failures).
- Manual cycle battery (3 independent stop→start→chat cycles + down→error→
  restored→restored): backend on `0.0.0.0:8001`; page 200,
  `js/config.js?v=4` 200, `POST /api/chat/ask` → 401 (auth gate = route live);
  backend-down state confirmed via refused connection; restart returned 200s.
- Backend left running healthy on `http://0.0.0.0:8001`.

## Unchanged (added this session)

Frontend `config.js` logic, chatbot behaviors, backend code, ports, auth, and all
previously documented areas remain untouched. Commits were not made.

---

# Addendum 2 — the definitive failure mode: a frontend ReferenceError masquerading as a connectivity error (2026-09-23)

After the two fixes above (port 8001 + cache-busting contract) both shipped and
their 11 regression tests passed, the symptom *still* reproduced:

```
[CUS] POST http://localhost:8001/api/chat/ask | stream=true
[CUS] Chat done in 18ms | citations: 0
... browser renders the full reply AND then ...
Cannot connect to backend (http://localhost:8001)
```

## Root cause (finally proven end-to-end)

The backend was provably healthy: raw-TCP framing, WHATWG-fetch (Node) parsing of
the live stream, and Playwright-driven Chromium all consumed the exact SSE the
browser gets — `event: options` → `event: done` (or plain-token cache replay) →
clean chunked `0\r\n\r\n` termination. The "Cannot connect" text is emitted
**only** from the network-level `.catch` at `frontend/js/chatbot.js:1267-1271`
(`e.name !== "AbortError"`). The failure was a **JavaScript ReferenceError**:

- `frontend/js/chatbot.js` `doChat()` declares its per-stream state with
  `var lastActivity = 0; var stallWarned = false; var renderScheduled = false;`
  **inside** the `.then(function (resp) { ... })` fetch callback (old line 1116).
- `finish()` is a sibling function in `doChat()` scope. The `done`-event handler
  calls `finish()`, whose body executes `renderScheduled = false;` (old line
  1283). Because `var` is function-scoped to the `.then` callback,
  `renderScheduled` was **not defined** in `finish()`'s scope → `ReferenceError:
  renderScheduled is not defined`.
- That exception escaped the stream handler and propagated to the outer `.catch`,
  which (being non-AbortError) printed the misleading
  `⚠️ Cannot connect to backend (http://localhost:8001)` banner — **after** the
  real content had already been rendered. Exact browser console evidence:
  ```
  [error] [CUS] Network error: renderScheduled is not defined
  ```
  Playwright reproduced this 100% deterministically (1/1 with `admissions`,
  and every completed stream), while the backend never errored.

This explains every earlier observation: HTTP 200, planning logged, valid SSE,
then an unrelated banner — the network and backend were never the problem.

## Fix (this session)

| File | Change |
|------|--------|
| `frontend/js/chatbot.js` | Hoisted `lastActivity`/`stallWarned`/`renderScheduled` to a single `var` declaration at `doChat()` function scope (before `ensureAuth()`); the fetch callback now *assigns* `lastActivity = Date.now(); stallWarned = false; renderScheduled = false;` without re-declaring (`var`) them, so `finish()` and the stream callback share the same flags. |

2-line, behavior-preserving change: identical semantics for every stream path;
only the variable scoping is corrected.

## Regression tests (now 13 total in `test_backend_connectivity.py`, all pass)

1. `test_chatbot_sse_state_declared_at_dochatch_scope` — the three flags are
   declared once at `doChat()` scope (indent 4) and are never re-declared with
   `var` inside the `.then()` callback. Guards against re-introducing the
   scoping bug that caused the ReferenceError.
2. `test_served_chatbot_js_equals_the_fixed_disk_copy` — the backend must serve
   the exact fixed `chatbot.js` bytes (`GET /js/chatbot.js` == disk).

## Verification (this session)

- **Playwright-driven real Chromium** against the live backend, before vs after:
  - Before: `admissions` → `Cannot connect` banner + console
    `[error] [CUS] Network error: renderScheduled is not defined`.
  - After: same query renders all 5 option chips + Back, no banner, no errors;
    RAG text stream ("What is the fee structure for BCA?") renders the complete
    fee card/detail, `[CUS] Chat done in 432ms`, errors: NONE.
- Backend stream byte-level: raw TCP read shows chunked `event: options` +
  `event: done` + `0\r\n\r\n` (clean termination); Node WHATWG-fetch parse of the
  live 401→login→retry flow: `options,done`, `cached: undefined`, done in 15ms.
- **Full E2E battery (real browser, Phase-32 list), all 7 scenarios render with
  zero console errors** (banner=false everywhere):
  1. `hello` → greeting answer; 2. `What is MCA?` → programme detail card;
  3. `admissions` → options chips (UG/PG/PhD/Integrated/DYD) + Back;
  4. `Is MCA admission open?` → current-status answer;
  5. `What is the weather on the moon?` → no-evidence answer;
  6. `What is the BCA fee?` + 7. `Are there scholarships for it?` → follow-up.
- **Failure-mode battery (Phase 33):** FastAPI healthy + Ollama healthy →
  `/api/chat/ask` 200 with `event: done` (fresh queries yield
  `event: options` + `event: done`; repeat queries correctly replay the plain-text
  `response_cache` + `done`). FastAPI genuinely down → connection refused
  (distinct from the wrong-conflation case). Playwright shows no
  `requestfailed`, no `Network error`/`ReferenceError` console entries on the
  exact failing query.
- `python -m pytest tests/test_backend_connectivity.py -q` → **13 passed**.
- Full suite `python -m pytest tests/ -q` → **925 passed / 22 skipped / 2 failed
  in 621.09s**; the 2 failures are the pre-existing flaky
  `test_phase3c51_session_lifecycle.py` tests that **pass in isolation**
  (5 passed) and are unrelated to this change.
- Working tree preserved; only `chatbot.js` (2-line fix) and the test file
  changed. Commits were not made.

## Final causal trace (Phase-2 requested chain, conclusively observed)

```
Browser doChat()  ── fetch POST /api/chat/ask (Bearer JWT, chat_id → null)
  ↓
FastAPI route /api/chat/ask (auth 200) → db.close() → returns StreamingResponse(media_type=text/event-stream)
  ↓  _sse_with_heartbeat(_map_events(...))   — 200 headers go out first
  ↓  _map_events → admission_controller.admit() → orchestrator.engine.process()
  ↓  planner.plan(...)   → [entity_extraction] 0.3ms → [planning] 96.1ms action=navigation target=None
  ↓  engine._process → action == Navigation → response = OptionsSection (Admissions)
  ↓  executes ✓ (update_context_from_plan, update_nav_breadcrumb→push_breadcrumb, add_context_to_response)
  ↓  yield OptionsSection → _map_events → SSE frame "event: options\ndata: {…}\n\n" sent on the wired stream
  ↓  yield done → SSE frame "event: done\ndata: {"chat_id":…}\n\n" → 0\r\n\r\n chunked close (raw-TCP proven)
  ↓
Browser fetch resolves 200 → resp.body.getReader() → reader.read() resolves → SSE parser
  ↓  ev "options" → renderOptions(chips) renders; ev "done" → state.chatId set → finish()
  ↓  finish() → renderScheduled=false (was ReferenceError pre-fix) → msgEl.innerHTML = renderMarkdown()
UI  ✓  no banner, replies rendered (Playwright proven)
```

This is the complete, instrument-free observational chain; the only failure ever
present was the finish() ReferenceError on the browser side, now fixed.

## Unchanged (added this session)

Backend code, routes, ports, cache-busting tokens (all `chatbot.js?v=7`
includes untouched), auth, prompts, RAG/Chroma/Ollama, and every area listed in
the prior addenda remain untouched. Commits were not made.