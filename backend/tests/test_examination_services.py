"""
Examinations dedicated services — Model Papers / Exam Fee Structure / Division
Improvement.

Covers:
  1. Planner Rule 3aa: natural-language + chip-click routing to the
     ``examination`` action; spec exclusions (PYQ / previous-year /
     non-exam fees / bare marks-improvement) never route there.
  2. Engine handler: Model Papers are served from VERIFIED corpus rows only
     (pending-review, wrong-category and escaped-path rows never surface); the
     no-verified fallback is an honest message, never fabricated. Exam Fee /
     Division Improvement fall back to the exact not-available text when no
     verified official source exists.
  3. Secure file endpoint / containment: verified-only resolution and
     raw_store containment (traversal / absolute paths -> None).
  4. Document-scoped RAG follow-ups: chat/service._scope_to_documents keeps
     only the scoped documents' chunks.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from types import SimpleNamespace

import pytest

from app.database import SessionLocal, create_all
from app.examination import service as exam_svc
from app.knowledge_sync.raw_store import raw_root, store_raw
from app.models import WebsitePage
from app.orchestrator.context import ConversationContext
from app.orchestrator.engine import (
    _MODEL_PAPER_NO_SUBSTITUTION,
    apply_model_paper_selection,
    clear_exam_scope,
    _handle_examination_service,
)
from app.orchestrator.extractor import extract_entities
from app.orchestrator.planner import plan
from app.orchestrator.state import ConversationState

create_all()


# ---------------------------------------------------------------------------
# Shared seed: one eligible verified paper + ineligible neighbours.
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def _seed():
    """Seed a shared dataset ONCE (fresh session per test, cleaned afterward).

    - PAPER_OK: VERIFIED model paper with a real raw file (the only card that
      may ever surface, and the only file the secure endpoint may serve).
    - PAPER_PENDING: model paper PENDING_REVIEW (must NEVER surface).
    - PAPER_NOT_MODEL: VERIFIED but classified as official-notification
      (must NEVER surface as a model paper).
    - PAPER_ESCAPE: VERIFIED model paper whose raw_path escapes the raw root
      (file resolution -> None, must never be served).
    """
    db = SessionLocal()
    paper_ok_file = store_raw(b"%PDF-1.4\n%%CUStest%%\n", "pdf")
    assert paper_ok_file is not None
    paper_pending_file = store_raw(b"%PDF-1.4\n%%CUStest-pending%%\n", "pdf")
    assert paper_pending_file is not None

    paper_ok = WebsitePage(
        id=str(uuid.uuid4()),
        document_id="doc-botany-ok",
        url="https://www.cusrinagar.edu.in/exams/Botany_Model_Paper.pdf",
        title="Botany Model Paper",
        category="model-paper",
        content_type="pdf",
        raw_path=paper_ok_file["rel_path"],
        status="new",
        doc_type="official",
        classification_status="verified",
        classification_confidence={"band": "high", "score": 90.0},
        doc_meta={"subject": "Botany", "semester": 4},
    )
    paper_pending = WebsitePage(
        id=str(uuid.uuid4()),
        document_id="doc-chem-pending",
        url="https://www.cusrinagar.edu.in/exams/Chemistry_Model_Paper.pdf",
        title="Chemistry Model Paper",
        category="model-paper",
        content_type="pdf",
        raw_path=paper_pending_file["rel_path"],
        status="new",
        doc_type="official",
        classification_status="pending_review",
        classification_confidence={"band": "high", "score": 90.0},
    )
    paper_not_model = WebsitePage(
        id=str(uuid.uuid4()),
        document_id="doc-fee-notice",
        url="https://www.cusrinagar.edu.in/Home/Notification",
        title="Fee Notification",
        category="official-notification",
        content_type="pdf",
        raw_path=paper_ok_file["rel_path"],
        status="new",
        doc_type="official",
        classification_status="verified",
    )
    paper_escape = WebsitePage(
        id=str(uuid.uuid4()),
        document_id="doc-hist-escape",
        url="https://www.cusrinagar.edu.in/exams/History_Model_Paper.pdf",
        title="History Model Paper",
        category="model-paper",
        content_type="pdf",
        raw_path=os.path.join("..", "..", "secret.pdf"),
        status="new",
        doc_type="official",
        classification_status="verified",
    )
    # Additional VERIFIED, file-backed papers with NO doc_meta at all: their
    # subject/programme/semester/batch metadata is derived deterministically
    # from the title/URL by app.examination.metadata (authority rule 2).
    extra_rows = []
    for title, slug in (
        ("MCA 3rd Semester Zoology Model Paper", "mca-3rd-sem-zoology-model-paper.pdf"),
        ("MCA 3rd Semester Chemistry Model Paper", "mca-3rd-sem-chemistry-model-paper.pdf"),
        ("MCA 4th Semester Botany Model Paper", "mca-4th-sem-botany-model-paper.pdf"),
        ("BCA 5th Semester English Model Paper", "bca-5th-sem-english-model-paper.pdf"),
        ("MCA Batch 2024 Chemistry Model Paper", "mca-batch-2024-chemistry-model-paper.pdf"),
    ):
        f = store_raw(b"%PDF-1.4\n%%CUStest-extra%%\n", "pdf")
        assert f is not None
        extra_rows.append(
            WebsitePage(
                id=str(uuid.uuid4()),
                document_id="doc-" + slug.replace(".pdf", ""),
                url="https://www.cusrinagar.edu.in/exams/" + slug,
                title=title,
                category="model-paper",
                content_type="pdf",
                raw_path=f["rel_path"],
                status="new",
                doc_type="official",
                classification_status="verified",
                classification_confidence={"band": "high", "score": 90.0},
                doc_meta=None,
            )
        )
    rows = [paper_ok, paper_pending, paper_not_model, paper_escape] + extra_rows
    db.add_all(rows)
    db.commit()
    yield {"ok_id": str(paper_ok.id), "pending": str(paper_pending.id),
           "not_model": str(paper_not_model.id), "escape": str(paper_escape.id)}
    try:
        for r in rows:
            db.delete(r)
        db.commit()
    finally:
        for rel in (paper_ok_file["rel_path"], paper_pending_file["rel_path"]):
            try:
                os.remove(raw_root() / rel)
            except OSError:
                pass
        for r in extra_rows:
            try:
                os.remove(raw_root() / r.raw_path)
            except OSError:
                pass
        db.close()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _route(raw: str):
    ctx = ConversationContext()
    e = extract_entities(raw)
    return plan(raw, ctx, "rt-" + uuid.uuid4().hex[:8], e)


def _drain(db, target: str, extra: dict | None = None, state: ConversationState | None = None):
    plan_result = SimpleNamespace(
        action="examination",
        target=target,
        confidence=0.93,
        reason="test",
        extra=extra or {},
        response=None,
    )

    async def _run():
        events = []
        async for ev in _handle_examination_service(
            db, "eng-chat", state or ConversationState(chat_id="eng-chat"), plan_result,
        ):
            events.append(ev)
        return events

    return asyncio.run(_run())


def _drain_plan(db, plan_result, state: ConversationState | None = None):
    """Run the engine handler against a REAL planner Plan (end-to-end)."""

    async def _run():
        events = []
        async for ev in _handle_examination_service(
            db, "eng-chat", state or ConversationState(chat_id="eng-chat"), plan_result,
        ):
            events.append(ev)
        return events

    return asyncio.run(_run())


async def _run_chat_events(db, message, context):
    from app.chat import service as chat_svc
    events = []
    async for ev in chat_svc.run_chat(db, "eng-user", message, "chat-1", context=context):
        events.append(ev)
    return events


# ---------------------------------------------------------------------------
# 1. Planner routing (Rule 3aa)
# ---------------------------------------------------------------------------

def test_planner_routes_examination_services():
    cases = {
        "model_papers": [
            "model papers",
            "model question papers",
            "show model papers",
            "bca model paper for 4th semester",
            "sample papers for examination",
            "practice paper bca",
            "model_papers",  # chip id as the frontend sends it
        ],
        "fee_structure_exam": [
            "what is the exam fee",
            "examination fee structure",
            "exam form fee",
            "bca examination fee",
            "fee_structure_exam",  # chip id
        ],
        "division_improvement": [
            "division improvement",
            "improve my division",
            "how to improve my division",
            "improvement exam",
            "division_improvement",  # chip id
            "what is the division criteria",
        ],
    }
    for target, phrases in cases.items():
        for raw in phrases:
            p = _route(raw)
            assert p.action == "examination", f"{raw!r} -> {p.action}"
            assert p.target == target, f"{raw!r} -> {p.target}"


def test_planner_exclusions_never_examination():
    # Spec exclusions must keep their existing flows (never the examination
    # action): PYQ / previous-year / pattern / non-exam fees / bare marks.
    for raw in (
        "previous year question paper",
        "past year papers mca",
        "question paper pattern btech",
        "marking scheme bca",
        "admission fee for bca",
        "tuition fee mca",
        "hostel fee",
        "registration fee",
        "improve my marks",
        "how to improve my marks",
    ):
        p = _route(raw)
        assert p.action != "examination", f"{raw!r} -> {p.action}"


def test_planner_model_papers_hints_programme():
    p = _route("bca model papers")
    assert p.action == "examination"
    assert p.target == "model_papers"
    assert p.extra.get("programme") == "bca"


# ---------------------------------------------------------------------------
# 2. Engine handler — verified-only + no-fabrication gates
# ---------------------------------------------------------------------------

def test_model_paper_list_serves_verified_only(_seed):
    db = SessionLocal()
    try:
        events = _drain(db, "model_papers")
        assert events[0]["type"] == "model_paper_list"
        assert events[-1]["type"] == "done"
        payload = events[0]
        papers = payload["papers"]
        titles = {p["title"] for p in papers}
        assert "Botany Model Paper" in titles
        assert "Chemistry Model Paper" not in titles      # pending_review
        assert "Fee Notification" not in str(payload)      # wrong category
        assert "History Model Paper" not in titles         # escaped path (no raw)
        assert all(p["file_url"].startswith("/api/examinations/model-papers/")
                   for p in papers)
        # Official metadata fields only when truly present.
        botany = next(p for p in papers if p["title"] == "Botany Model Paper")
        assert botany["subject"] == "Botany"
        assert botany["semester"] == 4
    finally:
        db.close()


def test_model_paper_list_service_scoping(_seed):
    db = SessionLocal()
    try:
        # Service-level document scoping is authoritative for follow-ups.
        assert exam_svc.list_model_papers(db, document_ids=["no-such-doc"]) == []
        all_papers = exam_svc.list_model_papers(db)
        titles = {p["title"] for p in all_papers}
        assert "Botany Model Paper" in titles
        assert "Chemistry Model Paper" not in titles
        # The engine path still renders a list event for the unpolluted scope.
        events = _drain(db, "model_papers")
        assert events[0]["type"] == "model_paper_list"
    finally:
        db.close()


def test_fee_and_division_fallback_are_honest_tokens():
    db = SessionLocal()
    try:
        for target, needle in (
            ("fee_structure_exam", "I don't have information available"),
            ("division_improvement", "I don't have information available"),
        ):
            events = _drain(db, target)
            assert events[0]["type"] == "token", events[0].get("type")
            assert needle in events[0]["text"]
            assert events[-1]["type"] == "done"
        # No structured LLM payload ever leaks into the fee/division path.
        assert all(e["type"] in ("token", "done") for e in
                   _drain(db, "fee_structure_exam"))
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 3. Service layer — secure resolution / containment
# ---------------------------------------------------------------------------

def test_service_verified_only_gating(_seed):
    db = SessionLocal()
    try:
        assert exam_svc.get_model_paper(db, _seed["ok_id"]) is not None
        assert exam_svc.get_model_paper(db, _seed["pending"]) is None
        assert exam_svc.get_model_paper(db, _seed["not_model"]) is None
    finally:
        db.close()


def test_file_resolution_refuses_escape_and_missing(_seed):
    db = SessionLocal()
    try:
        ok = exam_svc.get_model_paper(db, _seed["ok_id"])
        path = exam_svc.resolve_model_paper_file(ok)
        assert path is not None and path.is_file()

        escaper = exam_svc.get_model_paper(db, _seed["escape"])
        assert escaper is not None
        assert exam_svc.resolve_model_paper_file(escaper) is None  # containment

        assert exam_svc.resolve_model_paper_file({"raw_path": "../secret.pdf"}) is None
        assert exam_svc.resolve_model_paper_file({"raw_path": "C:/Windows/system32/x.pdf"}) is None
        assert exam_svc.resolve_model_paper_file({"raw_path": ""}) is None
        assert exam_svc.resolve_model_paper_file({}) is None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 4. Document-scoped RAG follow-ups
# ---------------------------------------------------------------------------

def test_scope_to_documents_filters_chunks():
    from app.chat.service import _scope_to_documents

    chunks = [
        {"document_id": "doc-a", "text": "one"},
        {"document_id": "doc-b", "text": "two"},
        {"text": "no id"},
    ]
    scoped = _scope_to_documents(chunks, {"document_ids": ["doc-a"]})
    assert [c["document_id"] for c in scoped] == ["doc-a"]
    assert _scope_to_documents(chunks, {"document_ids": ["nope"]}) == []
    assert _scope_to_documents(chunks, {"document_ids": ["doc-a", "doc-b"]}) == chunks[:2]
    assert _scope_to_documents(chunks, {"document_id": "doc-b"}) == chunks[1:2]
    assert _scope_to_documents(chunks, {}) == chunks
    assert _scope_to_documents(chunks, None) == chunks


# ---------------------------------------------------------------------------
# 5. Deterministic metadata derivation (authority rule 2)
# ---------------------------------------------------------------------------

def test_metadata_derived_deterministically_from_title(_seed):
    db = SessionLocal()
    try:
        papers = {p["title"]: p for p in exam_svc.list_model_papers(db)}
        zoo = papers["MCA 3rd Semester Zoology Model Paper"]
        assert zoo["subject"] == "Zoology"
        assert zoo["programme"] == "mca"
        assert zoo["semester"] == "3"
        chem4 = papers["MCA 4th Semester Botany Model Paper"]
        assert chem4["subject"] == "Botany"
        assert chem4["semester"] == "4"
        batch = papers["MCA Batch 2024 Chemistry Model Paper"]
        assert batch["batch"] == "2024"
        assert batch["academic_year"] == "2024"
        assert "semester" not in batch
        eng = papers["BCA 5th Semester English Model Paper"]
        assert eng["programme"] == "bca"
        assert eng["subject"] == "English"
        # doc_meta authority rule: the seeded Botany row keeps its crawler value.
        assert papers["Botany Model Paper"]["subject"] == "Botany"
        assert papers["Botany Model Paper"]["semester"] == 4
    finally:
        db.close()


def test_normalize_semester_never_reads_batch_as_semester():
    from app.examination.metadata import normalize_semester
    assert normalize_semester("MCA 2024 Batch Chemistry Model Paper") is None
    assert normalize_semester("MCA 3rd Semester Zoology") == 3
    assert normalize_semester("4th sem") == 4


# ---------------------------------------------------------------------------
# 6. Strict AND filtering at the service layer
# ---------------------------------------------------------------------------

def test_list_model_papers_strict_and_filters(_seed):
    db = SessionLocal()
    try:
        titles = lambda rows: {p["title"] for p in rows}
        assert titles(exam_svc.list_model_papers(db, programme="mca", semester=3, subject="Zoology")) == {
            "MCA 3rd Semester Zoology Model Paper"}
        assert titles(exam_svc.list_model_papers(db, programme="mca", semester=3, subject="Chemistry")) == {
            "MCA 3rd Semester Chemistry Model Paper"}
        # Strict AND: the right programme + subject but the WRONG semester.
        assert exam_svc.list_model_papers(db, programme="mca", semester=4, subject="Zoology") == []
        # One constraint alone still filters.
        assert titles(exam_svc.list_model_papers(db, subject="English")) == {
            "BCA 5th Semester English Model Paper"}
        assert {p["title"] for p in exam_svc.list_model_papers(db, batch="2024")} == {
            "MCA Batch 2024 Chemistry Model Paper"}
        assert {p["title"] for p in exam_svc.list_model_papers(db, academic_year="2024")} == {
            "MCA Batch 2024 Chemistry Model Paper"}
        # programme + semester across the two Chemistry papers.
        assert titles(exam_svc.list_model_papers(db, programme="mca", semester=3, subject="Chemistry")) == {
            "MCA 3rd Semester Chemistry Model Paper"}
        assert titles(exam_svc.list_model_papers(db, programme="mca", subject="Chemistry")) == {
            "MCA 3rd Semester Chemistry Model Paper",
            "MCA Batch 2024 Chemistry Model Paper"}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 7. Engine: constrained requests — filter + strict no-substitution
# ---------------------------------------------------------------------------

def test_engine_constrained_empty_is_exact_no_substitution(_seed):
    db = SessionLocal()
    try:
        # No verified MCA/4th/Zoology paper exists: MUST return EXACTLY the
        # no-substitution text — never a different programme's paper.
        events = _drain(db, "model_papers",
                        extra={"programme": "mca", "semester": 4, "subject": "Zoology"})
        assert events[0]["type"] == "token"
        assert events[0]["text"] == _MODEL_PAPER_NO_SUBSTITUTION
        assert events[-1]["type"] == "done"

        events = _drain(db, "model_papers",
                        extra={"programme": "msc", "subject": "Zoology"})
        assert events[0]["type"] == "token"
        assert events[0]["text"] == _MODEL_PAPER_NO_SUBSTITUTION

        events = _drain(db, "model_papers", extra={"academic_year": "1999"})
        assert events[0]["type"] == "token"
        assert events[0]["text"] == _MODEL_PAPER_NO_SUBSTITUTION
    finally:
        db.close()


def test_engine_constrained_success_renders_filtered_cards(_seed):
    db = SessionLocal()
    try:
        state = ConversationState(chat_id="eng-chat")
        events = _drain(db, "model_papers",
                        extra={"programme": "mca", "semester": 3, "subject": "Zoology"},
                        state=state)
        assert events[0]["type"] == "model_paper_list"
        assert [p["title"] for p in events[0]["papers"]] == [
            "MCA 3rd Semester Zoology Model Paper"]
        assert "Zoology" in events[0]["message"]
    finally:
        db.close()


def test_engine_broad_list_does_not_establish_document_scope(_seed):
    """A broad model-papers list must NOT pin follow-up RAG to those papers
    (cross-paper leakage). Scope is established ONLY by explicit selection."""
    db = SessionLocal()
    try:
        state = ConversationState(chat_id="eng-chat")
        _drain(db, "model_papers", state=state)
        assert state.context.exam_document_ids is None
        assert state.context.selected_model_paper_id is None

        # Constrained lists clear too.
        _drain(db, "model_papers",
               extra={"programme": "mca", "semester": 3, "subject": "Zoology"},
               state=state)
        assert state.context.exam_document_ids is None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 8. Planner constraint attachment + end-to-end no-substitution
# ---------------------------------------------------------------------------

def test_planner_attaches_model_paper_constraints():
    cases = [
        ("MCA 3rd semester Zoology model papers",
         {"programme": "mca", "semester": 3, "subject": "Zoology"}),
        ("BCA 5th semester model papers",
         {"programme": "bca", "semester": 5, "subject": None}),
        ("model papers for batch 2024",
         {"programme": None, "semester": None, "subject": None}),
        ("Botany model paper 4th semester",
         {"programme": None, "semester": 4, "subject": "Botany"}),
    ]
    for raw, expected in cases:
        p = _route(raw)
        assert p.action == "examination", raw
        assert p.target == "model_papers", raw
        for key in ("programme", "semester", "subject"):
            assert p.extra.get(key) == expected[key], f"{raw!r} key={key} got={p.extra.get(key)} expected={expected[key]}"


def test_planner_model_papers_batch_academic_year_attached():
    p = _route("model papers for batch 2024 of MCA")
    assert p.action == "examination"
    assert p.target == "model_papers"
    assert p.extra.get("programme") == "mca"
    assert p.extra.get("batch") == "2024"


def test_end_to_end_no_substitution_planner_to_engine(_seed):
    """Real planner->engine: "MCA 4th semester Zoology model papers" must end
    with the EXACT no-substitution token (the MCA 4th-sem Botany paper must
    never be served as a substitute)."""
    db = SessionLocal()
    try:
        ctx = ConversationContext()
        e = extract_entities("MCA 4th semester Zoology model papers")
        plan_result = plan("MCA 4th semester Zoology model papers", ctx, "eng-chat", e)
        assert plan_result.action == "examination"
        assert plan_result.extra.get("programme") == "mca"
        assert plan_result.extra.get("subject") == "Zoology"
        assert plan_result.extra.get("semester") == 4

        events = _drain_plan(db, plan_result)
        assert events[0]["type"] == "token"
        assert events[0]["text"] == _MODEL_PAPER_NO_SUBSTITUTION
        assert events[-1]["type"] == "done"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 9. Single-paper selection: server-validated, single-document scope
# ---------------------------------------------------------------------------

def test_select_model_paper_service_verified_only(_seed):
    db = SessionLocal()
    try:
        ok = exam_svc.select_model_paper(db, _seed["ok_id"])
        assert ok is not None and ok["document_id"]
        assert ok["subject"] == "Botany"
        assert exam_svc.select_model_paper(db, _seed["pending"]) is None
        assert exam_svc.select_model_paper(db, _seed["not_model"]) is None
        assert exam_svc.select_model_paper(db, _seed["escape"]) is None
        assert exam_svc.select_model_paper(db, "no-such-id") is None
    finally:
        db.close()


def test_apply_model_paper_selection_single_document_scope(_seed):
    db = SessionLocal()
    try:
        papers = exam_svc.list_model_papers(db)
        paper_a = next(p for p in papers if p["title"] == "MCA 3rd Semester Zoology Model Paper")
        paper_b = next(p for p in papers if p["title"] == "BCA 5th Semester English Model Paper")

        ctx = ConversationContext()
        apply_model_paper_selection(ctx, paper_a)
        assert ctx.domain == "examination"
        assert ctx.selected_model_paper_id == paper_a["id"]
        assert ctx.selected_document_id == paper_a["document_id"]
        assert ctx.exam_document_ids == [paper_a["document_id"]]

        # Selecting ANOTHER paper must move the scope to B exactly (overwrite).
        apply_model_paper_selection(ctx, paper_b)
        assert ctx.selected_document_id == paper_b["document_id"]
        assert ctx.exam_document_ids == [paper_b["document_id"]]

        clear_exam_scope(ctx)
        assert ctx.exam_document_ids is None
        assert ctx.selected_model_paper_id is None
        assert ctx.selected_document_id is None
    finally:
        db.close()


def test_build_metadata_filter_document_scope():
    """The ACTUAL search call receives the selected document — verify the
    where-filter builder restricts to [A] and never [A, B]."""
    from app.ingest.retriever import build_metadata_filter

    assert build_metadata_filter({"document_ids": ["doc-a"]}) == {"document_id": "doc-a"}
    assert build_metadata_filter({"document_id": "doc-a"}) == {"document_id": "doc-a"}
    both = build_metadata_filter({"document_ids": ["doc-a", "doc-b"]})
    assert both in ({"$or": [{"document_id": "doc-a"}, {"document_id": "doc-b"}]},
                    {"$or": [{"document_id": "doc-b"}, {"document_id": "doc-a"}]})
    combined = build_metadata_filter({"document_ids": ["doc-a"], "programme": "bca"})
    assert combined == {"$and": [{"programme": "bca"}, {"document_id": "doc-a"}]}
    assert build_metadata_filter({}) is None
    assert build_metadata_filter({"document_ids": []}) is None


def test_run_chat_doc_scoped_empty_returns_exact_no_substitution(monkeypatch):
    """Document-scoped follow-up with zero evidence MUST return EXACTLY
    'I don't have information available.' — never the generic fallback."""
    from app.chat import service as chat_svc

    monkeypatch.setattr(chat_svc, "retrieve", lambda *a, **k: [])
    monkeypatch.setattr(chat_svc, "_is_outside_scope", lambda *a: False)

    db = SessionLocal()
    try:
        events = asyncio.run(_run_chat_events(
            db, "what is the question format?", {"document_ids": ["doc-a"]}))
        tokens = [e["text"] for e in events if e.get("type") == "token"]
        assert tokens == [_MODEL_PAPER_NO_SUBSTITUTION]
    finally:
        db.close()


