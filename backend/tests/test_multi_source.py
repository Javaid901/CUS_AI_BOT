"""
backend/tests/test_multi_source.py

Phase 3C-6 — Intelligent Multi-Source Answering.

Covers the full multi-source pipeline:
  decomposition  →  evidence collection  →  validation  →  LLM synthesis
  and its integration with the existing planner + orchestration engine.

Key non-regressions enforced here:
  * single-source / dedicated-service questions are NEVER hijacked (examination
    fee, division improvement, date sheets, comparisons, general knowledge);
  * evidence is authoritative and per-source; structured data beats RAG;
  * missing evidence → the EXACT fallback sentence, no LLM fabrication;
  * SSO-style SSE contract (token + done, provenance-only) is preserved.

Run:  python -m pytest tests/test_multi_source.py -q
"""

from __future__ import annotations

import asyncio
import datetime
import json
import uuid
from types import SimpleNamespace

import pytest

from app.database import SessionLocal, create_all
from app.catalogue.seed import seed_catalogue
from app.orchestrator.context import ConversationContext
from app.orchestrator.extractor import extract_entities
from app.orchestrator.planner import plan
from app.multi_source.decompose import (
    SourceType,
    SubQuery,
    decompose_query,
    query_category,
)
from app.multi_source.evidence import (
    EvidenceItem,
    EvidencePool,
    collect_evidence,
    validate,
)
from app.multi_source.synthesize import (
    MISSING_EVIDENCE_FALLBACK,
    build_question,
    format_evidence_block,
    synthesize_answer,
)

create_all()


# ---------------------------------------------------------------------------
# Seed data
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def _seed_catalogue():
    db = SessionLocal()
    try:
        seed_catalogue(db)
        db.commit()
    finally:
        db.close()


def _cleanup(db, *models_and_ids):
    for model, row_id in models_and_ids:
        try:
            if row_id:
                db.query(model).filter(model.id == row_id).delete()
        except Exception:
            db.rollback()
    try:
        db.commit()
    except Exception:
        db.rollback()


def _seed_exam_fee_page():
    from app.models import WebsitePage

    db = SessionLocal()
    page = None
    try:
        page = WebsitePage(
            id=str(uuid.uuid4()),
            url="https://cusrinagar.edu.in/exams/fee-structure",
            title="Examination Fee Structure 2026",
            content=(
                "The examination fee for MCA semester examinations is Rs 1200 "
                "per semester. Exam form fee details are published by the examination wing."
            ),
            category="official",
            status="new",
            classification_status="verified",
        )
        db.add(page)
        db.commit()
        db.refresh(page)
        return db, page
    except Exception:
        db.rollback()
        db.close()
        raise


def _seed_date_sheet():
    from app.models import DateSheetEntry, UniversityNotice
    from sqlalchemy import delete

    db = SessionLocal()
    notice = None
    entry = None
    try:
        db.execute(delete(DateSheetEntry))
        db.commit()
        notice = UniversityNotice(
            id=uuid.uuid4(),
            title="MCA 3rd Semester Date Sheet 2026",
            notice_type="date_sheet",
            filename="mca_ds.pdf",
            file_path="/tmp/mca_ds.pdf",
            file_size=1024,
            sha256="a" * 64,
            extraction_status="verified",
            programme_ids=json.dumps(["mca"]),
            is_verified=True,
            is_published=True,
            published_at=datetime.datetime.utcnow(),
            created_at=datetime.datetime.utcnow(),
            updated_at=datetime.datetime.utcnow(),
        )
        db.add(notice)
        db.flush()
        entry = DateSheetEntry(
            notice_id=notice.id,
            row_no=1,
            programme_id="mca",
            semester="3",
            subject="Data Structures",
            exam_date="2026-01-15",
            extraction_status="verified",
        )
        db.add(entry)
        db.commit()
        db.refresh(notice)
        db.refresh(entry)
        return db, notice, entry
    except Exception:
        db.rollback()
        db.close()
        raise


# ---------------------------------------------------------------------------
# Fake LLM gate / generator
# ---------------------------------------------------------------------------

class _FakeGate:
    def __init__(self):
        self.acquired = 0
        self.released = 0

    async def acquire(self, timeout=0.0):
        self.acquired += 1
        return True

    def release(self):
        self.released += 1


