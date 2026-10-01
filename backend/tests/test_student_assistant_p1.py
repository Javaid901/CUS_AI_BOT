"""
backend/tests/test_student_assistant_p1.py

P1 — Trustworthy intelligent path extensions for the General University
Student-Assistant (implementation order Phases 1→6).

Covers:
  * P1-A current-status evidence isolation: only dated official
    announcement-shaped evidence (verified notice / official_notification
    document / dated verified website page) may establish current status; general
    RAG knowledge and programme-profile facts never do; the deterministic
    short-circuit fires when no authority exists; dated official conflicts are
    surfaced instead of silently chosen.
  * P1-B bounded information plan: conservative guard, JSON-only schema,
    defensive parsing and graceful degradation; ONE extra LLM call max for
    genuinely complex knowledge questions only.
  * Phase 4 procedure source-support behavior (prompt contract).
  * P1-D document/notice comparison: routing (kind=documents), notices-first
    sub-questions, comparison-tailored synthesis prompt, dated-evidence told
    apart from invented supersession.
  * P1-C analytics: intelligent events record task kind / sources / currentness
    / llm-use without touching unrelated events.
  * Protected-route negatives with P1 enabled — nothing protected ever reaches
    the intelligent/information-plan path.

Run:  python -m pytest tests/test_student_assistant_p1.py -q
"""

from __future__ import annotations

import asyncio
import datetime
import uuid
from types import SimpleNamespace

import pytest

from app.database import SessionLocal, create_all
from app.catalogue.seed import seed_catalogue
from app.orchestrator.context import ConversationContext
from app.orchestrator.extractor import extract_entities
from app.orchestrator.planner import plan
from app.multi_source.decompose import SourceType, SubQuery, build_intelligent_subs
from app.multi_source.evidence import EvidenceItem, EvidencePool

create_all()


@pytest.fixture(scope="module", autouse=True)
def _seed_catalogue():
    db = SessionLocal()
    try:
        seed_catalogue(db)
        db.commit()
    finally:
        db.close()


def _planning_action(raw_message: str, *, ctx: ConversationContext | None = None):
    e = extract_entities(raw_message)
    p = plan(raw_message, ctx or ConversationContext(), "sa-p1-test", e)
    return p.action, p.target, p.reason, p.extra


def _status_notice_item(sub: str) -> EvidenceItem:
    return EvidenceItem(
        sub_question=sub, source=SourceType.NOTICES,
        text="Admission 2026 - Notice: applications to MCA are open.",
        title="Admission 2026 Notice", source_id="n-1",
        issued_at="2026-09-12T00:00:00+00:00", verified=True,
        source_label="notice",
    )


def _official_notification_item(sub: str, title: str, date: str) -> EvidenceItem:
    return EvidenceItem(
        sub_question=sub, source=SourceType.DOCUMENTS,
        text=f"{title} (published {date})", title=title, source_id=title,
        issued_at=date, verified=True, source_label="official_notification",
    )


def _evergreen_regulation_item(sub: str) -> EvidenceItem:
    return EvidenceItem(
        sub_question=sub, source=SourceType.DOCUMENTS,
        text="CUS Regulations 2026 - MCA Scheme and Curriculum.", title="CUS Regulations 2026",
        source_id="reg-1", issued_at="2026-09-01T00:00:00+00:00",
        verified=True, source_label="other_official_document",
    )


def _generic_rag_item(sub: str) -> EvidenceItem:
    return EvidenceItem(
        sub_question=sub, source=SourceType.RAG,
        text="MCA admissions happen every year at Cluster University Srinagar.",
        title="2020 Prospectus", source_id="rag-old", relevance=0.9, direct=True,
    )


# ---------------------------------------------------------------------------
# P1-A — current-status evidence isolation (deterministic)
# ---------------------------------------------------------------------------