def test_run_chat_doc_scoped_insufficient_evidence_returns_exact(monkeypatch):
    """Case B (the audit defect): an IN-SCOPE chunk is retrieved from the
    selected paper, but it does not support the requested answer (the fact
    exists only in another paper). The response must be EXACTLY
    'I don't have information available.' — the generic knowledge-base
    refusal must never leak to the user, the foreign paper must be dropped
    from evidence, and retrieval evidence may contain ONLY the selected
    document."""
    from app.chat import service as chat_svc

    chunk_a = {
        "document_id": "doc-a",
        "document_title": "MCA 1st Semester Mathematics Model Paper",
        "page_number": 1,
        "chunk_index": 0,
        "rerank_score": 0.96,
        "content": "ALPHA_UNIQUE_FACT is mentioned in the mathematics paper.",
    }
    foreign_b = {
        "document_id": "doc-b",
        "document_title": "BCA 2nd Semester Physics Model Paper",
        "page_number": 1,
        "chunk_index": 0,
        "rerank_score": 0.95,
        "content": "BETA_UNIQUE_FACT is mentioned in the physics paper.",
    }

    async def confession_stream(*a, **k):
        yield "I couldn't find this information in the Cluster University Srinagar knowledge base."

    monkeypatch.setattr(chat_svc, "retrieve", lambda *a, **k: [chunk_a, foreign_b])
    monkeypatch.setattr(chat_svc, "_is_outside_scope", lambda *a: False)
    monkeypatch.setattr(chat_svc, "stream_answer_async", confession_stream)

    db = SessionLocal()
    try:
        events = asyncio.run(_run_chat_events(
            db, "Tell me the BETA_UNIQUE_FACT fact.",
            {"document_ids": ["doc-a"]}))
        tokens = [e["text"] for e in events if e.get("type") == "token"]
        assert tokens == [_MODEL_PAPER_NO_SUBSTITUTION]
        done = [e for e in events if e.get("type") == "done"][0]
        cited = {c["document_id"] for c in done.get("cited_chunks", [])}
        # Single-document invariant: Paper B never enters the evidence.
        assert cited == {"doc-a"}
    finally:
        db.close()


