"""
backend/tests/test_student_assistant.py

P0 — General University Student-Assistant.

Covers the approved P0 architecture end to end:
  * planner intelligent gate — fires for genuinely university-related
    current-status and complex knowledge/procedure questions, and NEVER for
    protected single-service routes (results / admit card / exam form /
    grievance / model papers / date sheets / authority / catalogue / fees);
  * the deterministic current-status detector (generic — no per-programme or
    per-process workflows);
  * new evidence collectors (verified + published official UniversityDocument
    records and verified WebsitePage snippets) with provenance metadata;
  * notice-frame evidence so status questions surface the newest notice
    title/date;
  * build_intelligent_subs / the engine handler (status short-circuit + mode
    prompt wiring);
  * prompt honion: exact fallback text, current-status honesty, and no internal
    retrieval terminology in student-facing answers.

Non-regressions enforced:
  * "what is the capital of France?", "xyzzy qwerty" and the bare
    "schedule" probe keep their existing route (never intelligent);
  * fee / exam-fee shorthand keeps the planner's examination-fee
    disambiguation;
  * existing multi-source evidence behaviour is untouched (schedule rows stay
    first, etc.).

Run:  python -m pytest tests/test_student_assistant.py -q
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
from app.multi_source.evidence import EvidenceItem, EvidencePool, collect_evidence, validate

create_all()


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


def _planning_action(raw_message: str, *, ctx: ConversationContext | None = None):
    e = extract_entities(raw_message)
    p = plan(raw_message, ctx or ConversationContext(), "sa-test", e)
    return p.action, p.target, p.reason, p.extra


# ---------------------------------------------------------------------------
# A.  Planner protected-route negatives (P0 §"Do NOT swallow protected routes")
# ---------------------------------------------------------------------------

def test_respects_protected_student_services():
    for raw in ("show my result", "check my result", "download my admit card",
                "fill my exam form", "download admit card"):
        action, _, _, _ = _planning_action(raw)
        assert action not in ("intelligent", "rag", "slot_fill", "catalogue"), f"{raw!r} -> {action}"
        assert action in ("student_service", "unavailable_service"), f"{raw!r} -> {action}"


def test_respects_grievance_authority_model_papers_and_datesheets():
    dedicated = {
        "i want to submit a grievance": ("grievance",),
        "show mca model papers": None,
        "show the mca date sheet": None,
        "when is the mca 3rd semester exam": None,
    }
    for raw, exact in dedicated.items():
        action, _, _, _ = _planning_action(raw)
        if exact:
            assert action in exact, f"{raw!r} -> {action} (expected {exact})"
        else:
            assert action != "intelligent", f"{raw!r} must not be hijacked -> {action}"
            assert action not in ("rag", "slot_fill"), f"{raw!r} -> {action} (should stay a dedicated route)"
    # Authority lookups: "who is registrar" / "who handles exams" route to the
    # authority directory when seeded; the idiomatic "who is the registrar?"
    # falls through to RAG today. Neither may be taken over by the general
    # assistant.
    for raw in ("who is registrar", "who handles exams", "who is the registrar?"):
        action, _, _, _ = _planning_action(raw)
        assert action != "intelligent", f"{raw!r} must not be hijacked -> {action}"


def test_respects_catalogue_structure_and_fee_routes():
    for raw, expected_action in (
        ("fee structure of bca", "catalogue"),
        ("eligibility of mca", "catalogue"),
        ("bcaa fee", "catalogue"),
        ("how many semesters are in mca?", "structured"),
    ):
        action, _, _, _ = _planning_action(raw)
        assert action == expected_action, f"{raw!r} -> {action} (expected {expected_action})"
    action, target, _, _ = _planning_action("fee")
    assert (action, target) == ("slot_fill", "programme"), f"bare 'fee' -> {action}/{target}"


def test_gate_never_hijacks_general_knowledge_or_ambiguous():
    # Non-university questions keep the plain RAG route even with programme ctx.
    ctx = ConversationContext()
    ctx.programme = "mca"
    action, target, _, _ = _planning_action("what is the capital of France?", ctx=ctx)
    assert action == "rag", f"capital-of-france -> {action}"
    assert "MCA" not in target

    action, _, _, _ = _planning_action("xyzzy qwerty")
    assert action in ("rag", "news", "clarify"), f"xyzzy qwerty -> {action}"

    action, _, _, _ = _planning_action("what is the schedule for sem 4 of mca")
    assert action in ("rag", "news", "clarify", "slot_fill", "structured", "catalogue"), \
        f"bare schedule -> {action} (must not become intelligent)"


# ---------------------------------------------------------------------------
# B.  Planner status gate (generic current-status detection)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "when will bca results be announced?",
    "are admissions open for mca?",
    "is the admission form out?",
])
def test_status_questions_route_to_intelligent_status(raw):
    action, _, _, extra = _planning_action(raw)
    assert action == "intelligent", f"{raw!r} -> {action}"
    assert (extra or {}).get("intelligent_kind") == "status"


def test_exam_fee_announcement_keeps_dedicated_examination_route():
    # "has the exam fee been announced?" is an EXAMINATION-service concern
    # (handled by an earlier protected rule); the general assistant must not
    # swallow it.
    action, target, _, _ = _planning_action("has the mca exam fee been announced?")
    assert action != "intelligent", f"exam-fee announcement hijacked -> {action}"
    assert "examination_fee" in target or "fee" in target, target


# ---------------------------------------------------------------------------
# C.  Planner knowledge gate (complex procedures / multi-aspect)
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("raw", [
    "explain the mca admission procedure",
    "how do i apply for mca admission",
    "what is the process to get admission in mca",
])
def test_complex_knowledge_routes_to_intelligent_knowledge(raw):
    action, _, _, extra = _planning_action(raw)
    assert action == "intelligent", f"{raw!r} -> {action}"
    assert (extra or {}).get("intelligent_kind") == "knowledge"


def test_simple_attribute_questions_stay_on_their_existing_route():
    # "documents required for admission" already resolves to a slot-fill; the
    # knowledge gate must not capture it.
    action, target, _, _ = _planning_action("documents required for admission")
    assert (action, target) == ("slot_fill", "programme"), f"{action}/{target}"


# ---------------------------------------------------------------------------
# D.  Deterministic status detector unit checks (generic, no per-process rules)
# ---------------------------------------------------------------------------

def test_status_detector_unit_checks():
    from app.orchestrator.current_status import classify_current_status, gate_intelligent

    assert classify_current_status("is admission open?")
    assert classify_current_status("are admissions closed?")
    assert classify_current_status("has the date sheet been released?")
    assert classify_current_status("when is the last date for the exam form?")
    assert not classify_current_status("what is the mca duration")
    assert not classify_current_status("how much is the mca fee")

    # Non-university content never gates.
    assert gate_intelligent("what is the capital of France?") is None
    assert gate_intelligent("how is the weather in srinagar?") is None
    # Fee shorthand without a status marker keeps the planner's fee route.
    assert gate_intelligent("what is the mca examination fee",
                            SimpleNamespace(word_count=6, programme="mca")) is None
    # Short, ambiguous, no programme stays in the clarify pipeline.
    assert gate_intelligent("how to apply", SimpleNamespace(word_count=3, programme=None)) is None


# ---------------------------------------------------------------------------
# E.  Official UniversityDocument evidence (verified + published only)
# ---------------------------------------------------------------------------

def _seed_official_documents(db):
    from app.models.university_document import UniversityDocument

    rows = [
        UniversityDocument(
            id=uuid.uuid4(), title="MCA Admission 2026 - Official Notification",
            doc_type="official_notification", source="crawler",
            programme_id="mca", status="published",
            is_verified=True, is_published=True,
            published_at=datetime.datetime(2026, 5, 10, tzinfo=datetime.timezone.utc),
            source_url="https://cusrinagar.edu.in/admissions/mca-2026",
        ),
        UniversityDocument(
            id=uuid.uuid4(), title="BBA Admissions 2026 - Official Notification",
            doc_type="official_notification", source="manual_upload",
            programme_id="bba", status="published",
            is_verified=True, is_published=True,
            published_at=datetime.datetime(2026, 5, 8, tzinfo=datetime.timezone.utc),
            source_url="https://cusrinagar.edu.in/admissions/bba-2026",
        ),
        UniversityDocument(
            id=uuid.uuid4(), title="MCA Admission 2026 (DRAFT - not published)",
            doc_type="official_notification", source="manual_upload",
            programme_id="mca", status="draft",
            is_verified=False, is_published=False,
            source_url="https://cusrinagar.edu.in/internal/draft",
        ),
        # Non-official doc_type never surfaces in the public collection.
        UniversityDocument(
            id=uuid.uuid4(), title="MCA Model Paper 2026",
            doc_type="model_paper", source="crawler",
            programme_id="mca", status="published",
            is_verified=True, is_published=True,
            source_url="https://cusrinagar.edu.in/exams/mca-model",
        ),
    ]
    for r in rows:
        db.add(r)
    db.commit()
    for r in rows:
        db.refresh(r)
    return rows


def test_document_evidence_collects_only_verified_published_official():
    from app.models.university_document import UniversityDocument

    db = SessionLocal()
    try:
        rows = _seed_official_documents(db)
        sub = SubQuery(
            text="Official CUS documents relevant to: explain the mca admission procedure",
            source=SourceType.DOCUMENTS,
        )
        ctx = ConversationContext()
        pool = asyncio.run(collect_evidence(db, [sub], SimpleNamespace(
            programme="mca", topic="admission",
        ), ctx))
        items = pool.for_sub(sub.text)
        assert items, "official document evidence must be non-empty"
        published = [i for i in items if "MCA Admission 2026 - Official Notification" in i.text]
        assert len(published) == 1
        item = published[0]
        assert item.source == SourceType.DOCUMENTS
        assert item.url == "https://cusrinagar.edu.in/admissions/mca-2026"
        assert item.doc_id, "canonical document id must be carried"
        assert item.verified is True
        assert item.issued_at.startswith("2026-05-10")
        assert item.programme == "mca"
        # The unverified draft and the BBA-only doc must never appear.
        assert not any("DRAFT" in i.text for i in items)
        assert not any("BBA" in i.text for i in items)
    finally:
        _cleanup(db, *[(UniversityDocument, r.id) for r in rows])
        db.close()


def test_document_evidence_no_db_is_empty():
    sub = SubQuery(text="Official CUS documents relevant to: x", source=SourceType.DOCUMENTS)
    pool = asyncio.run(collect_evidence(None, [sub], SimpleNamespace(programme=None), ConversationContext()))
    assert not pool.for_sub(sub.text)


# ---------------------------------------------------------------------------
# F.  Verified WebsitePage evidence (verified + healthy crawl only)
# ---------------------------------------------------------------------------

def _seed_website_pages(db):
    from app.models.website_sync import WebsitePage

    pages = [
        WebsitePage(
            id=str(uuid.uuid4()), url="https://cusrinagar.edu.in/admissions/procedure",
            title="Admission Procedure | MCA", category="official",
            content="Admission to MCA requires passing the entrance test. The procedure is published by the admission cell.",
            status="new", classification_status="verified", http_status=200,
            last_synced=datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc),
        ),
        WebsitePage(
            id=str(uuid.uuid4()), url="https://cusrinagar.edu.in/admissions/procedure-unverified",
            title="Admission Procedure | MCA (unverified)", category="official",
            content="Admission to MCA requires passing the entrance test.",
            status="new", classification_status="draft", http_status=200,
            last_synced=datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc),
        ),
        WebsitePage(
            id=str(uuid.uuid4()), url="https://cusrinagar.edu.in/holidays/2026",
            title="Holiday List", category="knowledge",
            content="A list of university holidays.",
            status="new", classification_status="verified", http_status=200,
            last_synced=datetime.datetime(2026, 6, 1, tzinfo=datetime.timezone.utc),
        ),
    ]
    for p in pages:
        db.add(p)
    db.commit()
    for p in pages:
        db.refresh(p)
    return pages


def test_website_evidence_collects_only_verified_pages():
    from app.models.website_sync import WebsitePage

    db = SessionLocal()
    try:
        pages = _seed_website_pages(db)
        sub = SubQuery(
            text="Pages on the official CUS website relevant to: explain the mca admission procedure",
            source=SourceType.WEBSITE,
        )
        ctx = ConversationContext()
        pool = asyncio.run(collect_evidence(db, [sub], SimpleNamespace(
            programme="mca", topic="admission",
        ), ctx))
        items = pool.for_sub(sub.text)
        assert items, "website evidence must be non-empty"
        assert any("entrance test" in i.text for i in items)
        # Unverified pages never appear; the URL is the verified https one.
        assert all(i.url.startswith("https://") for i in items)
        assert all(i.verified for i in items)
        assert not any("unverified" in i.title for i in items)
    finally:
        _cleanup(db, *[(WebsitePage, p.id) for p in pages])
        db.close()


# ---------------------------------------------------------------------------
# G.  Notice-frame evidence (current-status questions read newest notice)
# ---------------------------------------------------------------------------

def test_notice_frame_evidence_without_schedule_rows():
    from app.models.db_models import UniversityNotice

    db = SessionLocal()
    notice = None
    try:
        notice = UniversityNotice(
            id=uuid.uuid4(), title="Admission 2026 - Notice of Open Selection",
            notice_type="notice", filename="adm-2026.pdf", original_filename="adm-2026.pdf",
            file_type="pdf", file_path="/tmp/adm-2026.pdf", extraction_status="verified",
            is_verified=True, is_published=True,
            notification_date=datetime.datetime(2026, 5, 20, tzinfo=datetime.timezone.utc),
            published_at=datetime.datetime(2026, 5, 20, tzinfo=datetime.timezone.utc),
        )
        db.add(notice)
        db.commit()
        db.refresh(notice)
        sub = SubQuery(text="is admission open?", source=SourceType.NOTICES)
        ctx = ConversationContext()
        pool = asyncio.run(collect_evidence(db, [sub], SimpleNamespace(
            programme=None, topic="admission",
        ), ctx))
        items = pool.for_sub(sub.text)
        assert items, "notice-frame evidence must be non-empty even without schedule rows"
        assert items[0].source == SourceType.NOTICES
        assert "Admission 2026" in items[0].text
        assert "published" in items[0].text
        assert items[0].verified is True
    finally:
        _cleanup(db, (UniversityNotice, notice.id))
        db.close()


# ---------------------------------------------------------------------------
# H.  build_intelligent_subs — one implicit fragment, distinct sub-texts
# ---------------------------------------------------------------------------

def test_build_intelligent_subs_single_fragment_distinct_texts():
    entities = SimpleNamespace(programme="mca")
    subs_knowledge = build_intelligent_subs(
        "explain the mca admission procedure", entities, ConversationContext(), kind="knowledge"
    )
    sources = {s.source for s in subs_knowledge}
    texts = [s.text for s in subs_knowledge]
    assert len(texts) == len(set(texts)), "each generated sub must carry a DISTINCT text"
    assert SourceType.DOCUMENTS in sources and SourceType.WEBSITE in sources and SourceType.RAG in sources
    assert SourceType.PROGRAMME in sources

    subs_status = build_intelligent_subs(
        "are admissions open?", SimpleNamespace(programme="mca"), ConversationContext(), kind="status"
    )
    assert subs_status[0].source == SourceType.NOTICES
    assert subs_status[0].text == "are admissions open?"
    assert any(s.source == SourceType.PROGRAMME for s in subs_status)


def test_evidence_item_new_metadata_is_backward_compatible():
    item = EvidenceItem("q?x", SourceType.NOTICES, "some text", direct=True)
    d = item.as_dict()
    assert d["url"] == "" and d["verified"] is False
    assert d["doc_id"] == "" and d["issued_at"] == "" and d["last_synced"] == ""
    assert d["programme"] == "" and d["semester"] == "" and d["batch"] == ""


# ---------------------------------------------------------------------------
# I.  Engine handler — status short-circuit + mode prompt wiring
# ---------------------------------------------------------------------------

def _empty_pool():
    return EvidencePool()


def test_handle_intelligent_status_short_circuits_without_llm(monkeypatch):
    from app.ingest.prompts import CURRENT_STATUS_UNAVAILABLE
    from app.orchestrator import engine as eng

    llm_called = {}

    async def _fake_collect(db, subs_, entities, ctx, rag_ctx=None):
        return _empty_pool()

    async def _boom(original, subs_, pool_, chat_id="", system=None):
        llm_called["called"] = True
        yield "MUST NEVER BE REACHED"

    async def _stub_event(**kw):
        return None

    monkeypatch.setattr("app.multi_source.evidence.collect_evidence", _fake_collect)
    monkeypatch.setattr("app.multi_source.synthesize.synthesize_answer", _boom)
    monkeypatch.setattr(eng, "collect_event", _stub_event)

    plan_result = SimpleNamespace(
        action="intelligent",
        extra={"original_query": "are admissions open?", "intelligent_kind": "status"},
    )
    ctx = ConversationContext()
    state = SimpleNamespace(last_intent="none")
    events = []

    async def _drain():
        async for ev in eng._handle_intelligent(
            None, "u", "are admissions open?", "sa-status", state, ctx,
            SimpleNamespace(programme=None, topic="admission"), plan_result,
        ):
            events.append(ev)

    asyncio.run(_drain())
    tokens = [e.get("text", "") for e in events if e.get("type") == "token"]
    assert tokens == [CURRENT_STATUS_UNAVAILABLE], f"tokens={tokens}"
    assert not llm_called, "current-status with no official evidence must NOT call the LLM"
    done = [e for e in events if e.get("type") == "done"]
    assert done and done[0]["intelligent_debug"]["short_circuit"] == "no_current_official_evidence"


def test_handle_intelligent_knowledge_passes_mode_prompt_and_citations(monkeypatch):
    from app.ingest.prompts import STUDENT_ASSISTANT_SYSTEM_PROMPT
    from app.orchestrator import engine as eng

    q = "explain the mca admission procedure"
    pool = EvidencePool()
    pool.add(EvidenceItem(
        sub_question=q, source=SourceType.RAG,
        text="MCA admission begins with the entrance test.",
        title="Admission Prospectus 2026", source_id="doc-1", relevance=0.9, direct=True,
    ))
    captured = {}

    async def _fake_collect(db, subs_, entities, ctx, rag_ctx=None):
        captured["subs"] = list(subs_)
        return pool

    async def _fake_synth(original, subs_, pool_, chat_id="", system=None):
        captured["system"] = system
        captured["original"] = original
        yield "The MCA admission procedure begins with the entrance test."

    async def _stub_event(**kw):
        return None

    monkeypatch.setattr("app.multi_source.evidence.collect_evidence", _fake_collect)
    monkeypatch.setattr("app.multi_source.synthesize.synthesize_answer", _fake_synth)
    monkeypatch.setattr(eng, "collect_event", _stub_event)

    plan_result = SimpleNamespace(
        action="intelligent",
        extra={"original_query": q, "intelligent_kind": "knowledge"},
    )
    ctx = ConversationContext()
    state = SimpleNamespace(last_intent="none")
    events = []

    async def _drain():
        async for ev in eng._handle_intelligent(
            None, "u", q, "sa-k", state, ctx,
            SimpleNamespace(programme="mca", topic="admission"), plan_result,
        ):
            events.append(ev)

    asyncio.run(_drain())
    assert captured["system"] == STUDENT_ASSISTANT_SYSTEM_PROMPT
    tokens = [e.get("text", "") for e in events if e.get("type") == "token"]
    assert "entrance test" in tokens[0]
    done = [e for e in events if e.get("type") == "done"]
    assert done and done[0]["intelligent_debug"]["kind"] == "knowledge"


# ---------------------------------------------------------------------------
# J.  Prompt contract — no internal terminology, current-status honesty
# ---------------------------------------------------------------------------

def test_student_assistant_prompt_contract():
    from app.ingest.prompts import CURRENT_STATUS_UNAVAILABLE, STUDENT_ASSISTANT_SYSTEM_PROMPT

    prompt = STUDENT_ASSISTANT_SYSTEM_PROMPT.lower()
    # The LLM must never parrot internal retrieval machinery in an answer.
    for term in ("rag", "chroma", "bm25", "vector", "retrieval", "sub-question", "evidence"):
        assert term in prompt, f"mode prompt must instruct against exposing {term!r}"

    # Current-status honesty: "not found" must never be turned into "not announced".
    assert "current-status" in prompt.lower()
    assert "old date" in prompt.lower()
    assert CURRENT_STATUS_UNAVAILABLE
    assert "old date" in CURRENT_STATUS_UNAVAILABLE
    assert "as if" in CURRENT_STATUS_UNAVAILABLE

    # A bare current-state claim ("admission is open", "result declared") must never
    # be grounded in programme-profile facts (eligibility / fee / duration). Only a
    # dated official announcement in the evidence may support such a claim.
    assert "admission is open" in prompt
    assert "official announcement" in prompt
    assert "not an announcement" in prompt
    assert "duration" in prompt