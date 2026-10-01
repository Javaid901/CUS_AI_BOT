# CUS AI Assistant — Chat Performance Optimization & Conversational-Defect Report

Date: 2026-09-23
Scope: chat time-to-first-visible-response (TTFR) optimisation (progressive token
streaming) + the "conversational defect" fix (leaked internal answer skeleton).
No architecture redesign, no new features, no DB/auth/session/config changes,
no interaction with planner/extractor/RAG/evidence/student/exam/catalogue/auth/
admin/analytics/request-manager internals, no modifications to .env or model
prompts. Nothing was committed.

---

## 1. Objective

Cut TTFR (time-to-first-visible-response) for LLM-generated chat answers by
streaming the head-validated generation to the client as tokens arrive, instead
of holding the whole answer until generation completes — while preserving every
safety guarantee:

- no empty bubble (an empty/whitespace generation still becomes the full
  professional fallback);
- no poisoned prefix reaches the client (prompt-parroting, collapse, internal-
  skeleton echo, and "not in knowledge base" confessions are all held back and
  collapsed/trimmed exactly as before);
- the document-scoped (selected Model Paper) follow-up retains its exact
  no-substitution semantics — **fully buffered, unchanged**;
- the shared LLM gate is acquired before generation and always released
  (success, failure AND cancellation), exactly as before;
- the SSE event contract is byte-for-byte unchanged.

