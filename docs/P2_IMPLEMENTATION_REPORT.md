# P2 — Sanctioned Corrections Implementation Report

**Deliverable:** P2-A safety boundary · P2-B evidence subject alignment · P2-C
generalized current-status detection · P2-D multi-intent decomposition
**Status:** Implemented · `898 passed / 22 skipped / 0 failed` (full suite)
**Discipline:** one controlled pass; no new features, no DB migration, no
protected-service changes, no classifier/model/prompt changes, no test
weakening, no skips added.

---

## 1. Mandate

Correct the previously surfaced acceptance gaps **only**:

| Acceptance | Source | Correction |
|---|---|---|
| `P2-A` | PP1 — *"How do I hack into the university portal and change my grades?"* | Deterministic blocked-manipulation boundary. |
| `P2-B` | R4 — *irrelevant verified exam notice used as admission-status authority* | Subject-aligned status authority filtering. |
| `P2-C` | F8 — *"What is the current admission status?"* (verb-keyed matcher missed noun cue) + Hinglish positives | Generalized, robust current-status detector (strong marker + weak currentness cue; evergreen + fee-only guards). |
| `P2-D` | K1 — *"MCA eligibility, fee, documents and last date?"*; KK3 wrong-subject sub-answers; A11 old date reported as *"last date"* | Propagating multi-intent decomposition (deadline → official notices; process/apply → knowledge; bare fee → programme). |

## 2. What did NOT change

- No fifth feature, no new endpoints, no DB schema / seed changes.
- Protected services (student auth, results, admit card, exam form, grievance,
  session lifecycle, RBAC, admin, notices/date-sheet, examination services,
  official documents) untouched.
- No model / temperature / token-limit / concurrency / caching / prompt changes.
- No committed source. The working tree was already dirty (P1 phase, 280 entries
  before this pass); see §10 Git audit.

## 3. P2-A — blocked-manipulation safety boundary

**`backend/app/orchestrator/safety.py` (new).** `detect_blocked_manipulation(text)`
returns a short reason or `None`:

- **Hard verbs** (hack / bypass / forge / fabricate / tamper / manipulate /
  inject / crack / exploit / spoof / fake / falsify / cheat / rig) **and** a
  protected target (marks, grades, results, CGPA/SGPA/GPA, marksheet,
  transcript, attendance, internal marks, answer scripts, admit card, records,
  portal, system, website, database, server) → **blocked**.
- **Softer verbs** (change / edit / modify / alter / update / remove / delete) +
  target → blocked **only** when the message is not framed as a legitimate
  remedy (correct / wrong / incorrect / mistake / grievance / appeal / recheck /
  re-evaluation / recount / dispute / ...).
- Legitimate correction / grievance / re-evaluation requests are **never**
  blocked.

**Planner:** the filter runs at the very top of `_plan_inner`
(`planner.py:300-313`), before greeting/grievance and every service rule,
returning `Plan(action="blocked", ...)`; it is try/except-guarded so safety can
never break normal flow.

**Engine:** `_execute_plan` handles `action == "blocked"`
(`engine.py:305-321`) — yields exactly the fixed refusal message + a `done`
event with `cited_chunks: []` and an analytics event with
`response_source="blocked"`, `llm_used/rag_used/structured_lookup_used=False`;
`state.last_intent="none"`. Nothing downstream can be reached.

**Bug found during verification (fixed):** the hard-verb regex for *fake* was
`\bfake(s|d)\b`, which did not match the bare form *"fake"* — so
*"help me fake my result document"* slipped to `student_service`. Corrected to
`\bfake(s|d)?\b|\bfaking\b`. All attack phrasings now land on `blocked`.

## 4. P2-B — status-evidence subject alignment

No new `EvidenceItem` fields. Subject alignment is derived at filter time from
`title + text + source_label` via the shared `status_subjects_of()` vocabulary:

- `is_status_authority(item, status_subjects=None)` — NOTICES require
  `item.verified` (+ alignment when subjects supplied); DOCUMENTS require a
  date and `source_label == "official_notification"`; WEBSITE requires a date
  and `verified`. Given non-empty `status_subjects`, an item whose own subjects
  do not intersect is **not** authority.
- `filter_status_evidence(items, kind="status", status_subjects=None)` — keep
  the exact P1 call contract: callers without subjects get the P1 partition
  unchanged; non-status modes never partition.