def test_filter_status_evidence_partitions_authority_and_context():
    from app.multi_source.evidence import filter_status_evidence, is_status_authority

    sub = "is mca admission open?"
    items = [
        _status_notice_item(sub),                                   # notices -> authority
        _official_notification_item(sub, "MCA Admission 2026", "2026-09-12T00:00:00+00:00"),  # dated notification -> authority
        _evergreen_regulation_item(sub),                           # regulations -> NOT authority
        EvidenceItem(                                               # undated verification -> NOT authority
            sub, SourceType.WEBSITE, "Open selection page", title="Admissions page",
            source_id="w-1", issued_at="", verified=True, source_label="official",
        ),
        EvidenceItem(                                               # dated verified page -> authority
            sub, SourceType.WEBSITE, "Applications open at the admission cell.",
            title="Admissions page", source_id="w-2",
            issued_at="2026-09-10T00:00:00+00:00", verified=True, source_label="official",
        ),
        EvidenceItem(                                               # programme profile -> NOT authority
            sub, SourceType.PROGRAMME, "MCA — Eligibility: a BCA degree.",
            title="[MCA] MCA (structured catalogue)", direct=True,
        ),
        _generic_rag_item(sub),                                     # RAG -> NEVER authority
    ]
    assert not is_status_authority(_evergreen_regulation_item(sub))
    assert not is_status_authority(_generic_rag_item(sub))
    assert is_status_authority(_status_notice_item(sub))

    authority, context_only = filter_status_evidence(items, "status")
    assert {i.title for i in authority} == {
        "Admission 2026 Notice", "MCA Admission 2026", "Admissions page",
    }
    assert {i.title for i in context_only} == {
        "CUS Regulations 2026", "Admissions page", "[MCA] MCA (structured catalogue)",
        "2020 Prospectus",
    }

    # Non-status modes never partition (ordinary behavior preserved).
    authority, context_only = filter_status_evidence(items, "knowledge")
    assert len(authority) == len(items) and not context_only


def test_dated_notice_conflicts_only_flags_real_dated_opposition():
    from app.multi_source.evidence import dated_notice_conflicts

    sub = "is the ug 6th semester exam fee waiver still valid?"
    docs_sub = "Official CUS documents relevant to: is the ug 6th semester exam fee waiver still valid?"
    pool = EvidencePool()
    pool.add(_official_notification_item(docs_sub, "Examination Fee 2026 - Notice", "2026-08-01T00:00:00+00:00"))
    pool.add(_official_notification_item(docs_sub, "Examination Fee 2026 - Revised Notice", "2026-09-01T00:00:00+00:00"))
    # A notice frame + its schedule row (undated) must NEVER conflict.
    pool.add(_status_notice_item(sub))
    conflicts = dated_notice_conflicts(pool)
    assert docs_sub in conflicts
    assert sub not in conflicts


# ---------------------------------------------------------------------------
# P1-A — engine level: status answers follow the isolation
# ---------------------------------------------------------------------------

def _drain_engine(eng, db, message, chat_id, entities, plan_result, *, ctx=None):
    events = []
    state = SimpleNamespace(last_intent="none")
    async def _run():
        async for ev in eng._handle_intelligent(
            db, "u", message, chat_id, state, ctx or ConversationContext(), entities, plan_result,
        ):
            events.append(ev)
        await asyncio.sleep(0)  # let fire-and-forget analytics tasks run
        await asyncio.sleep(0)
    asyncio.run(_run())
    return events, state