def _patch_synthesis(monkeypatch, tokens=("Canned answer.",), fake_stream=None):
    """Patch the synthesis module's gate + generator with deterministic fakes."""
    gate = _FakeGate()
    monkeypatch.setattr("app.multi_source.synthesize.shared_llm_gate", gate)

    async def _stream(question, context, system=None):
        for token in tokens:
            yield token

    monkeypatch.setattr(
        "app.multi_source.synthesize.stream_answer_async",
        fake_stream or _stream,
    )
    return gate


# ---------------------------------------------------------------------------
# A.  Decomposition & routing (planner stays authoritative)
# ---------------------------------------------------------------------------

def _plan_action(raw: str, ctx: ConversationContext | None = None):
    ctx = ctx or ConversationContext()
    e = extract_entities(raw)
    return plan(raw, ctx, f"ms-{uuid.uuid4()}", e)


def test_single_source_exam_fee_not_rerouted():
    """A lone exam-fee question keeps the dedicated examination service."""
    p = _plan_action("what is the MCA exam fee")
    assert p.action != "multi_source"
    assert p.action == "examination", f"expected examination, got {p.action}"


def test_division_improvement_single_task_not_rerouted():
    """'division improvement + how many papers' is ONE knowledge task → the
    dedicated examination service handles it (multi-source must not split it)."""
    p = _plan_action("what is division improvement and how many papers can I improve")
    assert p.action != "multi_source"
    assert p.action == "examination", f"expected examination service, got {p.action}"


def test_general_knowledge_single_not_rerouted():
    p = _plan_action("what is a database index?")
    assert p.action != "multi_source"


def test_protected_service_keyword_not_rerouted():
    p = _plan_action("fill exam form")
    assert p.action == "student_service"


def test_comparison_not_rerouted():
    p = _plan_action("compare MCA and MBA")
    assert p.action != "multi_source"


def test_three_part_question_decomposes():
    q = "What is the eligibility for MCA, what is the exam fee, and what subjects are in the programme?"
    p = _plan_action(q)
    assert p.action == "multi_source", p.reason
    subs = [SubQuery(**s) for s in p.extra["multi_source"]]
    assert len(subs) == 3
    assert {s.source for s in subs} == {SourceType.PROGRAMME, SourceType.EXAMINATION}
    assert p.extra["category"] == "UNIVERSITY_KNOWLEDGE"


def test_attribute_list_decomposes():
    q = "What is the MCA eligibility, duration and exam fee?"
    p = _plan_action(q)
    assert p.action == "multi_source", p.reason
    subs = [SubQuery(**s) for s in p.extra["multi_source"]]
    assert len(subs) == 3
    assert any(s.source == SourceType.EXAMINATION for s in subs)
    assert any("duration" in s.text for s in subs)


def test_two_source_decomposes():
    q = "What is the MCA eligibility and exam fee?"
    p = _plan_action(q)
    assert p.action == "multi_source", p.reason
    subs = [SubQuery(**s) for s in p.extra["multi_source"]]
    assert {s.source for s in subs} == {SourceType.PROGRAMME, SourceType.EXAMINATION}


def test_date_sheet_and_subjects_decomposes():
    q = "When is the MCA 3rd semester exam and what subjects are included?"
    p = _plan_action(q)
    assert p.action == "multi_source", p.reason
    subs = [SubQuery(**s) for s in p.extra["multi_source"]]
    assert {s.source for s in subs} == {SourceType.NOTICES, SourceType.PROGRAMME}


def test_structured_plus_rag_decomposes():
    q = "What are the MCA subjects and what is the admission process?"
    p = _plan_action(q)
    assert p.action == "multi_source", p.reason
    subs = [SubQuery(**s) for s in p.extra["multi_source"]]
    assert {s.source for s in subs} == {SourceType.PROGRAMME, SourceType.RAG}


def test_mixed_general_and_university_category():
    q = "what is database normalization and which MCA subjects cover it?"
    subs = decompose_query(q)
    assert subs is not None
    assert any(s.source == SourceType.PROGRAMME for s in subs)
    assert query_category(subs) == "MIXED_QUERY"


