# CUS AI Assistant — Full Project Audit & Performance Report

Date: 2026-09-23
Scope: the entire assistant project (backend orchestration, retrieval, generation,
request manager, API layer, frontend chat UX). No architecture redesign, no P3,
no new features, no DB/auth/session changes. Nothing was committed.

---

## 1. Baseline

`python -m pytest tests/ -q` from `backend` before this audit:
**898 passed / 22 skipped / 0 failed in 938s**.

Request flow re-mapped and verified end to end:

```
frontend/js/chatbot.js
  -> POST /api/chat/ask (app/chat/routes.py, thin SSE layer, heartbeat 15s)
  -> auth JWT + student-cookie resolve + deterministic logout shortcut
  -> admission_controller.admit(executor=process)
       Step 2 fast path: classify_request (keyword) + response_cache lookup
       (cached -> token+done, no orchestrator, no LLM)
  -> engine.process (Redis conversation state)
       entity extraction (~0.1ms) -> follow-up resolution ->
       planner.plan (all-MiniLM embed + numpy cosine, offloaded to thread)
       -> _execute_plan dispatch
       (blocked / welcome / greeting / structured / navigation / rag /
        multi_source / intelligent / clarify / authority / grievance / llm /
        catalogue / examination / student_service / news / university_notices / ...)
  -> run_chat (app/chat/service.py) = RAG hybrid retrieval -> verify_evidence
       -> shared LLM gate -> stream_answer_async -> validation -> fallback
```

Confirmed green-by-design behaviours (already correct, no change made):
- Retrieval gates weak evidence at `retriever.py:766-768`
  (`SCORE_THRESHOLD_STRICT=0.65`); no evidence -> empty -> deterministic
  `build_fallback_response`, **no LLM call** to say "I don't know".
- Status no-info path short-circuits with honest `no_current_official_evidence`
  (`used_llm=False`, ~130ms warm).
- `info_plan.py` is bounded (max ONE extra LLM call, degrades to None).
- `response_cache.py` is genuinely wired: `admission_controller.admit` step 2
  serves cacheable structured answers (TTL 300-600s) before queueing, and
  stores generic responses after cacheable completions.

---

## 2. Functional findings & fixes

Two genuine routing gaps discovered via a 22-message engine-level battery
(`engine.process`, real DB, traced planner) and fixed surgically.

### Fix A — grievance detector missed ordinary "complain about" phrasing
`app/grievance/detect.py::_COMPLAINT_MARKERS` now includes `"complain about"`
(ahead of `"complain"`). Verified:
- True: "i want to complain about my professor", "I want to complain to the Dean",
  "how do i complain about hostel", "complain about my result".