def test_status_with_only_evergreen_documents_and_rag_short_circuits(monkeypatch):
    """P1-A flagship: an 'old prospectus + regulations' evidence set must NOT
    produce 'admission is open'. Deterministic honest fallback instead."""
    from app.ingest.prompts import CURRENT_STATUS_UNAVAILABLE
    from app.orchestrator import engine as eng

    q = "is mca admission open?"
    entities = SimpleNamespace(programme="mca", topic="admission")
    ctx = ConversationContext()
    subs = build_intelligent_subs(q, entities, ctx, kind="status")
    rag_sub = next(s.text for s in subs if s.source == SourceType.RAG)
    docs_sub = next(s.text for s in subs if s.source == SourceType.DOCUMENTS)

    pool = EvidencePool()
    pool.add(_evergreen_regulation_item(docs_sub))
    pool.add(_generic_rag_item(rag_sub))

    llm_called = {}

    async def _fake_collect(db_, subs_, e, c, rag_ctx=None):
        return pool

    async def _boom(original, subs_, pool_, chat_id="", system=None, context=None):
        llm_called["called"] = True
        yield "MUST NEVER BE REACHED"

    async def _stub_event(**kw):
        recorded["event"] = kw

    async def _stub_perf(**kw):
        recorded["perf"] = kw

    recorded: dict[str, dict] = {}
    monkeypatch.setattr("app.multi_source.evidence.collect_evidence", _fake_collect)
    monkeypatch.setattr("app.multi_source.synthesize.synthesize_answer", _boom)
    monkeypatch.setattr(eng, "collect_event", _stub_event)
    monkeypatch.setattr(eng, "collect_performance", _stub_perf)

    plan_result = SimpleNamespace(
        action="intelligent",
        extra={"original_query": q, "intelligent_kind": "status"},
    )
    events, _ = _drain_engine(eng, None, q, "p1-status-regs", entities, plan_result, ctx=ctx)
    tokens = [e.get("text", "") for e in events if e.get("type") == "token"]
    assert tokens == [CURRENT_STATUS_UNAVAILABLE]
    assert not llm_called
    done = [e for e in events if e.get("type") == "done"][0]
    assert done["intelligent_debug"]["short_circuit"] == "no_current_official_evidence"
    assert done["intelligent_debug"]["authority_sources"] == []
    assert recorded["event"]["service_requested"] == "status"
    assert recorded["event"]["llm_used"] is False


def test_status_with_dated_announcement_synthesizes_marked_evidence(monkeypatch):
    from app.ingest.prompts import STUDENT_ASSISTANT_SYSTEM_PROMPT
    from app.orchestrator import engine as eng

    q = "is mca admission open?"
    entities = SimpleNamespace(programme="mca", topic="admission")
    ctx = ConversationContext()
    subs = build_intelligent_subs(q, entities, ctx, kind="status")
    rag_sub = next(s.text for s in subs if s.source == SourceType.RAG)

    notice = _status_notice_item(next(s.text for s in subs if s.source == SourceType.NOTICES))
    pool = EvidencePool()
    pool.add(notice)
    pool.add(_generic_rag_item(rag_sub))

    captured = {}

    async def _fake_collect(db_, subs_, e, c, rag_ctx=None):
        return pool

    async def _fake_synth(original, subs_, pool_, chat_id="", system=None, context=None):
        captured["system"] = system
        captured["context"] = context
        yield "As per the CUS notice dated 12 September 2026, MCA applications are open."

    async def _stub_event(**kw):
        return None

    monkeypatch.setattr("app.multi_source.evidence.collect_evidence", _fake_collect)
    monkeypatch.setattr("app.multi_source.synthesize.synthesize_answer", _fake_synth)
    monkeypatch.setattr(eng, "collect_event", _stub_event)
    monkeypatch.setattr(eng, "collect_performance", _stub_event)

    plan_result = SimpleNamespace(
        action="intelligent",
        extra={"original_query": q, "intelligent_kind": "status"},
    )
    events, _ = _drain_engine(eng, None, q, "p1-status-ok", entities, plan_result, ctx=ctx)
    assert captured["system"] == STUDENT_ASSISTANT_SYSTEM_PROMPT
    assert captured["context"]["mark_status"] is True
    assert id(notice) in captured["context"]["authority_ids"]
    tokens = [e.get("text", "") for e in events if e.get("type") == "token"]
    assert "applications are open" in tokens[0]
    done = [e for e in events if e.get("type") == "done"][0]
    assert done["intelligent_debug"]["kind"] == "status"


