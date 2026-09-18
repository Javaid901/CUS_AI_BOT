"""
backend/tests/test_p1a_conversation_context.py

Phase 4 P1-A — Conversation Context & Follow-up Resolution.

Deterministic, planner/engine-level matrix (no Ollama, no LLM generation):

  A  programme inheritance          ("What subjects are there?" after MCA)
  B  semester inheritance           ("What about 3rd semester?" after MCA)
  C  anaphora -> exam schedule      ("When are those exams?" after MCA sem 3)
  D  explicit override              ("What about BCA?" after MCA)
  E  continued context               ("How long is it?" after BCA)
  G  no context -> no fabrication   (fresh "What subjects are there?")
  H  unrelated question             ("What is the capital of France?") must NOT
                                    inherit the last programme
  I  protected student service      ("Show my results.") keeps its auth gate
  K  no context -> no fabrication   (fresh "What about 3rd semester?")
  L  semester override              ("What about 4th semester?" after sem 2)

Run:  python -m pytest tests/test_p1a_conversation_context.py -q -p no:cacheprovider
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: F401  (register catalogue tables before any session)

from app.database import SessionLocal, create_all
from app.orchestrator.context import (
    ConversationContext,
    detect_exam_reference,
    is_referential_followup,
    is_university_related,
    resolve_followup,
)
from app.orchestrator.extractor import extract_entities
from app.orchestrator.planner import plan


def _seed() -> None:
    create_all()
    from app.catalogue.seed import seed_catalogue
    db = SessionLocal()
    try:
        seed_catalogue(db)
    finally:
        db.close()


def _ctx(**kw) -> ConversationContext:
    c = ConversationContext()
    for key, value in kw.items():
        setattr(c, key, value)
    return c


@pytest.fixture(scope="module", autouse=True)
def _seeded():
    _seed()


def _plan(msg: str, ctx: ConversationContext):
    return plan(msg, ctx, str(uuid.uuid4()), extract_entities(msg))


def _contract(p) -> dict:
    return (p.extra or {}).get("contract") or {}


# ---------------------------------------------------------------------------
# Resolver primitives
# ---------------------------------------------------------------------------


def test_resolver_primitives():
    assert is_university_related("what is the fee?")
    assert is_university_related("BCA")
    assert not is_university_related("what is the capital of France?")
    assert is_referential_followup("what about it?")
    assert is_referential_followup("when are those exams?")
    assert detect_exam_reference("when are those exams?")
    assert not detect_exam_reference("what is the exam fee?")
    assert not detect_exam_reference("show me model papers")
    assert not detect_exam_reference("exam")


# ---------------------------------------------------------------------------
# C — anaphoric exam reference resolves to the conversation's programme + sem
# ---------------------------------------------------------------------------


def test_exam_followup_resolves_from_context():
    ctx = _ctx(programme="mca", programme_id="mca", semester="3")
    res = resolve_followup("When are those exams?", extract_entities("When are those exams?"), ctx)
    assert res.is_followup
    assert res.kind == "exam_schedule"
    assert res.planning_text is not None
    assert "MCA" in res.planning_text
    assert "semester 3" in res.planning_text
    assert "date sheet" in res.planning_text

    p = _plan(res.planning_text, ctx)
    assert p.action == "university_notices"
    assert (p.extra or {}).get("mode") == "schedule"
    assert (p.extra or {}).get("programme") == "mca"
    assert (p.extra or {}).get("semester") == 3


def test_exam_followup_without_semester():
    ctx = _ctx(programme="mca", programme_id="mca")
    res = resolve_followup("When are the exams?", extract_entities("When are the exams?"), ctx)
    assert res.planning_text is not None
    assert "semester" not in res.planning_text.lower()
    p = _plan(res.planning_text, ctx)
    assert p.action == "university_notices"
    assert (p.extra or {}).get("programme") == "mca"


def test_exam_followup_explicit_programme_not_overridden():
    ctx = _ctx(programme="mca", programme_id="mca", semester="3")
    res = resolve_followup("When are BCA exams?", extract_entities("When are BCA exams?"), ctx)
    assert res.planning_text is None
    assert not res.is_followup


def test_exam_fee_not_treated_as_schedule():
    ctx = _ctx(programme="mca", programme_id="mca", semester="3")
    res = resolve_followup("what is the exam fee?", extract_entities("what is the exam fee?"), ctx)
    assert res.planning_text is None


def test_referential_followup_is_flagged_but_not_rewritten():
    ctx = _ctx(programme="bca", programme_id="bca")
    res = resolve_followup("what about it?", extract_entities("what about it?"), ctx)
    assert res.is_followup
    assert res.planning_text is None


# ---------------------------------------------------------------------------
# H — unrelated questions must not inherit conversation context
# ---------------------------------------------------------------------------


def test_unrelated_question_does_not_inherit_programme():
    ctx = _ctx(programme="mca", programme_id="mca", semester="3")
    p = _plan("What is the capital of France?", ctx)
    assert p.action == "rag"
    assert "MCA" not in (p.target or "")
    assert _contract(p).get("programme") is None


# ---------------------------------------------------------------------------
# A / B — inheritance that must keep working
# ---------------------------------------------------------------------------


def test_programme_inheritance_subjects():
    ctx = _ctx(programme="mca", programme_id="mca")
    p = _plan("What subjects are there?", ctx)
    assert p.action == "catalogue"
    assert p.target == "subjects"


def test_semester_inheritance():
    ctx = _ctx(programme="mca", programme_id="mca")
    p = _plan("What about 3rd semester?", ctx)
    c = _contract(p)
    assert c.get("programme") == "mca"
    assert c.get("semester") == 3


# ---------------------------------------------------------------------------
# D — explicit current-message programme always wins
# ---------------------------------------------------------------------------


def test_explicit_programme_override():
    ctx = _ctx(programme="mca", programme_id="mca")
    p = _plan("What about BCA?", ctx)
    assert _contract(p).get("programme") == "bca"
    assert ctx.programme == "bca"


# ---------------------------------------------------------------------------
# E — duration follow-up
# ---------------------------------------------------------------------------


def test_duration_followup_after_bca():
    ctx = _ctx(programme="bca", programme_id="bca")
    p = _plan("How long is it?", ctx)
    assert p.action == "catalogue"
    assert p.target == "requested"


# ---------------------------------------------------------------------------
# G / K — no context -> ask / pick, never fabricate a programme
# ---------------------------------------------------------------------------


def test_no_context_subjects_asks_for_programme():
    p = _plan("What subjects are there?", _ctx())
    assert p.action == "slot_fill"
    assert (p.extra or {}).get("slot") == "programme"


def test_no_context_semester_does_not_fabricate():
    p = _plan("What about 3rd semester?", _ctx())
    assert p.action == "catalogue"
    assert p.target == "programme_pick"
    assert _contract(p).get("programme") is None


# ---------------------------------------------------------------------------
# I — protected student services keep their auth gate
# ---------------------------------------------------------------------------


def test_student_service_auth_preserved():
    ctx = _ctx(programme="mca", programme_id="mca")
    p = _plan("Show my results.", ctx)
    assert p.action == "student_service"
    assert p.target == "results"


# ---------------------------------------------------------------------------
# L — a new semester in the message overrides the stored semester
# ---------------------------------------------------------------------------


def test_semester_override_in_message():
    ctx = _ctx(programme="mca", programme_id="mca", semester="2")
    p = _plan("What about 4th semester?", ctx)
    c = _contract(p)
    assert c.get("semester") == 4
    assert c.get("programme") == "mca"


# ---------------------------------------------------------------------------
# Engine wiring — the resolver runs before planning on the real turn path
# ---------------------------------------------------------------------------


def test_engine_wires_exam_followup():
    import asyncio

    from app.orchestrator.state import ConversationState

    _seed()
    db = SessionLocal()
    try:
        from app.orchestrator.engine import _process

        state = ConversationState(chat_id=str(uuid.uuid4()))
        state.context = _ctx(programme="mca", programme_id="mca", semester="3")

        async def _ask(msg):
            events = []
            async for ev in _process(db, str(uuid.uuid4()), msg, state.chat_id, state):
                events.append(ev)
            return events

        evs = asyncio.run(_ask("When are those exams?"))
        types = [e.get("type") for e in evs]
        assert "date_sheet_schedule" in types or "notice_list" in types, types
        assert "token" not in types, types
        notice = next(e for e in evs if e.get("type") in ("date_sheet_schedule", "notice_list"))
        blob = str(notice.get("message") or notice.get("title") or "").lower()
        assert "mca" in blob and "semester 3" in blob, blob
    finally:
        db.close()