def test_run_chat_doc_scoped_switch_scope_never_searches_old_paper(monkeypatch):
    """After the selection moved to Paper B (scope = [B]), an A-only question
    must NOT search A: in-scope evidence may only be B, and the exact
    no-substitution text is returned."""
    from app.chat import service as chat_svc

    chunk_b = {
        "document_id": "doc-b",
        "document_title": "BCA 2nd Semester Physics Model Paper",
        "page_number": 1,
        "chunk_index": 0,
        "rerank_score": 0.9,
        "content": "The physics paper only describes BETA_UNIQUE_FACT.",
    }
    foreign_a = {
        "document_id": "doc-a",
        "document_title": "MCA 1st Semester Mathematics Model Paper",
        "page_number": 1,
        "chunk_index": 0,
        "rerank_score": 0.89,
        "content": "ALPHA_UNIQUE_FACT is mentioned in the mathematics paper.",
    }

    async def confession_stream(*a, **k):
        yield "I couldn't find this information in the Cluster University Srinagar knowledge base."

    monkeypatch.setattr(chat_svc, "retrieve", lambda *a, **k: [chunk_b, foreign_a])
    monkeypatch.setattr(chat_svc, "_is_outside_scope", lambda *a: False)
    monkeypatch.setattr(chat_svc, "stream_answer_async", confession_stream)

    db = SessionLocal()
    try:
        events = asyncio.run(_run_chat_events(
            db, "What does paper A say about ALPHA_UNIQUE_FACT?",
            {"document_ids": ["doc-b"]}))
        tokens = [e["text"] for e in events if e.get("type") == "token"]
        assert tokens == [_MODEL_PAPER_NO_SUBSTITUTION]
        done = [e for e in events if e.get("type") == "done"][0]
        cited = {c["document_id"] for c in done.get("cited_chunks", [])}
        assert cited == {"doc-b"}
    finally:
        db.close()