def test_single_source_decompose_returns_none():
    for q in ("what is the MCA eligibility", "tell me about MCA"):
        assert decompose_query(q) is None


def test_bare_programme_fragments_do_not_inflate_decomposition():
    # "mca and mba" tails are programme names, not extra information needs.
    q = "what are the subjects in MCA and MBA"
    assert decompose_query(q) is not None or True  # single-sub -> None expected
    subs = decompose_query(q)
    assert subs is None, "multi-programme aggregate must not trigger multi-source"


# ---------------------------------------------------------------------------
# B.  Evidence collection
# ---------------------------------------------------------------------------

def test_programme_evidence_collected():
    sub = SubQuery(text="what is the mca eligibility", source=SourceType.PROGRAMME)
    ctx = ConversationContext()
    db = SessionLocal()
    try:
        pool = asyncio.run(collect_evidence(db, [sub], extract_entities(sub.text), ctx))
        items = pool.for_sub(sub.text)
        assert items, "programme evidence must be non-empty"
        assert items[0].source == SourceType.PROGRAMME
        assert items[0].direct is True
        assert "eligibility" in items[0].text.lower()
        assert "MCA" in items[0].title
    finally:
        db.close()


def test_examination_fee_evidence_collected():
    db, page = _seed_exam_fee_page()
    try:
        sub = SubQuery(text="what is the mca exam fee", source=SourceType.EXAMINATION)
        ctx = ConversationContext()
        pool = asyncio.run(collect_evidence(db, [sub], extract_entities(sub.text), ctx))
        items = pool.for_sub(sub.text)
        assert items, "exam-fee evidence must be non-empty"
        assert items[0].source == SourceType.EXAMINATION
        assert "Rs 1200" in items[0].text or "1200" in items[0].text
        assert "Examination Fee" in items[0].title
    finally:
        _cleanup(db, (page.__class__, page.id))
        db.close()


def test_notices_date_sheet_evidence_collected():
    db, notice, entry = _seed_date_sheet()
    try:
        sub = SubQuery(
            text="when is the mca 3rd semester exam", source=SourceType.NOTICES
        )
        ctx = ConversationContext()
        pool = asyncio.run(collect_evidence(db, [sub], extract_entities(sub.text), ctx))
        items = pool.for_sub(sub.text)
        assert items, "date-sheet evidence must be non-empty"
        assert items[0].source == SourceType.NOTICES
        assert "Data Structures" in items[0].text
        assert "2026-01-15" in items[0].text
    finally:
        _cleanup(db, (entry.__class__, entry.id), (notice.__class__, notice.id))
        db.close()


def test_rag_evidence_collected(monkeypatch):
    sub = SubQuery(text="what is the admission process for mca", source=SourceType.RAG)
    rag_item = EvidenceItem(
        sub_question=sub.text,
        source=SourceType.RAG,
        text="Admission to MCA requires a BCA/BSc degree.",
        title="Admission Prospectus 2026",
        source_id="doc-1",
        relevance=0.9,
        direct=True,
    )

    def _fake_rag(s, rag_ctx=None, top_k=4):
        return [rag_item]
    monkeypatch.setattr(
        "app.multi_source.evidence._evidence_from_rag", _fake_rag
    )
    ctx = ConversationContext()
    pool = asyncio.run(collect_evidence(None, [sub], extract_entities(sub.text), ctx))
    items = pool.for_sub(sub.text)
    assert items and items[0].text == rag_item.text
    assert items[0].source == SourceType.RAG


def test_rag_missing_evidence_marks_missing(monkeypatch):
    sub = SubQuery(text="what is the admission process for mca", source=SourceType.RAG)

    def _no_rag(s, rag_ctx=None, top_k=4):
        return []

    monkeypatch.setattr("app.multi_source.evidence._evidence_from_rag", _no_rag)
    ctx = ConversationContext()
    pool = asyncio.run(collect_evidence(None, [sub], extract_entities(sub.text), ctx))
    result = validate(pool, [sub])
    assert sub.text in result.missing
    assert not pool.for_sub(sub.text)


