# P1 — Trustworthy Intelligent Path : Implementation Report

Status: **COMPLETE**. All P0 behavior preserved; every focused group and the
full suite pass. No DB migration, no protected-route change, no second
planner / context / RAG / generator / evidence framework, and at most ONE extra
LLM call (for genuinely complex knowledge questions only) were introduced.

Run: `python -m pytest tests -q` → **825 passed, 22 skipped, 0 failed (9:38)**.

---

## 1. Implementation summary (order followed)

| Phase | Goal | Change | Verified |
|-------|------|--------|----------|
| 1 | P1-A current-status evidence isolation | `evidence.py` helpers + engine short-circuit | tests 1–4 |
| 2/3 | P1-B info-plan schema/validator + integration | `info_plan.py` + engine plan step | tests 6–13 |
| 4 | Procedure source-support | rule 11 in `prompts.py` | tests 14–15 |
| 5 | P1-D document comparison/currentness | gate `"documents"` + subs + comparison prompt | tests 16–18 |
| 6 | P1-C analytics | `collect_event` fields + `collect_performance` + structured log | test 34 |
| 7 | Full regression + diff audit | `pytest tests` | 825 passed |

## 2. P1-A — status-evidence isolation (design)

Deterministic, metadata-only partition (`is_status_authority`,
`filter_status_evidence` in `backend/app/multi_source/evidence.py`) applied on
the `status` kind inside `_handle_intelligent`:

- **NOTICES** → always authority (announcement-shaped by construction).
- **DOCUMENTS** → authority only when dated **and** `source_label ==
  "official_notification"`. Evergreen `other_official_document` (regulations,
  schemes, syllabi) is context-only.
- **WEBSITE** → authority only when `issued_at` (sync date) **and** verified.
- **PROGRAMME** and **RAG** → never authority (background only).

No authority items ⇒ deterministic short-circuit: exact
`CURRENT_STATUS_UNAVAILABLE` sentence, **zero LLM calls**,
`state.last_intent = "knowledge"`, debug key
`short_circuit = "no_current_official_evidence"`, `llm_used=False`. When
authority exists the synthesis receives `synthesis_context` with
`mark_status=True` and `authority_ids` (provenance labels), so "dated X says
open" — never "probably/happens every year" — is the only way a current claim
forms. "Not found" is never converted into "not announced".

## 3. P1-A — acceptance behavior matrix (all tested)

| Scenario | Evidence set | Behavior |
|----------|--------------|----------|
| Current dated admission notice | NOTICES (issued) | synthesizes with marked evidence; `authority_sources=["notices"]` |
| Old prospectus / regulations only | `other_official_document` + RAG | short-circuits; honest fallback; no LLM |
| Programme profile "eligibility…" + generic RAG "happens every year" | PROGRAMME + RAG only | short-circuits; never "open" |
| Dated official fee notice vs revised notice | 2× `official_notification` dated | conflict surfaced via `conflict_notes`, not silently picked |
| Dated verified website "applications open" | WEBSITE issued+verified | authority (labeled) |
| Evergreen regulations dated | DOCUMENTS other_official_document | context-only (regulations ≠ announcement) |
| Undated / unverified website | WEBSITE no issued_at | context-only |
| Non-status kinds (knowledge, documents) | anything | `filter_status_evidence` is a no-op |

## 4. P1-B — information plan (schema & validation)

New `backend/app/orchestrator/info_plan.py` (a small deterministic helper —
engine is ~2500 lines; justified as its own module).

- `InfoPlan`: `mode`, `needs_current`, `required_facts`, `source_preferences`
  (frozen dataclass + `as_dict`).
- Allowed mode: fact / procedure / status / comparison / general
  (`"document"` accepted as an alias for `"comparison"`).
- Bounds: **≤6** required facts, **≤5** source preferences, **≤4** extra RAG
  fragments (`_MAX_FACT_SUBS`), **45 s** generation timeout (`_PLAN_TIMEOUT`).
- `parse_info_plan` is defensive: non-JSON, unknown mode/fact/source,
  oversized or empty-but-claimed facts/sources ⇒ `None`. Leading prose before
  a JSON object is tolerated; duplicates and surplus entries within bounds are
  trimmed deterministically.
- `plan_information` uses the **same** `shared_llm_gate` (timeout
  `settings.MAX_SEMAPHORE_WAIT`), the **same** `stream_answer_async` and a
  **plain-message** prompt (`INFORMATION_PLAN_SYSTEM_PROMPT`). JSON-only output;
  the LLM never picks a route, never invents URLs or source names, never asks
  for unbounded retrieval.
- `expand_subs_with_plan` only *appends* bounded RAG sub-questions keyed on the
  planned facts; it never removes a deterministic source and never rewrites.

## 5. P1-B — guard & call-count guarantee

`should_information_plan` fires **only** for genuinely complex intelligent
*knowledge* questions: **≥3 distinct fact markers** (eligibility / documents /
fee / application_process / deadline / selection / duration / requirements),
**or ≥2 markers with a compound separator** (`and`, `,`, `;`, `also`,
`including`). Verified cases:

- `"MCA eligibility, documents, fee, admission process and last date?"` → True
- `"what is the MCA eligibility, admission process and fee?"` → True
- `"explain the mca admission procedure"`, `"how do i apply for mca"`,
  `"what documents do i need for mca?"` → **False** (exact P0 call count)
- `"are admissions open for mca?"` (status kind) → **False**
- All protected/deterministic messages → **False** (never enters planning)

## 6. P1-B — failure / degradation behavior (all tested)

| Failure | Result |
|---------|--------|
| Gate busy (`acquire` False) | plan `None`, stream never called |
| `GenerationError` | plan `None` |
| Malformed / non-JSON output | plan `None` |
| Timeout (45 s exceeded; test uses 0.05 s) | plan `None` |
| Engine-level plan returns `None` | subs unchanged (5-source base), `synthesize_answer` called with **no** `context` kwarg, `plan_used=False`, `information_plan=None` |

A plan failure can never become a chat failure and never costs more than the
one extra call. The structured synthesis wrapper (`engine.py`) passes
`context=` only when non-`None` — the P0 `_fake_synth`-style tests that accept
no `context` kwarg stay green.

## 7. Phase 4 — procedure source-support

`STUDENT_ASSISTANT_SYSTEM_PROMPT` rule 11: derive every step **from verified
evidence** (documents/notices), never present a fixed/assumed step list, and
default every unverified step to explicit "not specified" acknowledgement.
Engine test proves a `procedure` plan's `required_facts` (documents, fee,
deadline) reach synthesis via `expected_facts` and drive that honest
acknowledgement path.

## 8. P1-D — document comparison & currentness

- `gate_intelligent` returns **`"documents"`** for comparison/currentness
  questions (new `_DOCUMENT_COMPARISON_RE`; checked after current-status,
  before fee disambiguation; survives `is_university_related`).
- `build_intelligent_subs(kind="documents")` puts the **NOTICES** view first
  and phrases the documents sub-question to force *published dates*
  ("Official CUS notices and notifications with their published dates…") so
  comparison is date-told, never invented supersession/cancellation.
- Documents mode appends `DOCUMENT_COMPARISON_RULES` to the system prompt
  (applied only for this kind), and `dated_notice_conflicts` feeds
  `conflict_notes` so genuine dated opposition is surfaced.
- **Routing honesty**: phrasing already deterministically served by the P0
  routes is never hijacked — `"compare these two notifications"` /
  `"which notification should I follow?"` keep `official_documents`; bare
  notice lookups keep `news`; date sheets keep `university_notices`;
  programme comparisons keep `comparison`; fee-exam keeps `structured`.

## 9. P1-C — analytics & observability

Schema-safe (no DB change; `InteractionEvent` columns are fixed): both
`collect_event` call sites on the intelligent path now record
`service_requested=kind`, `detected_service=kind`, `llm_used`, `rag_used`,
`structured_lookup_used` and `conversation_completed` (short-circuit records
`llm_used=False`). `collect_performance` adds `intelligent_evidence`
(always) and `intelligent_plan` (only when a plan ran) stage samples. A
structured `log.info("intelligent-analytics …")` line records
`task_kind / needs_current / no_current_info / used_llm / evidence_sources /
evidence_conflicts / information_plan_used` for both modes. Test 34 asserts the
event fields end-to-end.

## 10. P0 architecture intact (audit)

No reset / stash / revert / delete performed; the working tree's pre-existing
uncommitted state was preserved throughout. No changes to: auth/authorization,
student services, results, admit card, exam form, grievance, examination
services, model papers, date sheets, authority, catalogue, college, Website
Sync, Redis, request manager, DB schema, existing RAG/retriever, conversation
context, planner decision rules (only the P1 gate branch documented above),
generator, frontend, or SSE contracts.

## 11. Protected-route verification (P1 enabled)

`tests/test_student_assistant_p1.py::test_document_gate_never_hijacks_protected_or_generic`
repeats the protected negatives with P1 active: student_service (result /
exam form), university_notices (date sheet), official_documents (notification
comparisons), comparison, news, grievance, structured fee handling — **none**
touch the intelligent/information-plan path. Planner-level negatives are also
covered in `test_university_notices_routing.py` (green) so
`information_plan_used == false` holds for every deterministic route.

## 12. Tests added

`backend/tests/test_student_assistant_p1.py` — **34 tests**:
1–3 status isolation units; 4 conflict-unit; 5 status short-circuit (engine);
6 status-with-notice synthesis; 7 guard conservatism; 8 parse valid/normalized;
9 parse invalids (parametrized ×10); 10 plan via existing gate; 11 degradation
(parametrized ×4: gate_busy/generation/malformed/timeout); 12 plan expands subs
+ facts; 13 plan failure → exact P0; 14 procedure prompt contract; 15 procedure
plan → facts; 16 documents routing (parametrized ×5); 17 no-hijack negatives
(parametrized ×11); 18 documents subs; 19 documents engine (comparison prompt
+ conflict_notes); 20 intelligent analytics event fields.

## 13. Focused results