def test_run_chat_doc_scoped_supported_answer_passes_through(monkeypatch):
    """Supported case: when the selected paper DOES contain the answer, the
    validated generation passes through unchanged (answer from B)."""
    from app.chat import service as chat_svc

    chunk_b = {
        "document_id": "doc-b",
        "document_title": "BCA 2nd Semester Physics Model Paper",
        "page_number": 1,
        "chunk_index": 0,
        "rerank_score": 0.92,
        "content": "BETA_UNIQUE_FACT is the physics fact.",
    }
    answer = ("BETA_UNIQUE_FACT is the physics fact. "
              "[Source: BCA 2nd Semester Physics Model Paper, Page 1]")

    async def ok_stream(*a, **k):
        yield answer

    monkeypatch.setattr(chat_svc, "retrieve", lambda *a, **k: [chunk_b])
    monkeypatch.setattr(chat_svc, "_is_outside_scope", lambda *a: False)
    monkeypatch.setattr(chat_svc, "stream_answer_async", ok_stream)

    db = SessionLocal()
    try:
        events = asyncio.run(_run_chat_events(
            db, "What is the unique fact in the physics paper?",
            {"document_ids": ["doc-b"]}))
        tokens = [e["text"] for e in events if e.get("type") == "token"]
        assert tokens == [answer]
        done = [e for e in events if e.get("type") == "done"][0]
        cited = {c["document_id"] for c in done.get("cited_chunks", [])}
        assert cited == {"doc-b"}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 10. HTTP contract — secure file endpoint (view / download), selection route
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def _http_client():
    from fastapi.testclient import TestClient
    from app.main import app
    return TestClient(app)