# ---------------------------------------------------------------------------
# P1-B — information plan: guard, schema, parsing, degradation
# ---------------------------------------------------------------------------

def test_should_information_plan_is_conservative():
    from app.orchestrator.info_plan import should_information_plan

    assert should_information_plan(
        "MCA eligibility, documents, fee, admission process and last date?", "knowledge"
    )
    assert should_information_plan(
        "what is the MCA eligibility, admission process and fee?", "knowledge"
    )
    # Simple single-aspect questions never plan (P0 call count preserved).
    assert not should_information_plan("explain the mca admission procedure", "knowledge")
    assert not should_information_plan("how do i apply for mca", "knowledge")
    assert not should_information_plan("what documents do i need for mca?", "knowledge")
    # Status / protected routes never plan.
    assert not should_information_plan("are admissions open for mca?", "status")
    assert not should_information_plan("show my result", "knowledge")


def test_parse_info_plan_valid_and_normalized():
    from app.orchestrator.info_plan import InfoPlan, parse_info_plan

    raw = ('Sure! Here is the plan {"mode": "procedure", "needs_current": false, '
           '"required_facts": ["documents", "fee", "deadline"], '
           '"source_preferences": ["documents", "notices"]}')
    plan_ = parse_info_plan(raw)
    assert plan_ == InfoPlan(
        mode="procedure", needs_current=False,
        required_facts=("documents", "fee", "deadline"),
        source_preferences=("documents", "notices"),
    )
    # Duplicate/messy values are normalized within bounds.
    plan_ = parse_info_plan(
        '{"mode": "fact", "required_facts": ["FEE", "documents", "fee"], '
        '"source_preferences": ["rag", "rag", "programme"]}'
    )
    assert plan_.required_facts == ("fee", "documents")
    assert plan_.source_preferences == ("rag", "programme")
    # "document" is an allowed alias for comparison mode.
    assert parse_info_plan(
        '{"mode": "document", "required_facts": ["eligibility"]}'
    ).mode == "comparison"


@pytest.mark.parametrize("bad", [
    "", "not json at all", "null", "[]", '{"mode": "unknown"}',
    '{"mode": "fact", "required_facts": ["eligibility", "a", "b", "c", "d", "e", "f"]}',
    '{"mode": "fact", "required_facts": ["eligibility", "xyzzy"]}',
    '{"mode": "fact", "source_preferences": ["website", "bogus"]}',
    '{"mode": "fact", "source_preferences": ["website", "x", "y", "z", "q", "w"]}',
    '{"required_facts": ["fee"]}',  # no valid mode
])
def test_parse_info_plan_invalid_inputs_return_none(bad):
    from app.orchestrator.info_plan import parse_info_plan
    assert parse_info_plan(bad) is None


def test_plan_information_uses_existing_gate_and_generator(monkeypatch):
    from app.orchestrator.info_plan import (
        _PLAN_TIMEOUT,
        plan_information,
    )

    class _FakeGate:
        def __init__(self):
            self.acquired = 0
            self.released = 0

        async def acquire(self, timeout=0.0):
            self.acquired += 1
            return True

        def release(self):
            self.released += 1

    gate = _FakeGate()
    monkeypatch.setattr("app.orchestrator.info_plan.shared_llm_gate", gate)

    async def _stream(question, context, system=None):
        yield '{"mode": "procedure", "needs_current": false, '
        yield '"required_facts": ["documents", "fee"], '
        yield '"source_preferences": ["documents", "notices"]}'

    monkeypatch.setattr("app.orchestrator.info_plan.stream_answer_async", _stream)
    plan_ = asyncio.run(plan_information(
        "what documents, fee and last date for mca admission?", programme="mca"
    ))
    assert plan_ is not None
    assert plan_.mode == "procedure"
    assert plan_.required_facts == ("documents", "fee")
    assert gate.acquired == 1 and gate.released == 1
    assert _PLAN_TIMEOUT > 0