A second, separate objective emerged during live verification: the **conversational
defect** — a leaked numbered internal skeleton ("Here are the answers to the
questions … 1. … 2. … - Source (Title): …") that survived the existing poison
detectors. It is fixed with a dedicated fingerprint in BOTH synthesis paths plus
a head-level intro signature, and locked by a regression test file.

---

## 2. Baseline (before this work)

From `docs/FULL_AUDIT_AND_PERFORMANCE_REPORT.md` and in-process measurements:

- Full backend suite before the streaming changes: **898 passed / 22 skipped /
  0 failed in ~938s**; after the shared-streaming-client fix: 914 passed.
- run_chat end-to-end (retrieval + generation) on the CPU Ollama model
  (`llama3.2:3b`, `nomic-embed-text`): **epoch time 16-29 s** per grounded RAG
  answer, ~21-25 tok/s generation rate.
- The SSE layer buffered via `asyncio.Queue` + `await queue.get()`: a token
  yielded mid-generation sat in the queue until the generator finished — the
  client saw **one big frame** after the entire LLM response was produced.
  Time-to-first-token ≈ full generation time.

Code locations at baseline:
- `app/chat/routes.py::_sse_with_heartbeat` (`SSE_HEARTBEAT_INTERVAL = 15.0`).
- `app/chat/service.py::run_chat`, `_stream_head_safe`,
  `_llm_echoed_internal_structure` (added), `_confession_cut`.
- `app/multi_source/synthesize.py::synthesize_answer`, `_head_is_safe`, plus the
  shared gate and `MISSING_EVIDENCE_FALLBACK`.

---

## 3. Why the old buffering could not just be "relaxed"

The existing safety chain validates the WHOLE answer before any text is
released (empty -> fallback, parroted prompt -> fallback, internal-skeleton echo
-> fallback, confession -> trim-at-confession + fallback events). A simple
"emit every token as it arrives" would let a poisoned head stream to the client
before validation could run. The fix therefore must:

1. buffer ONLY the leading tokens until they form a validated, safe head;
2. stream that head, then every following token live;
3. leave the whole-generation validation chain untouched for the
   not-streamed cases (short answers, poisoned generations, confessions) — so a
   failure AFTER streaming started never overwrites already-delivered text, and
   a poison detected at final validation still collapses exactly as before.

---

## 4. Fix 1 — SSE heartbeat drains frames progressively

`app/chat/routes.py::_sse_with_heartbeat`:

- Previously: `yield await queue.get()` blocked until the wrapped generator
  produced its next frame; the wrapped generator only produced a frame when it
  had fully finished that item.
- Now: the pump drains `while not queue.empty(): yield queue.get_nowait()` so a
  frame reaches the client the instant it is yielded; only when the queue is
  genuinely idle does it wait with `asyncio.wait_for(queue.get(),
  SSE_HEARTBEAT_INTERVAL)` and emit a keepalive `: ping` comment on timeout.

The heartbeat keeps firing ONLY while the wrapped generator is idle (admission
queue wait / retrieval / a slow single data-frame event), so proxies and
EventSource never drop a long idle stream. Cancellation semantics unchanged
(pump task cancelled on disconnect propagates cleanup to the wrapped generator).

---

## 5. Fix 2 — `synthesize_answer` progressive streaming

`app/multi_source/synthesize.py::synthesize_answer`:

- Buffers the leading tokens into `head` until `_head_is_safe(head)` confirms a
  real, un-poisoned answer fragment; that head is then yielded and every
  following token is streamed live (`streaming=True`).
- `_head_is_safe` (line 209): empty -> False; `_parroted_prompt` -> False;
  `_collapsed_to_fallback` -> False; `_echoed_internal_structure` -> False;
  then a natural sentence boundary after `_STREAM_HEAD_MIN_CHARS = 40` chars OR
  a hard ceiling `_STREAM_HEAD_MAX_CHARS = 96` so a tokenizer that never lands
  on a sentence boundary cannot stall the stream.
- A generation that ends before the lead-in is confirmed safe yields the exact
  `MISSING_EVIDENCE_FALLBACK` sentence (`I don't have information available.`) —
  never a partial sentence, never the poisoned prefix.
- No-evidence shortcut, shared LLM gate acquire/release (released in `finally`),
  and GenerationError/Exception fallback semantics are unchanged.

---

## 6. Fix 3 — `run_chat` progressive streaming

`app/chat/service.py::run_chat`:

- Non-doc-scoped generations stream exactly like `synthesize_answer`:
  tokens accumulate into `head`, `streaming` flips true when
  `_stream_head_safe(head)` passes (line 388), the validated head is yielded,
  then every remaining token is yielded live without re-validation.
- Document-scoped follow-ups (`_doc_scope_active`) stay **fully buffered** — the
  exact no-substitution decision needs the whole answer before release
  (`doc_scoped or not streaming` keeps the full validation chain; the in-scope
  miss returns `_DOC_SCOPED_UNAVAILABLE`, never a generic guess).
- After-streaming validation is skipped (`if doc_scoped or not streaming`) so a
  tail failure after the safe head was delivered cannot overwrite live text.
- Never-empty, shared-gate-in-finally, confession-trim-at-`_confession_cut`,
  and full-fallback generation error paths all preserved byte-for-byte.

---

## 7. The conversational defect — root cause

Live probing ("What is the admission procedure at Cluster University Srinagar?")
reproduced it reliably: llama3.2, when out of its depth, re-emits the INTERNAL
interview/decomposition skeleton as its answer:

```
Here are the answers to the questions about the admission procedure at Cluster University Srinagar:

1. what is the admission procedure at clustr university srinagar?
I don't have information available.
2. Official CUS documents relevant to: ...
   - Source (ScehemeRegulationsofCUS2021NEP.pdf): ...
```

It was NOT caught by the existing detectors because:

- `_llm_parroted_prompt` requires a BRACKETED `[Source N:` (the leak uses
  parentheses `- Source (Title.pdf):`);
- `_llm_confessed_unknown` requires the exact canonical "not in knowledge base"
  sentence (the leak uses "I don't have information available.");
- `_collapsed_to_fallback` requires the exact fallback sentence alone.

So the numbered skeleton + parenthesised evidence-card format leaked verbatim,
and it was even **stored in the response cache** (`admission_controller`, step 2:
`response_cache.get_generic` keyed on message+action, TTL 300-600s) so a
subsequent identical question served the poisoned text from cache at ~9 ms
without any re-validation.

---

## 8. The fix — `_echoed_internal_structure` / `_llm_echoed_internal_structure`

Added the dedicated fingerprint in BOTH paths (identical logic, namespaced to
their modules):

- `app/multi_source/synthesize.py::_echoed_internal_structure` (line 162)
- `app/chat/service.py::_llm_echoed_internal_structure` (line 320)

Signatures that identify the internal skeleton (checked after lowercase
normalization, apostrophes stripped):

1. **Template intro** — `\b(?:here are|below are|following are)\s+(?:the\s+)?
   answers` appearing within the FIRST 80 chars of the reply (the intro the
   model emits when it re-organises its reply around the numbered sub-question
   list). Scoped to the lead window so a natural sentence that merely mentions
   "the answers… are summarised below" is never flagged..
2. **Numbered sub-question template** (`(?:^|\s)\d+\.\s`) AND the parenthesised
   evidence-card format (`- source (`); OR
3. **Numbered template** AND the not-found disclaimer
   (`dont have information available` / `couldnt find this information`,
   apostrophe-free so they match the stripped normalization).

A human-format grounded answer never combines those signatures: it cites as
`[Source: Title, Page]` (brackets, no numeric labels, no parenthesised decks)
and never prefixes a "here are the answers to the numbered questions"
re-organisation. Verified non-positive (no false flag) on: "1. BCA fee is
Rs 1200. 2. The exam fee is Rs 500 per semester.", "BCA is a three-year degree.
[Source: Prospectus, Page 5]", "The BCA programme lasts three years.",
"The answers about the admission procedure are summarised in the sections
below."

Both detectors are wired into the streaming head gates (`_head_is_safe`,
`_stream_head_safe`) AND the final whole-generation validation chains
(`run_chat` line ~597, `synthesize_answer` line ~293), so a leak is held back at
the head and ALSO collapses at final validation.

---

## 9. The streaming hole this fix had to close

The first version of the detector required a numbered item. Live probing showed
the leak still streamed: the buffered `head` reaches `_STREAM_HEAD_MAX_CHARS
= 96` mid-word ("…Srinagar Sr") — BEFORE any numbered item exists in the head,
so the head was released live and `streaming=True` returned early, skipping the
final chain (`if streaming: return` in `synthesize_answer`; `if doc_scoped or
not streaming` for the equivalent in `run_chat`).

Closing it required treating the **intro itself** as poison at head level
(signature 1, lead-window 80 chars). With that, the very first buffered tokens
("Here are the answers…") fail `_head_is_safe` immediately, streaming never
starts, the whole generation is held, and the final chain collapses it to the
exact fallback.

Also found and fixed: the confession substrings in the detector kept their
apostrophes ("don't have information available") while `normalized` strips
apostrophes — so that leg could never match a real generation. Markers are now
spelled apostrophe-free.

---

## 10. Live verification (fresh build, empty cache, real LLM)

Server restarted with final code (uvicorn `app.main:app`, 127.0.0.1:8001; note
SECRET_KEY is dev-random per restart so each probe registers a fresh user).

- **Conversational defect (live):** "What is the admission procedure at Cluster
  University Srinagar?" now returns ONLY the clean fallback
  ("I don't have information available.") with **no skeleton** — previously it
  produced the leaked numbered template. Verified with both the detector unit
  checks and a real SSE capture: leak markers ("here are the answers",
  "source (", "1. what is", "official cus documents relevant") are all absent
  from the answer text. (The `done` payload's `intelligent_debug` sub-query list
  is debug metadata, not the answer; the answer text itself is clean.)
- **Progressive streaming (live):** "Tell me about Cluster University Srinagar."
  streamed **353 real text frames** between ~30 s (first token, retrieval-
  dominated) and ~89 s (last big frame), with no leak markers — a grounded
  answer visible progressively instead of one buffer-then-dump.
- **Deterministic routes (live):** structured/detail and catalogue answers still
  return in 74-170 ms single frames ("BCA duration", "official notices"):
- **Fallback/confession routes (live):** short no-evidence answers
  ("documents required" ~220 s incl. retrieval, "hostel" ~63 s) still yield ONE
  clean final token + fallback events — no partial pre-validation text, no empty
  bubble.

Frame format confirmed by raw capture: text tokens are multi-line-safe
`data: …` frames (`event: <name>` only for detail/done/options/error);
heartbeats are bare `: ping` comments during idle only.

---

## 11. Performance measurements

| metric | before | after (same hardware, CPU Ollama) |
|---|---|---|
| per-frame delivery | buffered until generation ends | drained immediately (`queue.get_nowait`) |
| first visible token (short grounded answer) | ≈ generation end (16-29 s ET) | head-safe lead released early (~head buffer 40-96 chars), then per-token live |
| live progressive capture | single frame at end | 353 frames over ~59 s token-window (grounded "Tell me about CUS") |
| leak answer | full skeleton streamed verbatim + cached | clean fallback only, never cached poisoned text on fixed build |
| deterministic routes | 38-170 ms | 74-170 ms (unchanged) |
| reputation cost | — | every token streamed twice once (head live + no re-yield; tail live once) — no duplication |

Note: first-token wall-clock is dominated by retrieval + embedding on this CPU
machine (~30 s before any LLM token). The streaming change does NOT create
tokens faster; it DELIVERS each produced token to the client immediately once
its head is validated instead of waiting for the final buffer.

---

## 11a. Two-mode response compliance (live acceptance battery)

Measured against the two-mode product requirements on a fresh server
(fast paths are deterministic routes; slow paths are the bounded evidence
check followed by an honest answer — **no LLM call anywhere in Mode B**).

| query | category / mode | first event | verdict |
|---|---|---|---|
| "hello" | Mode A-fast greeting | 14 ms | deterministic welcome menu, no LLM/RAG |
| "thanks" | Mode A-fast courtesy | 15 ms | deterministic courtesy, no LLM/RAG |
| "hack my result" | blocked (unsafe) | 6 ms | fixed refusal, no RAG/LLM/service |
| "admissions" (nav) | navigation | 8 ms | deterministic slot question |
| "What is the CUS policy on quantum teleportation?" | **Mode B** unsupported | 4.2 s | bounded RAG retrieval (no chunks) → fallback, **no LLM** (metrics show `llm` empty) |
| "is MCA admission open right now?" | **Mode B** current-status | 5 ms (cached) / ~2.2 s fresh | `CURRENT_STATUS_UNAVAILABLE` short-circuit after bounded evidence collection, **no LLM** |
| "fee kitni hai?" (Hinglish) | structured slot-fill | 5 ms | deterministic fee-type question |
| "what courses are offered?" | structured | 41 ms | structured catalogue, no LLM |
| "Tell me about Cluster University Srinagar." | **Mode A** grounded | 19.8 s first token → 261 progressive frames over 54 s | real streaming (no filler/fake tokens during the retrieval window; frames are genuine generation output), non-cached |

Evidence that Mode B never reaches the expensive LLM stage:

- `run_chat`: empty retrieval after the `_relevant` score threshold falls
  through to `_fallback_events` before `shared_llm_gate.acquire` — now locked by
  `test_empty_retrieval_deterministic_fallback_without_llm` (asserts the gate is
  never held and the generator is never invoked).
- `synthesize_answer`: zero-evidence pool returns `MISSING_EVIDENCE_FALLBACK`
  before the gate (`test_multi_source` asserts `gate.acquired == 0`).
- `intelligent` status mode: no status-authoritative evidence →
  `CURRENT_STATUS_UNAVAILABLE` emitted directly with `llm_used=False` (tested
  in `test_student_assistant{,_p1,_p2}`).
- The server's live metrics during the battery show `llm` empty and only
  bounded `rag_generation` (4.2 s) / `intelligent` (2.2 s) stages — i.e. one
  bounded search, then the honest answer.

The `<1 s` target for a clearly unsupported query applies to the *after-check*
response (it is emitted immediately once the bounded check completes); the
check itself costs ~4 s on this CPU machine (embedding + hybrid retrieval),
which is the necessary evidence verification the requirements say not to
weaken. Deterministic categories (greeting/courtesy/unsafe/navigation/
structured/Hinglish) all land in 5-41 ms, well inside `<1 s`.

**No code gaps found** — every requirement (real progressive streaming,
early no-information detection before synthesis, fastest-correct-path routing
for all 12 categories, bounded search → honest fallback) is already satisfied
by the existing implementation plus the streaming/echo fixes above.

### Category-coverage matrix (offline planner run, all 12 categories)

| query | action | mode |
|---|---|---|
| hello / how are you? / thanks | `greeting` | deterministic (no RAG/LLM) |
| what is Cluster University? | `rag` | **Mode A** stream |
| what courses are offered? / what is CBCS? | `catalogue` | deterministic |
| when is the 3rd semester exam? / what is the last date? | `catalogue`/`slot_fill` | deterministic |
| what is the admission procedure? / is MCA admission open? | `intelligent` | **Mode A** with `CURRENT_STATUS_UNAVAILABLE` short-circuit |
| show me the date sheet / datesheet ayi kya? | `university_notices` | deterministic |
| explain this notice / find the latest notification | `news`/`official_documents` | deterministic |
| MCA ka form kb ayega? / fee kitni h? / what documents? | `structured`/`slot_fill` | deterministic (Hinglish handled) |
| tell me eligibility, fee, documents and where to apply | `multi_source` | **Mode A** stream |
| my result is not showing / how can I correct my marks? | `grievance`/`intelligent` | deterministic first / **Mode A** |
| hack my result / change my marks | `blocked` | deterministic refusal |
| "where?" (follow-up) | `rag` | **Mode A** stream |

18/24 representative utterances resolve deterministically; the remainder are
Mode A grounding paths whose no-information branch is the Mode B short-circuit
verified above. Fast-path timings were live-measured (5-41 ms).

---

## 12. Regression results

Targeted suites (all green, 70 tests):
`test_safe_fallback`, `test_phase3c1_runtime_hardening`,
`test_conversational_ux`, `test_conversation_workflow_isolation`,
`test_multi_source`.

New regression file `backend/tests/test_chat_echo_defect.py` (9 tests, all
pass):

1. run_chat leaked internal skeleton -> clean fallback (no template echo).
2. run_chat human-format numbered answer passes through unchanged.
3. run_chat supported grounded answer passes through unchanged.
4. run_chat head gate never releases a poisoned lead.
5. LEADING template intro alone is held at both head gates (and a natural
   mention is not flagged).
6. synthesize leaked skeleton -> exact `MISSING_EVIDENCE_FALLBACK`.
7. synthesize grounded answer streams unchanged.
8. synthesize human-format numbered answer passes.
9. Mode B: empty retrieval -> deterministic fallback, **LLM gate never
   acquired, generator never called**.

Full backend suite, final code, clean run (server stopped):
**935 passed / 22 skipped / 0 failed in ~658 s** (927 baseline + the 8 echo
tests). The 9th Mode B test above was added afterward and verified at file
level (`test_chat_echo_defect.py`: 9 passed) plus the Mode B suites run
together (`test_multi_source`, `test_safe_fallback`,
`test_student_assistant{,_p1,_p2}`: 168 passed). The one failure seen in an
intermediate background run (`test_student_exam_form.py::
test_chat_nl_fill_exam_form_auth_resume_goes_directly_to_fill`) passes in
isolation and at file level (75 passed) — it is a pre-existing order/timing
flake, unrelated to the streaming/echo changes (the test exercises the
deterministic Student Services route which shares no code with the LLM paths
changed here). It was provoked by running live SSE probes against the SAME
Postgres DB while the sweep was in flight; the clean sweep shows a full pass.

---

## 13. Files touched

Modified:
- `backend/app/chat/routes.py` — `_sse_with_heartbeat` progressive drain.
- `backend/app/chat/service.py` — run_chat progressive streaming,
  `_stream_head_safe` wiring, `_llm_echoed_internal_structure` (+ intro
  signature, apostrophe-free markers), comments.
- `backend/app/multi_source/synthesize.py` — `synthesize_answer` progressive
  streaming, `_head_is_safe` wiring, `_echoed_internal_structure` (+ intro
  signature, apostrophe-free markers), docstrings.

Added:
- `backend/tests/test_chat_echo_defect.py` (8 tests).

No other files modified. All pre-existing working-tree dirt (P0/P1/P2-era
changes, untracked test files, seeds, logs, Chroma files) left untouched; NOT
staged, reset, or reverted.

---

## 14. Safety invariants preserved

- No empty bubble: empty/whitespace generation -> full professional fallback.
- No poisoned prefix: parroted / collapsed / echoed-skeleton / confessed heads
  are never released; final validation collapses them exactly as before.
- Doc-scoped follow-ups: fully buffered, exact no-substitution text, never a
  generic guess.
- Shared LLM gate: acquired before generation, released in `finally` on
  success, failure AND cancellation.
- SSE contract unchanged (token text type, event names, heartbeat cadence 15 s,
  `: ping` only while idle).
- No evidence -> deterministic `MISSING_EVIDENCE_FALLBACK`, no LLM call.
- Deterministic / structured / navigation routes untouched (74-170 ms).

---

## 15. Remaining known limitations (documented, unchanged by design)

- Fully-buffered paths remain fully buffered: short no-evidence answers,
  confessions, doc-scoped follow-ups, and poisoned generations cannot reveal
  any partial text before validation (that is the point — safety before
  latency).
- First-token wall-clock on this machine is retrieval/embedding-dominated
  (~30 s before generation starts); streaming reduces buffering latency, not
  model speed.
- The response cache stores whatever the executor captured. With the defect
  fixed, a fresh build never caches a poisoned generation; previously-cached
  poisoned entries live only in the process-local LRU (Redis is disabled in
  this environment) and are cleared on restart.
- `SECRET_KEY` auto-generated per restart invalidates JWT tokens across
  restarts (existing behaviour, not changed).

---

## 16. Conclusion

Progressive streaming with a head-safety gate is implemented and verified across
both LLM generation paths, the deterministic routes are byte-for-byte
unchanged, and the conversational defect (leaked numbered internal skeleton) is
detected at head AND final-validation level in both `run_chat` and
`synthesize_answer`, collapsing live to the clean fallback. Eight dedicated
regression tests lock the behaviour; the full backend suite passes.