| Run | Result |
|-----|--------|
| `test_student_assistant_p1.py` | **34 passed** (~20 s) |
| `test_student_assistant.py` + `test_university_notices_routing.py` + `test_intelligence_taxonomy.py` | **47 passed** (P0 routing unaffected after the P1-D gate) |
| `test_multi_source.py` | 31 passed (earlier run) |
| `test_smart_orchestrator.py test_intelligence.py test_p1a_conversation_context.py` | 56 passed (earlier run) |

## 14. Full-suite regression

`python -m pytest tests -q` → **825 passed, 22 skipped, 0 failed, 577.81 s.**
Baseline was 789 passed / 22 skipped / 2 failed, where the 2 were
aggregate-only session-lifecycle timing flakes and one pre-existing
examination-service seed-mismatch test. **Neither reproduced today**; nothing
was modified to make them pass — they are timer/seed sensitive and were left
untouched per the "do not fix unrelated problems" rule.

## 15. Performance / latency observations

- No global increase to `num_predict`, generation timeout, concurrency, or
  retrieval `K`. Guarded complex questions pay at most **one** extra LLM call,
  bounded by the existing gate and a 45 s generation cap; on any failure the
  P0 single-message cost is unchanged.
- Pre-existing dev LLM synthesis latency (55–208 s) is unchanged; the plan step
  itself adds only `plan_latency_ms` observable output. Full-suite wall time
  (~9.6 min) is in the P0 range.
- If production plan-latency ever trends toward the 45 s cap the structured
  `intelligent_plan` performance samples will make it visible in the existing
  analytics without code changes.

## 16. Bugs found & fixed during this work

1. **P1-D gate regex (introduced, fixed)**: patterns 1–2 of
   `_DOCUMENT_COMPARISON_RE` required `s?\b(?:is|are)` immediately after the
   noun, so real prose (`"notice is newer"`) never matched and comparison
   questions fell through to `news`. Whitespace (`\s+`) inserted before the
   verb/comparative and the first window widened to 40 chars. Caught by the
   new routing tests.
2. **Route-overlap discovery (not a bug)**: `official_documents` (Rule 3ab)
   legitimately owns "notification"-comparison phrasing before the intelligent
   gate. The gate deliberately does not fight it — the canonical-documents
   repo answers those (added as a no-hijack regression assertion).

## 17. Known issues for P2

- `is_university_related`'s semantic classifier conservatively gates some
  comparison phrasing to `None` (e.g. long "…fee notice or …" clauses fall to
  `grievance`/`news` today). Fine as conservative P0 behavior; a P2 could
  widen the semantic university-related net — requires intent-model retraining,
  out of scope.
- The two timer/seed-sensitive tests (section 14) should ideally be made
  deterministic in a future hardening pass (explicitly out of P1 scope).

## 18. Files changed / untouched (exact list)

P1 changes (all additive; each file's P0 content preserved):
- `backend/app/multi_source/evidence.py` — `is_status_authority`,
  `filter_status_evidence`, `dated_notice_conflicts`, `_STATUS_AUTHORITY_DOC_TYPES`.
- `backend/app/orchestrator/info_plan.py` — **new**: `InfoPlan`,
  `should_information_plan`, `parse_info_plan`, `expand_subs_with_plan`,
  `plan_information`.
- `backend/app/orchestrator/engine.py` — `_handle_intelligent` P1 wiring
  (isolation/short-circuit, plan, documents, conflict_notes, context,
  analytics), `collect_performance` import.
- `backend/app/orchestrator/current_status.py` — `_DOCUMENT_COMPARISON_RE` +
  `"documents"` branch (file is untracked in git; created in the P0 work).
- `backend/app/multi_source/decompose.py` — `"documents"` kind branch in
  `build_intelligent_subs`.
- `backend/app/multi_source/synthesize.py` — optional `context` on
  `format_evidence_block`/`synthesize_answer` (default None ⇒ identical P0).
- `backend/app/ingest/prompts.py` — rule 11, `DOCUMENT_COMPARISON_RULES`,
  `INFORMATION_PLAN_SYSTEM_PROMPT`.
- `backend/tests/test_student_assistant_p1.py` — **new**: 34 tests.

Untouched by P1 (verified): all protected/specialised services, catalogue,
college, authority(-_admin), grievance, knowledge_sync, request_manager,
analytics service, DB models/schema, frontend, SSRF/SSE, generator, retriever,
conversation context, planner rules other than the gate branch above.

## 19. Git diff audit (summary)

`git status`: ~190 modified + ~80 untracked files — all **pre-existing**
(uncommitted working-tree state from earlier phases; nothing was
reset/stashed/reverted by this work). `git diff --stat` bottom line:
`15 files changed, 1606 insertions(+), 65 deletions(-)` for tracked files —
most of which is prior-phase work in `evidence.py`, `engine.py`, `planner.py`,
`prompts.py`. The four P1-relevant files that are brand-new/untracked
(`current_status.py`, `info_plan.py`, `test_student_assistant.py`,
`test_student_assistant_p1.py`) do not appear in `diff --stat` precisely
because they are untracked. No `git add`/`commit` was performed (not
requested).