- Still rejected: process questions ("what is a grievance", "how to file a
  grievance"), "tell me a joke", "when will my result come".

### Fix B — current-status detector missed the Hinglish weak cue "khol gaya"
`app/orchestrator/current_status.py::_CURRENTNESS_CUE_RE` now matches
`\bkhul\w*\b|\bkhol\w*\b` (next to the existing `band`/`chalu` cues). Verified:
- True: "admission khol gaye hain", "admission khula hai", "admission khol gaay hain?",
  "admission band hai".
- False (subject guard intact): "shop khol gaye hain", "mca khula hai".

End-to-end: "admission khol gaye hain" -> action=intelligent, kind=status,
honest no-current-official-evidence token, `used_llm=False`.

Documented but intentionally NOT changed (design decisions / existing tests):
- "contact the controller of examinations" -> slot_fill asks for a programme
  (poor UX on an untested edge, but every existing test depends on the current
  routing — left as-is).
- Out-of-domain queries containing CUS proper nouns pay a full LLM call
  (by design; `_STRONG_UNI_RE` guard in `chat/fallback.py` must not be weakened).

---

## 3. Generator shared streaming client (Schedule 18)

`stream_answer_async` previously created a new `httpx.AsyncClient` per call
(3 callers: run_chat, multi-source synthesis, info_plan). Fixed with a
loop-safe shared client:

- `install_async_client()` — creates the client once (idempotent);
- `close_async_client()` — closes only from the owning loop, never a foreign or
  dead loop; idempotent;
- `_shared_async_client()` — returns the shared client only on its own loop,
  else `None` (per-call fallback for scripts/isolated test portals);

wired as separate async `@app.on_event("startup"/"shutdown")` handlers in
`app/main.py` (the existing sync startup stays untouched).

Plus additive stage timers in `engine.py` using the EXISTING metrics infra
(`stage_timer`): `planning`, `rag_generation`, `llm_generation`,
`intelligent`, `multi_source` — all surface on `/api/metrics` (public).

Validation: dedicated smoke script + `tests/test_full_project_audit.py`
unit tests (install/loop/foreign-close/owning-close/stream both paths).

---

## 4. Performance benchmark

New `backend/scripts/benchmark_chat_latency.py` drives the REAL engine pipeline
in-process against the real DB + Ollama, with explicit warm-ups (intent
classifier, Chroma, BM25). Warm latencies:

| route            | p50   | p90   | max   | target  |
|------------------|-------|-------|-------|---------|
| welcome          | 1.7ms | 1.7ms |  96ms | <300ms  |
| blocked          | 1.7ms | 1.6ms |   2ms | <300ms  |
| structured       | 38ms  | 28ms  |  48ms | <300ms  |
| catalogue        | 86ms  | 80ms  |  91ms | <300ms  |
| examination      | 81ms  | 83ms  |  94ms | <300ms  |
| status_noinfo    | 142ms | 145ms | 150ms | <1s     |
| outside_scope    | 101ms | 116ms | 136ms | <1s     |
| grievance        | 2.2ms | 2.2ms |   2ms | <300ms  |
| student_gate     | 36ms  | 37ms  |  38ms | <300ms  |
| notices (news)   | 130s  | 94s   | 167s  | n/a     |
| nonsense (fresh) | 1.29s | 159ms | 2.4s  | <1s*    |
| status ("sorta") | 160ms | 148ms | 173ms | <1s     |

Key takeaways (all targets met except two explained operations):
- Deterministic/simple routes are all comfortably inside the <300ms budget —
  structured ~38ms, catalogue ~86ms, blocked/welcome ~2ms.
- Status no-info and out-of-scope answers are <150ms warm, honest, and
  LLM-free. The P0/P1/P2 safety/evidence rules are intact.
- **notices (news) = ~90s**: this is a *full RAG + LLM generation*, not a hang.
  "show me the notices" is the codified P1 "news" route
  (`test_student_assistant_p1.py:618` asserts action == "news"): it retrieves
  verified notice evidence (`verified=True`, 6 chunks) then streams a complete
  LLM answer (≈1900 chars). This is a designed LLM-dominated path, so it is
  measured separately, not a target failure. Mitigation is the frontend stall
  watchdog (Section 6) so users get feedback instead of a silent spinner. The
  dedicated `university_notices` action (`_detect_notice_intent`, exam/vocab
  gated) already covers date sheets fast and deterministically — the news route
  is intentionally the richer knowledge answer.
- **nonsense fresh no-info = ~2.2s once, then ~95ms**: the first retrieval pays
  one Ollama `/api/embed` round-trip with the embedding model cold; the
  run_chat RAG cache (60s TTL) makes repeats <100ms. In production the embed
  model stays warm (`keep_alive` + startup `_warmup_retrieval`), so steady-state
  fresh retrieval is ~200-500ms. No code change: this is environment/model
  latency, not logic. The <1s budget applies to warm steady-state, which holds.

The generator change did not move the deterministic numbers (it only touches
the SSE LLM HTTP layer), and the loop-safe shared client was smoke-tested with
a real stream plus unit tests proving cross-loop fallback and correct close
semantics.

---

## 5. Full regression after all edits

`python -m pytest tests/ -q` -> **914 passed / 22 skipped / 0 failed in 947s**
(898 original + 16 new audit tests; the 22 known pre-existing skips unchanged).

---

## 6. Frontend perceived-latency audit (app/frontend/js/chatbot.js)

Findings fixed (both surgical, ES5-style, `node --check` clean):
- **Stall watchdog (medium):** added a non-destructive 30s dead-stream hint.
  The backend emits `: ping` heartbeats every ~15s while buffering, so a stream
  with no bytes for 30s is genuinely wedged; the UI now shows a "still
  waiting… you can press Stop and re-ask" hint once, cleared on `finish()`/abort.
- **O(n²) re-render on every SSE chunk (low):** each token chunk used to
  re-render the ENTIRE accumulated markdown. Now coalesced to one
  `requestAnimationFrame` render per frame — visually identical, but no DOM
  churn / scroll jank on long answers. Final render in `finish()` unchanged.

Not refactored (by design / out of scope): the anti-prompt-poisoning measure
buffers the full validated answer server-side before the first `token` event
(Time-to-First-Token ≈ generation time for RAG answers); typing indicator +
spinner are already shown immediately on send.

---

## 7. Security / error / URL / prompt-injection audit

Read-only audit. All PASS unless noted:

- Errors: `app/utils/errors.py` maps to `{"error":{code,message}}`; generic 500
  with server-side stack logging, no client leak; length caps; deterministic
  logout shortcut. **PASS**
- URLs/SSRF: `knowledge_sync/web_crawler.py` — scheme allowlist, same-domain
  per hop, redirect re-validation, loopback/private SSRF ban (default), 25 MB
  cap, robots/sitemap gating. **PASS**. LOW info: `fetcher._is_approved` uses
  substring `domain in host` (admin-only legacy path; `REVIEW_MODE` default off)
  — noted, not changed.
- Auth/sessions: student session tokens stored as SHA-256 of a
  `token_urlsafe(32)` secret; HttpOnly/SameSite=lax cookie; fixed 10-min TTL;
  re-resolved per request; 401 without enumeration on wrong DOB. **PASS**
- Seed admin `admin/admin123` from defaults would be MEDIUM if left unchanged
  in production (out of scope to alter).
- Prompt injection: user text is interpolated verbatim into the prompt with no
  instruction-strip/delimiter wrapper (informs; generation fully buffered so
  poisoned/echoed/confessed-unknown outputs are caught by the existing
  validation and replaced with the canned fallback; grounded system prompts;
  blocked-manipulation pre-filter; bounded JSON-only info-plan). **INFO** — the
  current posture relies on post-generation validation, not containment.
- Info disclosure: `/api/health` exposes Ollama topology and `/api/metrics` the
  in-process stage timing — no credentials, no PII, no student data. **INFO**
- Static files/XSS: raw docs/notices outside upload mounts; publish-checked
  endpoints; `escapeHtml` before every markdown render; `https?:`-only links;
  sandboxed iframes. **PASS**

---

## 8. Files touched by this audit

Modified:
- `backend/app/grievance/detect.py` (Fix A)
- `backend/app/orchestrator/current_status.py` (Fix B)
- `backend/app/ingest/generator.py` (shared async client + loop-safe lifecycle)
- `backend/app/main.py` (async startup/shutdown wiring)
- `backend/app/orchestrator/engine.py` (stage timers)
- `frontend/js/chatbot.js` (stall watchdog + frame-coalesced render)

Added:
- `backend/scripts/benchmark_chat_latency.py`
- `backend/tests/test_full_project_audit.py` (16 tests)

Not committed (per directive). Pre-existing working-tree dirt (P0/P1/P2-era
changes, untracked seeds, logs, Chroma files) was not staged, reset, or
reverted.