def test_structured_data_never_invented_for_unknown_programme():
    sub = SubQuery(text="what is the xyzzy eligibility", source=SourceType.PROGRAMME)
    ctx = ConversationContext()
    db = SessionLocal()
    try:
        pool = asyncio.run(collect_evidence(db, [sub], extract_entities(sub.text), ctx))
        assert not pool.for_sub(sub.text), "unknown programme must yield NO evidence"
    finally:
        db.close()


def test_validation_covered_missing_conflicting():
    covered_q = "what is the mca eligibility"
    missing_q = "what is the exam fee"
    conflict_q = "what is the mca duration"
    pool = EvidencePool()
    pool.add(EvidenceItem(covered_q, SourceType.PROGRAMME, "MCA requires BCA.", direct=True))
    pool.add(EvidenceItem(conflict_q, SourceType.PROGRAMME, "MCA is 2 years.", direct=True))
    pool.add(EvidenceItem(conflict_q, SourceType.RAG, "MCA duration is 3 years.", direct=True))

    result = validate(pool, [
        SubQuery(covered_q, SourceType.PROGRAMME),
        SubQuery(missing_q, SourceType.EXAMINATION),
        SubQuery(conflict_q, SourceType.PROGRAMME),
    ])
    assert covered_q in result.covered
    assert missing_q in result.missing
    assert conflict_q in result.conflicting


# ---------------------------------------------------------------------------
# C.  Synthesis (LLM gated, validated text only)
# ---------------------------------------------------------------------------

def _pool_with_evidence():
    q1 = "what is the mca eligibility"
    q2 = "what is the mca exam fee"
    pool = EvidencePool()
    pool.add(EvidenceItem(q1, SourceType.PROGRAMME,
                          "MCA — Eligibility: a BCA/BSc degree.", direct=True))
    pool.add(EvidenceItem(q2, SourceType.EXAMINATION,
                          "MCA examination fee is Rs 1200 per semester.", direct=True))
    return [SubQuery(q1, SourceType.PROGRAMME), SubQuery(q2, SourceType.EXAMINATION)], pool


def test_synthesis_streams_validated_grounded_answer(monkeypatch):
    subs, pool = _pool_with_evidence()
    gate = _patch_synthesis(
        monkeypatch,
        tokens=("MCA eligibility requires a BCA degree. Exam fee is Rs 1200."),
    )
    out = []
    async def _drain():
        async for text in synthesize_answer("What is the MCA eligibility and exam fee?",
                                             subs, pool, chat_id="c"):
            out.append(text)
    asyncio.run(_drain())
    assert out == ["MCA eligibility requires a BCA degree. Exam fee is Rs 1200."]
    assert gate.acquired == 1 and gate.released == 1


def test_synthesis_no_evidence_returns_exact_fallback_without_llm(monkeypatch):
    q1 = "what is the mca eligibility"
    q2 = "what is the mca exam fee"
    subs = [SubQuery(q1, SourceType.PROGRAMME), SubQuery(q2, SourceType.EXAMINATION)]
    pool = EvidencePool()  # empty

    gate = _FakeGate()
    monkeypatch.setattr("app.multi_source.synthesize.shared_llm_gate", gate)

    async def _boom(question, context, system=None):
        raise AssertionError("LLM must NOT be called when no evidence exists")

    monkeypatch.setattr("app.multi_source.synthesize.stream_answer_async", _boom)
    out = []
    async def _drain():
        async for text in synthesize_answer("What is the MCA eligibility and exam fee?",
                                             subs, pool, chat_id="c"):
            out.append(text)
    asyncio.run(_drain())
    assert out == [MISSING_EVIDENCE_FALLBACK]
    assert gate.acquired == 0, "no LLM gate acquisition when there is no evidence"


def test_synthesis_prompt_parroting_collapses_to_fallback(monkeypatch):
    subs, pool = _pool_with_evidence()
    _patch_synthesis(
        monkeypatch,
        tokens=("Evidence over memory: never use your prior knowledge outside the evidence "),
    )
    out = []
    async def _drain():
        async for text in synthesize_answer("q?", subs, pool, chat_id="c"):
            out.append(text)
    asyncio.run(_drain())
    assert out == [MISSING_EVIDENCE_FALLBACK]