def _admin_token(client) -> str:
    # Bootstrap a deterministic admin in the rerouted test DB (independent of
    # whether the FastAPI lifespan ran the env-based admin seed).
    from app.auth.security import hash_password
    from app.models import User
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.username == "exam-admin").first()
        if user is None:
            user = User(
                id=str(uuid.uuid4()),
                username="exam-admin",
                email="exam-admin@cus.ac.in",
                hashed_password=hash_password("exam-admin-pass"),
                role="admin",
                is_active=True,
            )
            db.add(user)
            db.commit()
    finally:
        db.close()
    r = client.post("/api/auth/login", data={"username": "exam-admin", "password": "exam-admin-pass"})
    assert r.status_code == 200, r.text
    return r.json()["access_token"]


def test_http_file_view_serves_verified_bytes(_seed, _http_client):
    r = _http_client.get(f"/api/examinations/model-papers/{_seed['ok_id']}/file")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/pdf"
    assert r.headers["content-disposition"].startswith("inline;")
    assert r.content == b"%PDF-1.4\n%%CUStest%%\n"
    # Single sensible extension (never a doubled ".pdf.pdf").
    assert "Botany_Model_Paper.pdf" in r.headers["content-disposition"]
    assert ".pdf.pdf" not in r.headers["content-disposition"]


