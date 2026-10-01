"""
backend/tests/test_general_knowledge.py

Curated GENERAL UNIVERSITY KNOWLEDGE layer (planner Rule 3ab-bis).

The extension adds one fast path for evergreen, non-programme-specific
admission knowledge. It is only allowed to exist as a strict SUBSET of what
the pipeline could already answer, so this file locks:

  * the data file's safety invariant — every served record is bound to a
    verified official source with an HTTPS URL, and no third-party source is
    present;
  * route priority — the curated layer sits AFTER every protected rule and
    BEFORE the intelligent / current-status gate;
  * current-status isolation — deadline / open-closed / released questions keep
    the existing status path and never reach the curated records;
  * catalogue ownership — programme-specific and short slot-fill fragments keep
    their existing routes;
  * navigation / examination / results ownership;
  * no retrieval and no planning on this path, and AT MOST ONE generation
    call, with a clean fall back to the existing knowledge path on any failure;
  * URL safety — the generation context can only ever offer the verified links.

Run:  python -m pytest tests/test_general_knowledge.py -q
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.catalogue.seed import seed_catalogue
from app.database import SessionLocal, create_all
from app.orchestrator.context import ConversationContext
from app.orchestrator.extractor import extract_entities
from app.orchestrator.general_knowledge import (
    GENERAL_KNOWLEDGE_SYSTEM_PROMPT,
    answer_from_record_ids,
    build_generation_prompt,
    knowledge_load_error,
    knowledge_records,
    knowledge_sources,
    resolve_general_knowledge,
)
from app.orchestrator.planner import plan

create_all()


@pytest.fixture(scope="module", autouse=True)
def _seed_catalogue():
    db = SessionLocal()
    try:
        seed_catalogue(db)
    finally:
        db.close()

_DATA_FILE = (
    Path(__file__).resolve().parents[1]
    / "app"
    / "orchestrator"
    / "data"
    / "general_university_knowledge.json"
)

# Domains that may authorise a fact. Anything else (aggregators, news portals,
# social pages) is a source gap, never an authority.
_ALLOWED_AUTHORITIES = frozenset({"official_university", "government_applicable"})

# Substrings that must never appear in a student-facing generated answer.
# Internal vocabulary the prompt must explicitly forbid the model from leaking.
# Retrieval technologies are a different check: the prompt must never name them
# at all, so they are deliberately absent from this tuple.
_INTERNALS = ("evidence", "verified facts", "knowledge base", "rag", "records")


def _plan_action(raw: str) -> str:
    return plan(raw, ConversationContext(), "gk-test", extract_entities(raw)).action


def _resolve(raw: str):
    return resolve_general_knowledge(raw, ConversationContext(), entities=extract_entities(raw))


def _ids(raw: str) -> set[str]:
    answer = _resolve(raw)
    return {r.id for r in answer.records} if answer else set()


# ---------------------------------------------------------------------------
# A.  Data-file safety invariants
# ---------------------------------------------------------------------------

def test_data_file_parses_and_has_records():
    assert knowledge_load_error() is None
    assert len(knowledge_records()) >= 10


def test_every_source_is_official_and_https():
    sources = knowledge_sources()
    assert sources, "the data file must declare its sources"
    for sid, src in sources.items():
        assert src.get("verified") is True, f"{sid} must be marked verified"
        assert str(src.get("url") or "").startswith("https://"), f"{sid} url"
        assert src.get("authority") in _ALLOWED_AUTHORITIES, f"{sid} authority"
        assert str(src.get("title") or "").strip(), f"{sid} title"
        assert str(src.get("reference") or "").strip(), f"{sid} reference"


def test_no_third_party_or_aggregator_source_is_present():
    """educationdunia / collegeadmission / social pages are source GAPS.

    They may hint at a question; they may never be a source of a fact.
    """
    raw = _DATA_FILE.read_text(encoding="utf-8").lower()
    for banned in ("educationdunia", "collegeadmission", "facebook", "instagram",
                   "x.com", "imtsinstitute", "shiksha", "nta.ac.in"):
        assert banned not in raw, f"non-authoritative source leaked: {banned}"


def test_records_bind_only_to_verified_https_sources():
    raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
    sources = raw["sources"]
    ids = set()
    for rec in raw["records"]:
        assert rec["id"] not in ids, f"duplicate record id {rec['id']}"
        ids.add(rec["id"])
        assert rec["answer_facts"], f"{rec['id']} has no facts"
        assert rec["sources"], f"{rec['id']} has no source"
        for sid in rec["sources"]:
            assert sid in sources, f"{rec['id']} -> unknown source {sid}"
            assert sources[sid]["verified"] is True
            assert sources[sid]["url"].startswith("https://")
        assert rec["level"] in {"undergraduate", "postgraduate", "general"}


def test_published_schedule_records_are_flagged_volatile():
    """A published schedule must be marked volatile so the prompt can warn
    the model never to call it 'currently open'."""
    for rec in knowledge_records():
        if rec.topic == "published_schedule":
            assert rec.volatile is True, f"{rec.id} must be volatile"


def test_records_carry_no_unverified_urls_in_their_facts():
    """Any URL printed to a student must be HTTPS; the official CUS site and
    the J&K Higher Education portal are the only hosts allowed."""
    allowed_hosts = ("www.cusrinagar.edu.in", "cusrinagar.edu.in", "jkadmissions.in")
    for rec in knowledge_records():
        for fact in rec.answer_facts:
            for url in re.findall(r"https?://[^\s,;)]+", fact):
                assert url.startswith("https://"), f"{rec.id}: non-https {url}"
                assert any(host in url for host in allowed_hosts), f"{rec.id}: {url}"


# ---------------------------------------------------------------------------
# B.  Resolution — the extension's own topics
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("what is the admission procedure at cluster university?", "ug_admission_process"),
        ("how do i get admission in cluster university of srinagar?", "ug_admission_process"),
        ("which website do i apply on for admission?", "ug_admission_portal"),
        ("where can i apply for ug admission?", "ug_admission_portal"),
        ("what documents are required for registration for ug admission?", "ug_required_documents"),
        ("which documents do I need to register?", "ug_required_documents"),
        ("how much is the registration fee for ug admission?", "ug_registration_fee"),
        ("on what basis is ug admission done?", "ug_admission_criteria"),
        ("is there a counselling centre for admission?", "ug_counselling_support"),
        ("what is the admission timeline for 2026?", "ug_admission_published_schedule"),
        ("how do i apply for pg admission?", "pg_admission_process"),
        ("what is the pg application fee?", "pg_application_fee"),
        ("what is the eligibility for pg admission?", "pg_eligibility_general"),
        ("is pg admission provisional?", "pg_provisional_admission"),
        ("what is the entrance test for pg admission?", "pg_entrance_test"),
        ("what is the pg admission timeline?", "pg_admission_published_schedule"),
        ("where can I find the admission prospectus?", "admission_prospectus_and_notices"),
        ("how can I contact the admissions office?", "admission_contact"),
    ],
)
def test_extension_topics_resolve(raw: str, expected: str):
    assert expected in _ids(raw), f"{raw!r} -> {_ids(raw)}"


def test_ug_and_pg_records_stay_distinct():
    """A UG question must never drag in the PG procedure and vice versa."""
    ug = _ids("how do i apply for ug admission?")
    assert "ug_admission_process" in ug
    assert "pg_admission_process" not in ug

    pg = _ids("how do i apply for pg admission?")
    assert "pg_admission_process" in pg
    assert "ug_admission_process" not in pg

    assert "pg_eligibility_general" not in _ids("what is the admission portal for ug?")
    assert "ug_admission_process" not in _ids("what is the pg application fee?")


def test_mixed_question_collects_both_levels_in_one_answer():
    answer = _resolve("what is the admission procedure, and which documents are needed?")
    assert answer is not None
    assert len(answer.records) >= 1
    assert len(answer.records) <= 3


def test_resolver_returns_none_for_unsupported_questions():
    for raw in (
        "tell me about the weather in srinagar",
        "what is the capital of France",
        "how do I bake sourdough bread",
        "xyzzy qwerty",
    ):
        assert resolve_general_knowledge(raw, ConversationContext()) is None, raw


# ---------------------------------------------------------------------------
# C.  Current-status isolation — the single most important guarantee
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    [
        "is admission open right now?",
        "are admissions still open?",
        "what is the last date for admission?",
        "has the admission notification been released?",
        "when will the admission process start?",
        "is the pg admission link active today?",
        "has the merit list been declared?",
        "was the admission deadline extended?",
    ],
)
def test_status_questions_never_reach_the_curated_records(raw: str):
    assert resolve_general_knowledge(raw, ConversationContext()) is None, raw


@pytest.mark.parametrize(
    "raw",
    [
        "is admission open right now?",
        "what is the last date to apply for admission?",
        "is the admission notification issued?",
    ],
)
def test_status_questions_keep_their_existing_status_route(raw: str):
    """The curated layer must not take a current-status question; the existing
    status / official-document route owns it."""
    action = _plan_action(raw)
    assert action in ("intelligent", "official_documents", "university_notices"), f"{raw!r} -> {action}"


def test_announced_artefact_state_questions_are_not_curated_knowledge():
    """A status marker over an announcement artefact is a state claim, not
    evergreen procedure, so the curated layer must abstain."""
    raw = "has the merit list been declared?"
    assert _plan_action(raw) != "general_knowledge"
    assert resolve_general_knowledge(
        raw, ConversationContext(), entities=extract_entities(raw),
    ) is None


# ---------------------------------------------------------------------------
# D.  Route priority and preserved ownership
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "raw",
    [
        "show my result",
        "download my admit card",
        "fill my exam form",
        "i want to submit a grievance",
        "show mca model papers",
        "show the mca date sheet",
        "i want to apply for grievance",
    ],
)
def test_protected_routes_are_never_captured(raw: str):
    assert _plan_action(raw) != "general_knowledge", raw


def test_curated_layer_never_captures_programme_specific_questions():
    for raw in (
        "what is the MCA admission procedure?",
        "what is the admission procedure for BCA?",
        "which documents are required for admission to mca?",
        "how much is the mba tuition fee?",
    ):
        assert _plan_action(raw) != "general_knowledge", raw


def test_short_programme_less_fragment_keeps_the_slot_fill():
    # Locked by the pre-existing student-assistant suite: the targeted
    # "which programme?" clarification is the better answer here.
    raw = "documents required for admission"
    p = plan(raw, ConversationContext(), "gk", extract_entities(raw))
    assert (p.action, p.target) == ("slot_fill", "programme")
    assert resolve_general_knowledge(
        raw, ConversationContext(), entities=extract_entities(raw),
    ) is None


def test_catalogue_still_owns_the_dyd_programme_profile():
    """A programme-profile question is not admission-procedure knowledge."""
    raw = "what is the Design Your Degree programme?"
    assert _plan_action(raw) != "general_knowledge"
    assert resolve_general_knowledge(
        raw, ConversationContext(), entities=extract_entities(raw),
    ) is None


def test_navigation_and_greetings_are_untouched():
    for raw in ("hi", "hello", "menu", "home", "back", "options"):
        assert _plan_action(raw) != "general_knowledge", raw


def test_examination_and_results_vocabulary_is_untouched():
    for raw in (
        "what is the exam date sheet?",
        "when will the results be declared?",
        "what is the mca examination fee?",
    ):
        assert _plan_action(raw) != "general_knowledge", raw


def test_planner_payload_carries_the_resolved_records():
    p = plan(
        "how do i apply for pg admission?",
        ConversationContext(),
        "gk",
        extract_entities("how do i apply for pg admission?"),
    )
    assert p.action == "general_knowledge"
    assert p.extra and p.extra["records"]
    assert p.extra["sources"]
    assert p.confidence >= 0.9


# ---------------------------------------------------------------------------
# E.  Engine — one generation call, no retrieval, clean degradation
# ---------------------------------------------------------------------------

class _Gate:
    def __init__(self) -> None:
        self.held = 0
        self.acquires = 0

    async def acquire(self, timeout: float = 0.0):
        self.acquires += 1
        self.held = 1
        return True

    def release(self) -> None:
        self.held = 0


def _drive_engine(monkeypatch, raw: str, chunks, *, gate_ok: bool = True, capture: dict | None = None):
    """Run _execute_plan for `raw` with retrieval and generation instrumented."""
    from app.orchestrator import engine as eng
    from app.orchestrator.state import ConversationState

    calls = {"stream": 0, "run_chat": 0, "plan_info": 0, "collect": 0, "systems": [], "contexts": []}
    gate = _Gate()
    if not gate_ok:
        async def _no_gate(timeout: float = 0.0):
            calls["stream"] += 0
            return False
        gate.acquire = _no_gate  # type: ignore[method-assign]

    async def _fake_stream(question, context, system=None, **kw):
        calls["stream"] += 1
        calls["systems"].append(system)
        calls["contexts"].append(context)
        for c in chunks:
            yield c

    async def _fake_run_chat(db, user_id, message, chat_id, context=None, **kw):
        calls["run_chat"] += 1
        yield {"type": "token", "text": "RAG-FALLBACK"}
        yield {"type": "done", "chat_id": chat_id, "cited_chunks": []}

    async def _fake_plan_information(*a, **kw):
        calls["plan_info"] += 1
        return None

    async def _stub_collect_event(**kw):
        calls["collect"] += 1
        if capture is not None:
            capture.update(kw)
        return None

    monkeypatch.setattr("app.ingest.generator.stream_answer_async", _fake_stream)
    monkeypatch.setattr("app.llm.gate.shared_llm_gate", gate)
    monkeypatch.setattr(eng, "run_chat", _fake_run_chat)
    monkeypatch.setattr(eng, "collect_event", _stub_collect_event)
    monkeypatch.setattr("app.orchestrator.info_plan.plan_information", _fake_plan_information)

    raw_entities = extract_entities(raw)
    plan_result = plan(raw, ConversationContext(), "gk-engine", raw_entities)
    state = ConversationState(chat_id="gk-engine")
    events: list[dict] = []

    async def _run():
        async for ev in eng._execute_plan(
            None, "u", raw, "gk-engine", state, ConversationContext(), raw_entities, plan_result,
        ):
            events.append(ev)

    asyncio.run(_run())
    return calls, events, state, gate


def test_engine_makes_exactly_one_generation_call_and_no_retrieval(monkeypatch):
    captured: dict = {}
    calls, events, _state, gate = _drive_engine(
        monkeypatch,
        "how do i apply for pg admission?",
        ["Step 1. Visit the CUS website. ", "Step 2. Select PG Admissions 2026."],
        capture=captured,
    )
    assert calls["stream"] == 1, "at most ONE generation call is allowed"
    assert calls["run_chat"] == 0, "this path must not retrieve"
    assert calls["plan_info"] == 0, "this path must not run an information plan"
    assert gate.held == 0, "the shared LLM gate slot must be released"
    assert calls["collect"] == 1
    assert captured.get("llm_used") is True
    assert captured.get("rag_used") is None
    assert captured.get("response_source") == "general_knowledge"
    tokens = " ".join(e.get("text", "") for e in events if e.get("type") == "token")
    assert "PG Admissions 2026" in tokens
    assert any(e.get("type") == "done" for e in events)


def test_engine_uses_the_curated_prompt_and_only_verified_links(monkeypatch):
    calls, _events, _state, _gate = _drive_engine(
        monkeypatch,
        "which website do i apply on for admission?",
        ["Apply at https://jkadmissions.in/ [Source: CUS UG Admission 2026-27 Official Notice]"],
    )
    assert calls["systems"] == [GENERAL_KNOWLEDGE_SYSTEM_PROMPT]
    context = calls["contexts"][0]
    sources = knowledge_sources()
    for url in re.findall(r"https?://[^\s,;)|]+", context):
        assert url.startswith("https://")
        assert any(url in str(s["url"]) or url.rstrip("/") in str(s["url"]) for s in sources.values()), url
    # The only portal the curated facts may offer.
    assert "jkadmissions.in" in context


def test_generation_failure_falls_back_to_the_existing_knowledge_path(monkeypatch):
    from app.ingest.generator import GenerationError

    async def _boom(question, context, system=None, **kw):
        raise GenerationError("ollama down")
        yield  # pragma: no cover - marks _boom as an async generator

    calls = {"stream": 0, "run_chat": 0}
    gate = _Gate()

    async def _fake_run_chat(db, user_id, message, chat_id, context=None, **kw):
        calls["run_chat"] += 1
        yield {"type": "token", "text": "RAG-FALLBACK"}
        yield {"type": "done", "chat_id": chat_id, "cited_chunks": []}

    from app.orchestrator import engine as eng
    from app.orchestrator.state import ConversationState

    monkeypatch.setattr("app.ingest.generator.stream_answer_async", _boom)
    monkeypatch.setattr("app.llm.gate.shared_llm_gate", gate)
    monkeypatch.setattr(eng, "run_chat", _fake_run_chat)

    raw = "how do i apply for pg admission?"
    entities = extract_entities(raw)
    plan_result = plan(raw, ConversationContext(), "gk-fail", entities)
    events: list[dict] = []

    async def _run():
        async for ev in eng._execute_plan(
            None, "u", raw, "gk-fail", ConversationState(chat_id="gk-fail"),
            ConversationContext(), entities, plan_result,
        ):
            events.append(ev)

    asyncio.run(_run())
    assert calls["run_chat"] == 1, "a generation failure must degrade to the RAG path"
    assert gate.held == 0
    tokens = " ".join(e.get("text", "") for e in events if e.get("type") == "token")
    assert "RAG-FALLBACK" in tokens


def test_gate_timeout_falls_back_without_a_second_call(monkeypatch):
    calls, events, _state, gate = _drive_engine(
        monkeypatch,
        "how do i apply for pg admission?",
        ["never reached"],
        gate_ok=False,
    )
    assert calls["stream"] == 0
    tokens = " ".join(e.get("text", "") for e in events if e.get("type") == "token")
    assert "RAG-FALLBACK" in tokens
    assert gate.held == 0


def test_poisoned_generation_collapses_to_the_safe_fallback(monkeypatch):
    from app.multi_source.synthesize import MISSING_EVIDENCE_FALLBACK

    leak = (
        "Here are the answers to the questions you asked.\n\n"
        "1. - Source (Prospectus.pdf): not a document.\n"
    )
    _calls, events, _state, _gate = _drive_engine(
        monkeypatch, "how do i apply for pg admission?", [leak],
    )
    tokens = " ".join(e.get("text", "") for e in events if e.get("type") == "token")
    assert "Prospectus.pdf" not in tokens
    assert tokens.strip() == MISSING_EVIDENCE_FALLBACK


# ---------------------------------------------------------------------------
# F.  Prompt + generation-prompt contract
# ---------------------------------------------------------------------------

def test_generation_prompt_only_offers_verified_sources():
    answer = _resolve("how do i apply for pg admission?")
    assert answer is not None
    question, context = build_generation_prompt("how do i apply for pg admission?", answer)
    assert question == "how do i apply for pg admission?"
    for rec in answer.records:
        for fact in rec.answer_facts:
            assert fact in context
    # Internal record identifiers must never reach the model: they invite it to
    # echo field names and internal structure into the student-facing answer.
    for rec in answer.records:
        assert rec.id not in context
    for src in answer.sources:
        assert src["title"] in context
        assert src["url"] in context
    # Nothing from outside the resolved records may appear.
    for rec in knowledge_records():
        if rec not in answer.records:
            for fact in rec.answer_facts:
                assert fact not in context


def test_generation_prompt_labels_the_level_of_each_record():
    answer = _resolve("how do i apply for pg admission?")
    assert answer is not None
    _q, context = build_generation_prompt("q", answer)
    assert "Postgraduate (PG)" in context


def test_prompt_forbids_inventing_facts_and_internal_terms():
    prompt = GENERAL_KNOWLEDGE_SYSTEM_PROMPT
    assert "ONLY source of truth" in prompt
    assert "Never invent" in prompt
    for term in _INTERNALS:
        assert term in prompt.lower(), f"prompt must forbid leaking {term!r}"
    # The prompt must never name a retrieval technology as a reason for an answer.
    for banned in ("chroma", "bm25", "embedding", "vector store"):
        assert banned not in prompt.lower(), f"prompt must not mention {banned!r}"


def test_prompt_requires_published_schedules_to_stay_in_the_past_tense():
    prompt = GENERAL_KNOWLEDGE_SYSTEM_PROMPT.lower()
    assert "published schedules" in prompt
    assert "never describe a published schedule as proof" in prompt


# ---------------------------------------------------------------------------
# G.  Cost — resolution is deterministic and free
# ---------------------------------------------------------------------------

def test_resolution_performs_no_llm_or_retrieval_call(monkeypatch):
    """The resolver is pure matching: no generator, no retriever, no planner."""
    from app.chat import service as chat_service

    def _explode(*a, **kw):  # pragma: no cover - must never run
        raise AssertionError("the resolver must not call the LLM or retrieval")

    monkeypatch.setattr("app.ingest.generator.stream_answer_async", _explode)
    monkeypatch.setattr(chat_service, "retrieve", _explode)
    for raw in (
        "how do i apply for pg admission?",
        "what documents are required for registration for ug admission?",
        "what is the admission procedure at cluster university?",
    ):
        assert _resolve(raw) is not None, raw


def test_records_are_cached_in_memory():
    first = knowledge_records()
    second = knowledge_records()
    assert first is second, "records must be parsed once and reused"


def test_resolver_is_robust_to_odd_input():
    for raw in ("", "   ", "a", "?", "x" * 500, "admission" * 60):
        resolve_general_knowledge(raw, ConversationContext())
    # A broken context object must not raise.
    assert resolve_general_knowledge("how do i apply for pg admission?", SimpleNamespace()) is not None


# ---------------------------------------------------------------------------
# H.  END-TO-END through the real orchestration entry point
# ---------------------------------------------------------------------------
#
# Everything above calls the resolver (or plan()) directly. That was NOT
# enough: the reported live bug reproduced only through the full
# engine.process() boundary, where the planner first rewrites the user's text
# with process_query_understanding() (which fuzzy-corrects "work" -> "worth",
# "cluster" -> "clustr"). A resolver that works in isolation can therefore be
# unreachable in production. These tests drive the REAL entry point.

#: The exact query reported from the live browser.
LIVE_QUERY = "what is admission procedure at cluster university"


def _no_retrieval(monkeypatch):
    """Make every retrieval layer explode, so a RAG regression cannot hide."""

    def _explode(*a, **kw):  # pragma: no cover - must never run
        raise AssertionError("retrieval must not run for a matched general question")

    from app.chat import service as chat_service
    import app.ingest.retrieve as retrieve_mod
    import app.ingest.retriever as retriever_mod

    # Chroma + BM25 + reranking all live behind these.
    monkeypatch.setattr(retriever_mod, "hybrid_search", _explode)
    monkeypatch.setattr(retriever_mod, "retrieve_hybrid", _explode)
    monkeypatch.setattr(retriever_mod, "rerank", _explode)
    monkeypatch.setattr(retrieve_mod, "retrieve_hybrid", _explode)
    # The RAG answer path itself.
    monkeypatch.setattr(chat_service, "retrieve", _explode)


def _drive_live(monkeypatch, raw: str, chat_id: str) -> list[dict]:
    """Run the real engine._process() and collect the SSE-shaped events."""
    from app.orchestrator import engine as eng

    calls = {"llm": 0}

    async def _fake_stream(question, context, system=None, **kw):
        calls["llm"] += 1
        yield "1. Apply online at the official admission portal."

    monkeypatch.setattr("app.ingest.generator.stream_answer_async", _fake_stream)
    _no_retrieval(monkeypatch)

    events: list[dict] = []

    async def _run():
        async for ev in eng._process(
            None, "u-test", raw, chat_id, _fresh_state(chat_id)
        ):
            events.append(ev)

    asyncio.run(_run())
    _last_llm_calls["n"] = calls["llm"]
    return events


def _fresh_state(chat_id: str):
    from app.orchestrator.state import ConversationState

    return ConversationState(chat_id=chat_id)


_last_llm_calls: dict[str, int] = {"n": 0}


def test_live_query_reaches_general_knowledge_end_to_end(monkeypatch):
    """The exact reported query must be answered by the curated layer.

    Asserts the whole production contract in one go: the planner selects the
    general-knowledge route, the admission-procedure record is matched, the
    answer is non-empty, retrieval/Chroma/BM25/rerank never run, no evidence
    block is produced, and exactly ONE LLM call is made.
    """
    plan_result = plan(LIVE_QUERY, ConversationContext(), "gk-live", extract_entities(LIVE_QUERY))
    assert plan_result.action == "general_knowledge", plan_result.reason
    matched = [r["id"] for r in (plan_result.extra or {}).get("records") or []]
    assert "ug_admission_process" in matched, matched

    events = _drive_live(monkeypatch, LIVE_QUERY, "gk-live-e2e")
    text = "".join(e.get("text", "") for e in events if e.get("type") == "token")
    assert text.strip(), "the live route produced no answer text"

    done = [e for e in events if e.get("type") == "done"]
    assert done, "the live route never completed"
    # No RAG evidence block may reach the student.
    assert not done[0].get("cited_chunks"), done[0].get("cited_chunks")

    # Exactly one answer-generation call.
    assert _last_llm_calls["n"] == 1, _last_llm_calls["n"]


def test_live_official_url_is_preserved_in_the_generation_context():
    """Section 13: the clickable link must come from stored verified data."""
    answer = _resolve(LIVE_QUERY)
    assert answer is not None
    _question, context = build_generation_prompt(LIVE_QUERY, answer)
    urls = re.findall(r"https://[^\s)\]]+", context)
    assert urls, "the verified official link was lost"
    for url in urls:
        assert url.startswith("https://")


@pytest.mark.parametrize(
    "raw",
    [
        "How do I apply for admission?",
        "How does UG admission work?",
        "How does PG admission work?",
        "What documents are required for UG admission?",
        "What documents are required for PG admission?",
    ],
)
def test_required_general_questions_are_served_without_retrieval(monkeypatch, raw: str):
    """Section 9/20: the listed general questions must not run full RAG."""
    plan_result = plan(raw, ConversationContext(), "gk-req", extract_entities(raw))
    assert plan_result.action == "general_knowledge", f"{raw!r} -> {plan_result.reason}"

    events = _drive_live(monkeypatch, raw, "gk-req-e2e")
    done = [e for e in events if e.get("type") == "done"]
    assert done and not done[0].get("cited_chunks")
    assert _last_llm_calls["n"] == 1


@pytest.mark.parametrize(
    "raw,forbidden",
    [
        ("are admissions open?", {"general_knowledge"}),
        ("what is the last date for admission?", {"general_knowledge"}),
        ("has the merit list been declared?", {"general_knowledge"}),
        ("has the selection list been released?", {"general_knowledge"}),
        ("is registration open right now?", {"general_knowledge"}),
        ("what is the Design Your Degree programme?", {"general_knowledge"}),
        ("examination fee of mca", {"general_knowledge"}),
    ],
)
def test_protected_routes_are_not_hijacked_by_the_general_layer(raw: str, forbidden: set):
    """Section 10/24: current-status, catalogue and examination ownership."""
    assert _plan_action(raw) not in forbidden, f"{raw!r} -> {_plan_action(raw)}"


# ---------------------------------------------------------------------------
# Answer completeness: the curated facts must actually carry the procedure.
# ---------------------------------------------------------------------------


def _facts_for(record_id: str) -> list[str]:
    for rec in knowledge_records():
        if rec.id == record_id:
            return list(rec.answer_facts)
    raise AssertionError(f"record {record_id} missing")


def test_ug_admission_process_facts_carry_every_published_stage():
    """The UG notice defines a real staged process; all of it must be servable."""
    facts = " ".join(_facts_for("ug_admission_process")).lower()
    for stage in (
        "register",
        "preference",
        "first merit list",
        "round 1",
        "second merit list",
        "round 2",
        "formalities",
        "fee",
        "spot round",
        "classwork",
    ):
        assert stage in facts, f"UG process fact is missing stage: {stage}"
    # The process must be presentable as ordered steps, not one prose sentence.
    ug = _facts_for("ug_admission_process")
    steps = [f for f in ug if f.lower().startswith("step ")]
    assert len(steps) >= 6, f"expected an ordered procedure, got {len(steps)} steps"


def test_ug_admission_process_does_not_assert_cycle_dates():
    """Dates belong to the volatile published-schedule record, not the process."""
    blob = " ".join(_facts_for("ug_admission_process")).lower()
    for month in ("january", "february", "march", "april", "may", "june", "july"):
        assert month not in blob, f"process record must not carry a {month} date"


def test_ug_portal_record_does_not_claim_dyd_only_wording():
    """'any other mode shall not be entertained' is DYD wording, not UG wording."""
    blob = " ".join(_facts_for("ug_admission_portal")).lower()
    assert "not be entertained" not in blob
    assert "any other mode" not in blob
    # The UG wording that IS in the notice must still be present.
    assert "apply online" in blob


def test_dyd_published_schedule_is_served_and_marked_volatile():
    """DYD published dates exist in its notice and need their own record."""
    recs = {r.id: r for r in knowledge_records()}
    assert "dyd_admission_published_schedule" in recs
    rec = recs["dyd_admission_published_schedule"]
    assert rec.volatile is True
    blob = " ".join(rec.answer_facts)
    assert "22 May 2026" in blob and "12 June 2026" in blob
    assert "not a live confirmation" in blob
    # A DYD date question must reach the curated layer without retrieval.
    assert _plan_action("what are the important dates for dyd admission?") == "general_knowledge"
    assert "dyd_admission_published_schedule" in _ids(
        "what are the important dates for dyd admission?"
    )


def test_dyd_schedule_record_does_not_steal_generic_ug_schedule_questions():
    """Adding a DYD schedule record must not hijack non-DYD date questions."""
    for raw in ("what is the admission timeline?", "admission dates for ug"):
        assert "dyd_admission_published_schedule" not in _ids(raw), raw


# ---------------------------------------------------------------------------
# Prompt: completeness and no meta-opener.
# ---------------------------------------------------------------------------


def test_prompt_demands_complete_answers_not_minimal_ones():
    low = GENERAL_KNOWLEDGE_SYSTEM_PROMPT.lower()
    assert "be concise" not in low, "prompt must not ask for brevity"
    assert "answer completely" in low
    assert "numbered list" in low
    assert "markdown link" in low


def test_prompt_forbids_source_meta_commentary():
    low = GENERAL_KNOWLEDGE_SYSTEM_PROMPT.lower()
    assert "excerpts" in low, "the prompt must forbid naming the material handed to it"
    # The model echoed a banned phrase verbatim when the prompt spelled it out
    # as a negative example, so the prompt must not contain the exact strings
    # it is trying to suppress.
    assert "the provided excerpts" not in low
    assert "based on the provided information" not in low
    assert "knowledge base" in low  # already banned, keep it banned


def test_generation_context_does_not_leak_record_identifiers():
    """Raw record ids invite the model to echo internal structure at students."""
    for raw in (
        "what is the admission process at cluster university?",
        "what documents are required for ug admission?",
        "how do i apply for pg admission?",
    ):
        answer = _resolve(raw)
        assert answer is not None, raw
        _q, context = build_generation_prompt(raw, answer)
        for rec in answer.records:
            assert rec.id not in context, f"{rec.id} leaked for {raw!r}"


# ---------------------------------------------------------------------------
# Compound / multi-intent: one context, one call, no per-clause retrieval.
# ---------------------------------------------------------------------------


def test_compound_curated_question_serves_both_clauses_without_retrieval(monkeypatch):
    raw = "what is the admission process and what documents are required for PG?"
    plan_result = plan(raw, ConversationContext(), "gk-compound", extract_entities(raw))
    assert plan_result.action == "general_knowledge", plan_result.reason
    record_ids = (plan_result.extra or {}).get("curated_record_ids")
    assert record_ids, "compound question must carry its resolved record ids"
    assert "ug_admission_process" in record_ids
    assert "pg_required_documents" in record_ids

    events = _drive_live(monkeypatch, raw, "gk-compound-e2e")
    done = [e for e in events if e.get("type") == "done"]
    assert done and not done[0].get("cited_chunks"), "compound path must not retrieve"
    assert _last_llm_calls["n"] == 1, "compound question must use exactly one call"


def test_compound_curated_context_contains_both_clauses():
    raw = "what is the admission process and what documents are required for PG?"
    plan_result = plan(raw, ConversationContext(), "gk-compound-ctx", extract_entities(raw))
    answer = answer_from_record_ids((plan_result.extra or {})["curated_record_ids"])
    assert answer is not None
    _q, context = build_generation_prompt(raw, answer)
    # The procedure half and the documents half must both be in ONE context.
    assert "register" in context.lower()
    assert "upload" in context.lower()


@pytest.mark.parametrize(
    "raw",
    [
        "what is the MCA eligibility, duration and exam fee?",
        "what is the msc physics eligibility and the mba admission fee?",
    ],
)
def test_compound_programme_questions_are_not_diverted_to_the_curated_layer(raw: str):
    """Mixing programme data must keep the existing multi-source/structured route."""
    ids = _ids(raw)
    assert "ug_admission_process" not in ids, raw
    assert "pg_required_documents" not in ids, raw


# ---------------------------------------------------------------------------
# SSE wire format: a streamed newline must survive to the client.
# ---------------------------------------------------------------------------


def _eventsource_payload(frame: str) -> str:
    """Rebuild one SSE event's data the way EventSource does: fields joined by \\n."""
    fields = [
        line[5:][1:] if line[5:].startswith(" ") else line[5:]
        for line in frame.split("\n")
        if line.startswith("data:")
    ]
    return "\n".join(fields)