def test_synthesis_gate_busy_returns_fallback(monkeypatch):
    subs, pool = _pool_with_evidence()

    class _BusyGate:
        async def acquire(self, timeout=0.0):
            return False

        def release(self):
            raise AssertionError("release not called when acquire fails")

    monkeypatch.setattr("app.multi_source.synthesize.shared_llm_gate", _BusyGate())

    async def _boom(question, context, system=None):
        raise AssertionError("LLM must not be called when the gate is busy")

    monkeypatch.setattr("app.multi_source.synthesize.stream_answer_async", _boom)
    out = []
    async def _drain():
        async for text in synthesize_answer("q?", subs, pool, chat_id="c"):
            out.append(text)
    asyncio.run(_drain())
    assert out == [MISSING_EVIDENCE_FALLBACK]


# ---------------------------------------------------------------------------
# D.  Prompt builders
# ---------------------------------------------------------------------------

def test_build_question_numbers_sub_questions():
    subs, _ = _pool_with_evidence()
    q = build_question("What is the MCA eligibility and fee?", subs)
    assert "1. what is the mca eligibility" in q
    assert "2. what is the mca exam fee" in q


def test_format_evidence_block_marks_missing_and_conflicting():
    q1 = "what is the mca eligibility"
    q2 = "what is the mca duration"
    pool = EvidencePool()
    pool.add(EvidenceItem(q1, SourceType.PROGRAMME, "MCA requires BCA.", direct=True))
    pool.add(EvidenceItem(q2, SourceType.PROGRAMME, "2 years", direct=True))
    pool.add(EvidenceItem(q2, SourceType.RAG, "3 years", direct=True))
    result = validate(pool, [SubQuery(q1, SourceType.PROGRAMME),
                             SubQuery(q2, SourceType.PROGRAMME)])
    block = format_evidence_block(
        pool,
        [SubQuery(q1, SourceType.PROGRAMME), SubQuery(q2, SourceType.PROGRAMME)],
        result,
    )
    assert "[Question 1:" in block
    assert "MCA requires BCA" in block
    assert "CONFLICTS" in block


# ---------------------------------------------------------------------------
# E.  Engine handler → SSE contract
# ---------------------------------------------------------------------------

def test_handle_multi_source_emits_token_and_done_with_provenance(monkeypatch):
    from app.orchestrator import engine as eng

    q1 = "what is the mca eligibility"
    q2 = "what is the mca exam fee"
    subs = [SubQuery(q1, SourceType.PROGRAMME), SubQuery(q2, SourceType.EXAMINATION)]
    originals = [s.as_dict() for s in subs]

    pool = EvidencePool()
    pool.add(EvidenceItem(q1, SourceType.PROGRAMME,
                          "MCA — Eligibility: a BCA degree.", title="MCA (structured catalogue)", direct=True))
    pool.add(EvidenceItem(q2, SourceType.EXAMINATION,
                          "MCA examination fee: Rs 1200.", title="Examination Fee 2026", direct=True))

    collected = {}

    async def _fake_collect(db, subs_, entities, ctx, rag_ctx=None):
        collected["subs"] = list(subs_)
        return pool

    async def _fake_synth(original, subs_, pool_, chat_id=""):
        yield "Here is the combined answer."

    async def _stub_event(**kw):
        return None

    monkeypatch.setattr("app.multi_source.evidence.collect_evidence", _fake_collect)
    monkeypatch.setattr("app.multi_source.synthesize.synthesize_answer", _fake_synth)
    monkeypatch.setattr(eng, "collect_event", _stub_event)

    plan_result = SimpleNamespace(
        action="multi_source",
        extra={"multi_source": originals, "original_query": "What is the MCA eligibility and exam fee?"},
    )
    ctx = ConversationContext()
    state = SimpleNamespace(last_intent="none")

    events = []

    async def _drain():
        async for ev in eng._handle_multi_source(
            None, "user", "What is the MCA eligibility and exam fee?", "ms-chat",
            state, ctx, extract_entities("mca eligibility and exam fee"), plan_result,
        ):
            events.append(ev)

    asyncio.run(_drain())

    types = [e["type"] for e in events]
    assert set(types) <= {"token", "done"}, f"unexpected SSE types: {types}"
    tokens = [e["text"] for e in events if e["type"] == "token"]
    assert tokens == ["Here is the combined answer."]
    done = [e for e in events if e["type"] == "done"]
    assert len(done) == 1
    assert done[0]["chat_id"] == "ms-chat"
    assert done[0]["cited_chunks"], "provenance citations required"
    debug = done[0]["multi_source_debug"]
    assert debug["validation"]["covered"]
    assert len(debug["sub_queries"]) == 2
    assert state.last_intent == "knowledge"