@pytest.mark.parametrize("failure_kind", ["gate_busy", "generation", "malformed", "timeout"])
def test_plan_information_degrades_gracefully(monkeypatch, failure_kind):
    from app.orchestrator.info_plan import plan_information

    class _BusyGate:
        async def acquire(self, timeout=0.0):
            return False

        def release(self):
            raise AssertionError("release must not be needed when acquire is False")

    releases = {"count": 0}

    class _GoodGate:
        async def acquire(self, timeout=0.0):
            return True

        def release(self):
            releases["count"] += 1

    if failure_kind == "gate_busy":
        monkeypatch.setattr("app.orchestrator.info_plan.shared_llm_gate", _BusyGate())
        async def _boom(question, context, system=None):
            raise AssertionError("no LLM call when the gate is busy")
        monkeypatch.setattr("app.orchestrator.info_plan.stream_answer_async", _boom)
    else:
        from app.ingest.generator import GenerationError
        monkeypatch.setattr("app.orchestrator.info_plan.shared_llm_gate", _GoodGate())

        async def _gen(question, context, system=None):
            if failure_kind == "generation":
                raise GenerationError("ollama down")
            if failure_kind == "malformed":
                yield "i have no idea what you mean."
                return
            await asyncio.sleep(30)  # timeout case
            yield "never"
        monkeypatch.setattr("app.orchestrator.info_plan.stream_answer_async", _gen)
        if failure_kind == "timeout":
            monkeypatch.setattr("app.orchestrator.info_plan._PLAN_TIMEOUT", 0.05)

    result = asyncio.run(plan_information("any question?", programme="mca"))
    assert result is None, failure_kind


# ---------------------------------------------------------------------------
# P1-B — engine integration (plan expands evidence; failure falls back)
# ---------------------------------------------------------------------------

def test_handle_intelligent_plan_expands_subs_and_passes_facts(monkeypatch):
    from app.ingest.prompts import STUDENT_ASSISTANT_SYSTEM_PROMPT
    from app.orchestrator import engine as eng
    from app.orchestrator.info_plan import InfoPlan

    q = "MCA eligibility, documents, fee, admission process and last date?"
    entities = SimpleNamespace(programme="mca", topic="admission")
    ctx = ConversationContext()
    base_subs = build_intelligent_subs(q, entities, ctx, kind="knowledge")

    async def _fake_plan(question, programme=None):
        return InfoPlan(
            mode="fact", needs_current=False,
            required_facts=("eligibility", "fee"),
            source_preferences=("programme", "notices", "documents", "website", "rag"),
        )

    captured = {}
    rag_sub = next(s.text for s in base_subs if s.source == SourceType.RAG)
    pool = EvidencePool()
    pool.add(EvidenceItem(
        rag_sub, SourceType.RAG, "MCA admission process starts with the entrance test.",
        title="Admission Prospectus", direct=True,
    ))

    async def _fake_collect(db_, subs_, e, c, rag_ctx=None):
        captured["subs"] = list(subs_)
        return pool

    async def _fake_synth(original, subs_, pool_, chat_id="", system=None, context=None):
        captured["system"] = system
        captured["context"] = context
        captured["subs_in_synth"] = list(subs_)
        yield "Eligibility: a BCA degree. Fee and last date are below."

    async def _stub_event(**kw):
        return None

    monkeypatch.setattr("app.orchestrator.info_plan.plan_information", _fake_plan)
    monkeypatch.setattr("app.multi_source.evidence.collect_evidence", _fake_collect)
    monkeypatch.setattr("app.multi_source.synthesize.synthesize_answer", _fake_synth)
    monkeypatch.setattr(eng, "collect_event", _stub_event)
    monkeypatch.setattr(eng, "collect_performance", _stub_event)

    plan_result = SimpleNamespace(
        action="intelligent",
        extra={"original_query": q, "intelligent_kind": "knowledge"},
    )
    events, _ = _drain_engine(eng, None, q, "p1-plan", entities, plan_result, ctx=ctx)

    # Subs were expanded with per-fact RAG fragments (bounded at 4).
    expanded = captured["subs"]
    assert len(expanded) == len(base_subs) + 2
    assert any("eligibility" in s.text for s in expanded)
    assert any("fee" in s.text for s in expanded)
    assert captured["system"] == STUDENT_ASSISTANT_SYSTEM_PROMPT
    assert captured["context"]["expected_facts"] == ["eligibility", "fee"]
    done = [e for e in events if e.get("type") == "done"][0]
    assert done["intelligent_debug"]["plan_used"] is True
    assert done["intelligent_debug"]["information_plan"]["required_facts"] == ["eligibility", "fee"]
    assert done["intelligent_debug"]["plan_latency_ms"] >= 0