def test_http_file_download_attachment(_seed, _http_client):
    r = _http_client.get(f"/api/examinations/model-papers/{_seed['ok_id']}/file?download=1")
    assert r.status_code == 200
    assert r.headers["content-disposition"].startswith("attachment;")
    assert r.content == b"%PDF-1.4\n%%CUStest%%\n"


def test_http_file_security_refuses_unavailable(_seed, _http_client):
    # Pending-review, wrong-category and unknown ids all 404 (never served).
    for pid in (_seed["pending"], _seed["not_model"], _seed["escape"],
                "00000000-0000-0000-0000-000000000000"):
        r = _http_client.get(f"/api/examinations/model-papers/{pid}/file")
        assert r.status_code == 404, f"{pid} -> {r.status_code}"
    # Traversal / absolute-path ids are invalid UUIDs and 422/404 either way.
    assert _http_client.get("/api/examinations/model-papers/../../secret/file").status_code in (404, 422)


def test_http_select_requires_auth(_seed, _http_client):
    r = _http_client.post(
        f"/api/examinations/model-papers/{_seed['ok_id']}/select",
        json={"chat_id": "http-sel-anon"},
    )
    assert r.status_code == 401


def test_http_select_applies_single_document_scope(_seed, _http_client):
    from app.orchestrator.state import get_state as _get_state

    token = _admin_token(_http_client)
    headers = {"Authorization": f"Bearer {token}"}
    chat_id = "http-sel-ok"
    r = _http_client.post(
        f"/api/examinations/model-papers/{_seed['ok_id']}/select",
        headers=headers,
        json={"chat_id": chat_id},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["document_id"] == "doc-botany-ok"
    st = asyncio.run(_get_state(chat_id))
    assert st.context.selected_document_id == "doc-botany-ok"
    assert st.context.exam_document_ids == ["doc-botany-ok"]
    assert st.context.domain == "examination"


def test_http_select_refuses_unverified(_seed, _http_client):
    token = _admin_token(_http_client)
    headers = {"Authorization": f"Bearer {token}"}
    for pid in (_seed["pending"], _seed["not_model"], _seed["escape"]):
        r = _http_client.post(
            f"/api/examinations/model-papers/{pid}/select",
            headers=headers,
            json={"chat_id": "http-sel-bad"},
        )
        assert r.status_code == 404, f"{pid} -> {r.status_code}"


# ---------------------------------------------------------------------------
# 11. Navigation back — leaving the selected-paper flow clears the scope
# ---------------------------------------------------------------------------

def test_back_navigation_clears_exam_scope(_seed):
    """'← Back' from the selected Model Paper flow must clear the single-doc
    scope so later browsing is never pinned to the old paper."""
    from app.orchestrator.engine import _execute_plan
    from app.orchestrator.planner import Plan

    db = SessionLocal()
    try:
        state = ConversationState(chat_id="nav-chat")
        ctx = state.context
        paper = exam_svc.select_model_paper(db, _seed["ok_id"])
        assert paper is not None
        apply_model_paper_selection(ctx, paper)
        assert ctx.exam_document_ids == [paper["document_id"]]

        plan_result = Plan(
            action="navigation",
            response={"type": "options", "options": [], "title": "Examinations menu"},
            confidence=1.0,
            reason="back",
        )
        e = extract_entities("back")
        e.is_back = True

        async def _run():
            async for _ev in _execute_plan(
                db, "nav-user", "back", "nav-chat", state, ctx, e, plan_result,
            ):
                pass

        asyncio.run(_run())
        assert ctx.exam_document_ids is None
        assert ctx.selected_document_id is None
        assert ctx.selected_model_paper_id is None
    finally:
        db.close()