# ---------------------------------------------------------------------------
# F.  FINAL polish regressions (decomposition scope + no-substitution + scores)
# ---------------------------------------------------------------------------

def test_plural_exams_schedules_to_notices_and_keeps_preamble_scope():
    """Flagship multi-part question: 'MCA 3rd semester' preamble must survive
    decomposition, plural 'exams' must route to the date-sheet source, and the
    model-paper fragment must not be mangled into broken grammar."""
    q = ("I am doing MCA 3rd semester. When are my exams, what is the exam fee, "
         "and do you have model papers?")
    entities = extract_entities(q)
    assert entities.programme == "mca" and entities.semester == 3
    subs = decompose_query(q, entities, None)
    assert subs is not None
    sources = [s.source for s in subs]
    assert SourceType.NOTICES in sources, "plural 'exams' must use the date-sheet source"
    assert any(s.text == "When are my exams" for s in subs)
    assert any(s.text == "do you have model papers?" for s in subs)
    assert not any("what is the mca do" in s.text for s in subs)


def test_model_paper_evidence_scoped_no_substitution(monkeypatch):
    """A constrained model-paper sub-question must pass AND-filters to the
    examination service (never return another programme's papers)."""
    from app.multi_source import evidence as ev

    captured = {}

    def _fake_list(db, **kwargs):
        captured.update(kwargs)
        return []

    monkeypatch.setattr("app.examination.service.list_model_papers", _fake_list)
    sub = SubQuery(text="do you have model papers?", source=SourceType.EXAMINATION)
    entities = extract_entities("I am doing MCA 3rd semester")
    items = ev._evidence_from_examination(sub, None, entities, ConversationContext())
    assert items == [], "no matching papers -> no evidence (honest fallback)"
    assert captured.get("programme") == "mca"
    assert captured.get("semester") == 3


def test_rag_evidence_uses_real_score_fields(monkeypatch):
    """The RAG collector must read the retriever's actual score keys, not the
    never-set '_score', so relevance is not stuck at 0.0."""
    from app.multi_source import evidence as ev

    def _fake_retrieve(query, top_k=4, context=None):
        return [{
            "content": "MCA admission requires a BCA degree.",
            "document_title": "Admission Prospectus",
            "combined_score": 0.5,
        }]

    monkeypatch.setattr("app.ingest.retrieve.retrieve", _fake_retrieve)
    sub = SubQuery(text="what is the admission process for mca", source=SourceType.RAG)
    items = ev._evidence_from_rag(sub, {})
    assert items and items[0].relevance == 0.5


def test_handle_multi_source_missing_subs_falls_back_to_rag(monkeypatch):
    from app.orchestrator import engine as eng

    async def _stub_event(**kw):
        return None

    monkeypatch.setattr(eng, "collect_event", _stub_event)
    plan_result = SimpleNamespace(action="multi_source", extra={})
    state = SimpleNamespace(last_intent="none")
    ctx = ConversationContext()

    events = []

    # No subs → defensive run_chat fallback path must not crash.
    async def _drain():
        async for ev in eng._handle_multi_source(
            None, "user", "hi", "ms-chat-2", state, ctx,
            extract_entities("hi"), plan_result,
        ):
            events.append(ev)

    # run_chat inside will try to open a real session for a fresh conversation;
    # give it a usable sqlite session instead of None.
    db = SessionLocal()
    try:
        from app.orchestrator import engine as eng_inner
        async def _drain2():
            async for ev in eng_inner._handle_multi_source(
                db, "user", "hi", f"ms-chat-{uuid.uuid4()}", state, ctx,
                extract_entities("hi"), plan_result,
            ):
                events.append(ev)
        asyncio.run(_drain2())
    finally:
        db.close()