def test_handle_intelligent_plan_failure_reverts_to_p0(monkeypatch):
    from app.ingest.prompts import STUDENT_ASSISTANT_SYSTEM_PROMPT
    from app.orchestrator import engine as eng

    q = "MCA eligibility, documents, fee, admission process and last date?"
    entities = SimpleNamespace(programme="mca", topic="admission")
    ctx = ConversationContext()
    base_subs = build_intelligent_subs(q, entities, ctx, kind="knowledge")

    async def _no_plan(question, programme=None):
        return None

    captured = {}

    async def _fake_collect(db_, subs_, e, c, rag_ctx=None):
        captured["subs"] = list(subs_)
        return EvidencePool()

    async def _fake_synth(original, subs_, pool_, chat_id="", system=None, context=None):
        captured["system"] = system
        captured["context"] = context
        yield "I don't have information available."

    async def _stub_event(**kw):
        return None

    monkeypatch.setattr("app.orchestrator.info_plan.plan_information", _no_plan)
    monkeypatch.setattr("app.multi_source.evidence.collect_evidence", _fake_collect)
    monkeypatch.setattr("app.multi_source.synthesize.synthesize_answer", _fake_synth)
    monkeypatch.setattr(eng, "collect_event", _stub_event)
    monkeypatch.setattr(eng, "collect_performance", _stub_event)

    plan_result = SimpleNamespace(
        action="intelligent",
        extra={"original_query": q, "intelligent_kind": "knowledge"},
    )
    events, _ = _drain_engine(eng, None, q, "p1-plan-fail", entities, plan_result, ctx=ctx)

    assert len(captured["subs"]) == len(base_subs), "no plan -> no extra RAG fragments"
    assert captured["system"] == STUDENT_ASSISTANT_SYSTEM_PROMPT
    assert captured["context"] is None, "no plan -> no synthesis context wrapping"
    done = [e for e in events if e.get("type") == "done"][0]
    assert done["intelligent_debug"]["plan_used"] is False
    assert done["intelligent_debug"]["information_plan"] is None


# ---------------------------------------------------------------------------
# Phase 4 — procedure source-support prompt contract
# ---------------------------------------------------------------------------

def test_student_assistant_prompt_procedure_source_support():
    from app.ingest.prompts import STUDENT_ASSISTANT_SYSTEM_PROMPT

    prompt = STUDENT_ASSISTANT_SYSTEM_PROMPT
    assert "PROCEDURE questions" in prompt
    assert "Derive every step from the verified" in prompt
    assert "does not specify that step" in prompt
    assert "never present a fixed step list" in prompt