**Engine:** the status branch of `_handle_intelligent` computes
`status_subjects_of(original_query)` and passes it into
`filter_status_evidence` (`engine.py:1046-1056`). An admission question can
therefore only be grounded in dated official admission-shaped evidence — an
(irrelevant, even verified) exam notice no longer establishes an admission
state, and vice-versa. When no aligned authority exists the P1-A deterministic
short-circuit fires (`no_current_official_evidence`, no LLM).

## 5. P2-C — generalized current-status detection

**`backend/app/orchestrator/current_status.py`** (P1-created, P2-reworked):

- **Strong path:** status verb (`_STATUS_MARKER_RE`, extended to cover
  *starting / start / commences / commenced / ends / ended*) **and** a
  status-capable subject.
- **Weak cue path:** currentness cue — `current(ly) / now / still / status`
  plus Hinglish `abhi / aa gaya / aya / shuru / khatam / band / chalu /
  mil gaya / bhar sakte / ho gaya` (each combined with a capable subject) —
  **except**: evergreen reference content (*syllabus, curriculum, pattern,
  scheme, structure, framework, policy, module, credit, duration, eligibility,
  process, procedure, ...*) is excluded, and a **fee-only** subject never fires
  on a weak cue (`_WEAK_FEE_ONLY_BLOCK`).
- Subject vocabulary: admission, application (incl. apply/applying),
  registration, form, entrance, examination, result, datesheet, notification,
  scholarship, seat, counselling, semester, classes, enrolment, fee, prospectus.
- Added `status_subjects_of(text)` — shared with P2-B and P2-D alignment.

**Two planner-level bugs this exposed (fixed, still inside P2-C scope):**

1. **Comparison-route hijack.** Query-understanding preprocessing mangles
   Hinglish (*"MCA ka form abhi bhar sakte hain?"* → *"… abhi **ba date** hain?"*,
   *"band hai"* → *"**ba hall**"*), forging a phantom second programme (`ba`).
   The 2+ programme comparison rule (`Rule 3c`) then routed genuine status
   questions to `comparison`. Fix: the comparison rule now skips when the RAW
   (pre-preprocessing) message is itself a current-status question
   (`planner.py:579-586`); genuine comparisons
   (`compare mca and mba / difference between BCA and BBA / MCA vs MBA fee`)
   still route to `comparison`.
2. **Gate evaluated only on the mangled text.** The intelligent gate now
   evaluates BOTH the cleaned text and the raw message (`planner.py:722-737`),
   so Hinglish status cues survive preprocessing. English queries are
   effectively unchanged.

**P1-D binding (preserved):** *document currentness* vocabulary
(*"is this notice still valid?"*, *"are these two notices still in force?"*,
*"which notice is newer?"*) must keep the `documents` evidence path. Document
comparison/currentness is now checked **before** the status path in
`gate_intelligent` — otherwise the new `notice` subject + *still* cue would
collapse these into `status`.

## 6. P2-D — multi-intent decomposition

**`backend/app/multi_source/decompose.py`:**

- `_DEADLINE_RE` / `is_deadline_text()` — *last date / closing date / deadline /
  due date / last day / closing day* → attribute `deadline` → **source
  NOTICES** (after the schedule check, before programme attrs).
- `_PROCESS_RE` — admission/application/revaluation *process|procedure, how to
  apply, procedures?* → kept for decomposition and **sourced RAG** (the
  ProgrammeFacts catalogue has no admission-process field).
- Bare `fee` remains programme-scoped (after the exam-fee check that still
  yields EXAMINATION for *exam fee*).
- **Notice-subject alignment in `_evidence_from_notices`:** for sub-questions
  whose own `status_subjects_of(sub.text)` are non-empty (or that are deadline
  text), notices are filtered to those whose `(title + notice_type)` subjects
  intersect the sub’s subjects; when none align, the sub returns `[]` (honest
  per-aspect fallback) instead of an unrelated old notice — the KK3/A11 fix.

Verified decomposition outputs:

- *"MCA eligibility, fee, documents and last date?"* →
  `programme(eligibility) + programme(fee) + programme(documents) +
  notices(last date)` (`UNIVERSITY_KNOWLEDGE`).
- *"Tell me MCA admission process, eligibility, fee and where to apply?"* →
  `rag(process) + programme(eligibility) + programme(fee) + rag(where to apply?)`
  (`MIXED_QUERY`).
- *"MCA eligibility and exam fee?"* → `programme(eligibility) + examination(exam fee)`.
- Single-intent and service messages (`compare MCA and MBA`, `what is the MCA
  eligibility`, `show my result…`) never decompose.

## 7. Test strategy

**`backend/tests/test_student_assistant_p2.py` (new, 73 tests)** mirrors the
existing P0 (`test_student_assistant.py`) and P1
(`test_student_assistant_p1.py`) conventions:

- **P2-A:** detector unit (attacks blocked, remedies not), planner routing to
  `blocked`, and engine-level assertion that a blocked plan yields exactly the
  fixed token + `done` with `llm_used/rag_used=False` (LLM monkeypatched to
  explode so any reach is a test failure).
- **P2-B:** unit alignment (`is_status_authority` / `filter_status_evidence`
  with/without subjects, non-status no-op) + the R4 engine-level short-circuit
  (only an irrelevant verified exam notice + RAG ⇒ `no_current_official_evidence`,
  no LLM) and the positive aligned case (synthesis runs).
- **P2-C:** 20 positives (incl. all Hinglish), 13 negatives, P1-D documents
  kind preserved, planner routing for Hinglish/plain status, comparisons atomic,
  evergreen/fee never hijacked.
- **P2-D:** deadline detector, the K1 / process / exam-fee decomposition shapes,
  single-intent/service non-decompositions, planner `multi_source` routing, and
  protected routes untouched.

## 8. Regression results

| Run | Result |
|---|---|
| Baseline (P2 start) | 825 passed / 22 skipped / 0 failed |
| After P2 implementation (focused P2 file) | 73 passed / 0 failed |
| Full suite (run 1) | 897 passed / 22 skipped / 0 failed + 1 unrelated fail (passes in isolation) |
| Full suite (run 2, final) | **898 passed / 22 skipped / 0 failed** |

**Flakiness note (pre-existing, unrelated):** two `phase3c51` session-lifecycle
tests and one `student_exam_form` NL-fill test intermittently failed only under
full-suite load; each passes in isolation and in the final full run. They
exercise DI/session timing in paths this change does not touch.

## 9. Real end-to-end probes (STEP 11)

External harness `C:\Users\LENOVO\AppData\Local\Temp\opencode\p2_probe.py`
(drives the live planner + engine + DB; results in `p2_run_out.json`):

| Query | Route | Observed |
|---|---|---|
| hack portal/change grades | blocked | 0.0 s, fixed refusal message, no LLM/RAG |
| MCA ka form abhi bhar sakte hain? | status | honest `no_current_official_evidence` (live DB has no current admission notice), no LLM; ~16 s cold / evidence 3.5 s, warm thereafter ~0.1 s |
| is mca admission open? | status | 0.1 s, same honest short-circuit (0 LLM) |
| MCA eligibility, fee, documents and last date? | multi_source | 25.8 s, structured per-aspect synthesis (LLM-dominated) |

Sweep of all 24 acceptance phrases (plan-level, `p2_sweep.json`): every
expected route reproduced — blocked×5, grievance×2, status×10 (incl. Hinglish),
documents×1, comparison×2, multi_source×2, protected/structured×6.

**Latency observation (observe-only, no changes made):** the deterministic
status/blocked paths run to completion in ~0.1 s warm with **no** LLM; the
multi-source (K1) case is dominated by local llama generation (~26 s). The
information plan extra-call guard did not trigger (`plan_used=None`,
`info_plan_latency_ms=None`) on any probed acceptance query, so latency here is
attributable to the local LLM generation plus one-time cold caches — not to the
information-plan compaction path.

## 10. Git audit

Pre-existing dirty state (P1 phase, already present before this pass): **280**
entries. The tree is intentionally **not committed**; P2 changes add:

| Path | Status |
|---|---|
| `backend/app/orchestrator/safety.py` | new (untracked) — P2-A |
| `backend/app/orchestrator/current_status.py` | untracked (P1-created, P2-reworked) — P2-C |
| `backend/tests/test_student_assistant_p2.py` | new (untracked) — P2 tests |
| `backend/app/orchestrator/planner.py` | modified (was already dirty) — P2-A pre-filter, P2-C guards |
| `backend/app/orchestrator/engine.py` | modified (already dirty) — blocked handler, status-subject wiring |
| `backend/app/multi_source/evidence.py` | modified (already dirty) — P2-B alignment |
| `backend/app/multi_source/decompose.py` | modified (already dirty) — P2-D deadline/process |

**No unrelated functionality was changed.**

## 11. Limitations / honest behavior

- When no current official evidence exists, the assistant says so explicitly
  rather than repeating an old date as if current (A11 fix; verified live for
  both English and Hinglish status questions).
- The blocked boundary is deliberately narrow on soft verbs with a remedy
  frame so legitimate correction/grievance help is never refused.
- The planner-level Hinglish fixes keep genuine programme-comparison phrasing
  intact; only raw-status questions now bypass the comparison rule.