@pytest.mark.parametrize(
    "token",
    [
        "\n",            # a newline-only token between two list items
        "\n\n",
        "1. All aspirants must register\n",
        "\n2. Then the merit list is published\n",
        "plain text",
        "trailing newline\n",
    ],
)
def test_sse_frame_round_trips_every_streamed_token(token: str):
    """splitlines() dropped the terminator, so list breaks vanished on the wire.

    The symptom was numbered steps running together
    ("...preferences there.2. The portal publishes...") for the real browser
    client, not just for a test harness.
    """
    from app.chat.routes import _sse

    assert _eventsource_payload(_sse(None, token)) == token


def test_sse_numbered_steps_keep_their_breaks():
    from app.chat.routes import _sse

    tokens = ["1. Register on the portal.\n", "\n", "2. Merit list is published.\n"]
    rebuilt = "".join(_eventsource_payload(_sse(None, t)) for t in tokens)
    assert rebuilt == "1. Register on the portal.\n\n2. Merit list is published.\n"
    assert "portal.\n\n2." in rebuilt


# ---------------------------------------------------------------------------
# Level scoping: "for PG" must reach the PG record, never silently the UG one.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "what is the admission process for pg?",
        "what is the admission procedure for postgraduate?",
        "what is the admission process for masters?",
    ],
)
def test_level_scoped_admission_process_reaches_that_level(raw: str):
    assert "pg_admission_process" in _ids(raw), raw
    assert "ug_admission_process" not in _ids(raw), raw


def test_generic_admission_process_stays_on_the_undergraduate_record():
    assert "ug_admission_process" in _ids("what is the admission process?")


def test_compound_pg_question_also_carries_the_pg_process():
    """The PG compound question must not answer the process half with UG steps."""
    raw = "what is the admission process and what documents are required for PG?"
    plan_result = plan(raw, ConversationContext(), "gk-compound-pg", extract_entities(raw))
    record_ids = (plan_result.extra or {}).get("curated_record_ids") or []
    assert "pg_admission_process" in record_ids, record_ids
    assert "pg_required_documents" in record_ids, record_ids
    answer = answer_from_record_ids(record_ids)
    _q, context = build_generation_prompt(raw, answer)
    assert "New Registration" in context, "PG steps must be in the context"