def test_handle_intelligent_procedure_plan_passes_facts(monkeypatch):
    """A procedure plan must feed its required facts into synthesis so missing
    steps are honestly acknowledged (no hard-coded process anywhere)."""
    from app.orchestrator import engine as eng
    from app.orchestrator.info_plan import InfoPlan

    q = "How do I apply for MCA revaluation: documents, fee and last date?"
    entities = SimpleNamespace(programme="mca", topic="revaluation")
    ctx = ConversationContext()

    async def _procedure_plan(question, programme=None):
        return InfoPlan(
            mode="procedure", needs_current=False,
            required_facts=("documents", "fee", "deadline"),
            source_preferences=("documents", "notices", "rag"),
        )

    captured = {}

    async def _fake_collect(db_, subs_, e, c, rag_ctx=None):
        return EvidencePool()

    async def _fake_synth(original, subs_, pool_, chat_id="", system=None, context=None):
        captured["context"] = context
        yield "The documents, fee and last date for revaluation are listed below."

    async def _stub_event(**kw):
        return None

    monkeypatch.setattr("app.orchestrator.info_plan.plan_information", _procedure_plan)
    monkeypatch.setattr("app.multi_source.evidence.collect_evidence", _fake_collect)
    monkeypatch.setattr("app.multi_source.synthesize.synthesize_answer", _fake_synth)
    monkeypatch.setattr(eng, "collect_event", _stub_event)
    monkeypatch.setattr(eng, "collect_performance", _stub_event)

    plan_result = SimpleNamespace(
        action="intelligent",
        extra={"original_query": q, "intelligent_kind": "knowledge"},
    )
    events, _ = _drain_engine(eng, None, q, "p1-procedure", entities, plan_result, ctx=ctx)
    assert captured["context"]["expected_facts"] == ["documents", "fee", "deadline"]


# ---------------------------------------------------------------------------
# P1-D — document / notice comparison
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "which notice is newer?",
    "is this notice still valid?",
    "was this notice revised?",
    "which notice is latest about the mca exam?",
    "are these two notices still in force?",
])
def test_document_comparison_routes_to_intelligent_documents(raw):
    action, _, _, extra = _planning_action(raw)
    assert action == "intelligent", f"{raw!r} -> {action}"
    assert (extra or {}).get("intelligent_kind") == "documents", f"{raw!r}"


def test_document_gate_never_hijacks_protected_or_generic():
    for raw, expected in (
        # Comparison wording already deterministically served by the canonical
        # documents repository (Rule 3ab) must NOT be hijacked by the gate.
        ("compare these two notifications", "official_documents"),
        ("which notification should I follow?", "official_documents"),
        ("compare the August and September fee notifications", "grievance"),
        ("compare mca and mba", "comparison"),
        ("compare the mca and bca fee structure", "comparison"),
        ("show the mca date sheet", "university_notices"),
        ("what is a notice", "news"),
        ("show me the notices", "news"),
        ("show my result", "student_service"),
        ("fill my exam form", "student_service"),
        ("has the mca exam fee been announced?", "structured"),
    ):
        action, _, _, _ = _planning_action(raw)
        assert action == expected, f"{raw!r} -> {action} (expected {expected})"


def test_build_intelligent_subs_documents_kind_notices_first():
    ctx = ConversationContext()
    entities = SimpleNamespace(programme="mca")
    subs = build_intelligent_subs("which notice is newer?", entities, ctx, kind="documents")
    assert subs[0].source == SourceType.NOTICES
    docs = next(s for s in subs if s.source == SourceType.DOCUMENTS)
    assert "published dates" in docs.text
    texts = [s.text for s in subs]
    assert len(texts) == len(set(texts)), "distinct sub-texts required"


def test_handle_intelligent_documents_uses_comparison_prompt_and_conflict_notes(monkeypatch):
    from app.ingest.prompts import DOCUMENT_COMPARISON_RULES, STUDENT_ASSISTANT_SYSTEM_PROMPT
    from app.orchestrator import engine as eng

    q = "which notice is newer?"
    entities = SimpleNamespace(programme=None, topic="notice")
    ctx = ConversationContext()
    subs = build_intelligent_subs(q, entities, ctx, kind="documents")
    docs_sub = next(s.text for s in subs if s.source == SourceType.DOCUMENTS)

    pool = EvidencePool()
    pool.add(_official_notification_item(docs_sub, "Examination Fee 2026", "2026-08-01T00:00:00+00:00"))
    pool.add(_official_notification_item(docs_sub, "Examination Fee 2026 - Revised", "2026-09-01T00:00:00+00:00"))

    captured = {}

    async def _fake_collect(db_, subs_, e, c, rag_ctx=None):
        return pool

    async def _fake_synth(original, subs_, pool_, chat_id="", system=None, context=None):
        captured["system"] = system
        captured["context"] = context
        yield "The newer notification is dated 1 September 2026."

    async def _stub_event(**kw):
        return None

    monkeypatch.setattr("app.multi_source.evidence.collect_evidence", _fake_collect)
    monkeypatch.setattr("app.multi_source.synthesize.synthesize_answer", _fake_synth)
    monkeypatch.setattr(eng, "collect_event", _stub_event)
    monkeypatch.setattr(eng, "collect_performance", _stub_event)

    plan_result = SimpleNamespace(
        action="intelligent",
        extra={"original_query": q, "intelligent_kind": "documents"},
    )
    events, _ = _drain_engine(eng, None, q, "p1-doc-compare", entities, plan_result, ctx=ctx)
    assert captured["system"] == STUDENT_ASSISTANT_SYSTEM_PROMPT + DOCUMENT_COMPARISON_RULES
    assert docs_sub in captured["context"]["conflict_notes"]
    tokens = [e.get("text", "") for e in events if e.get("type") == "token"]
    assert "newer notification" in tokens[0]
    done = [e for e in events if e.get("type") == "done"][0]
    assert done["intelligent_debug"]["kind"] == "documents"
    assert done["intelligent_debug"]["plan_used"] is False


# ---------------------------------------------------------------------------
# P1-C — analytics observability
# ---------------------------------------------------------------------------

def test_intelligent_event_records_p1_fields(monkeypatch):
    from app.orchestrator import engine as eng

    q = "explain the mca admission procedure"
    entities = SimpleNamespace(programme="mca", topic="admission")
    ctx = ConversationContext()
    subs = build_intelligent_subs(q, entities, ctx, kind="knowledge")
    rag_sub = next(s.text for s in subs if s.source == SourceType.RAG)
    pool = EvidencePool()
    pool.add(EvidenceItem(
        rag_sub, SourceType.RAG, "MCA admission begins with the entrance test.",
        title="Admission Prospectus", direct=True,
    ))
    pool.add(EvidenceItem(
        next(s.text for s in subs if s.source == SourceType.PROGRAMME),
        SourceType.PROGRAMME, "MCA — Eligibility: a BCA degree.",
        title="[MCA] MCA (structured catalogue)", direct=True,
    ))

    recorded_events = []
    recorded_perf = []

    async def _fake_collect(db_, subs_, e, c, rag_ctx=None):
        return pool

    async def _fake_synth(original, subs_, pool_, chat_id="", system=None, context=None):
        yield "MCA admission begins with the entrance test."

    async def _stub_event(**kw):
        recorded_events.append(kw)

    async def _stub_perf(**kw):
        recorded_perf.append(kw)

    monkeypatch.setattr("app.multi_source.evidence.collect_evidence", _fake_collect)
    monkeypatch.setattr("app.multi_source.synthesize.synthesize_answer", _fake_synth)
    monkeypatch.setattr(eng, "collect_event", _stub_event)
    monkeypatch.setattr(eng, "collect_performance", _stub_perf)

    plan_result = SimpleNamespace(
        action="intelligent",
        extra={"original_query": q, "intelligent_kind": "knowledge"},
    )
    _drain_engine(eng, None, q, "p1-analytics", entities, plan_result, ctx=ctx)

    event = recorded_events[0]
    assert event["planner_action"] == "intelligent"
    assert event["route_chosen"] == "intelligent"
    assert event["service_requested"] == "knowledge"
    assert event["detected_service"] == "knowledge"
    assert event["llm_used"] is True
    assert event["rag_used"] is True
    assert event["structured_lookup_used"] is True
    assert event["conversation_completed"] is True