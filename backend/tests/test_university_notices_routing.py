"""
P6 — University-notices / date-sheet chatbot routing.

Covers:
  1. Planner Rule 3a: date-sheet vocabulary -> `university_notices` (schedule
     mode only with programme + explicit semester); control flows (news,
     greeting) are untouched.
  2. Engine handler: VERIFIED + PUBLISHED rows only — unverified, unpublished
     and other-programme rows never leak into a schedule or notice list; the
     no-verified-data fallback is an honest message, never a fabricated table.
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from app.database import SessionLocal, create_all
from app.models import DateSheetEntry, UniversityNotice
from app.orchestrator.context import ConversationContext
from app.orchestrator.engine import _handle_university_notices
from app.orchestrator.extractor import extract_entities
from app.orchestrator.planner import plan
from app.orchestrator.state import ConversationState

create_all()


def _j(values: list[str]) -> str:
    return json.dumps(values)


@pytest.fixture(scope="module", autouse=True)
def _seed():
    """Seed the shared dataset ONCE (a fresh session is opened per test).

    - BCA s4: VERIFIED + PUBLISHED (the only row that may surface for bca).
    - BOGUS BCA s4: UNVERIFIED/UNPUBLISHED (must NEVER surface).
    - MCA s4: VERIFIED + PUBLISHED (must NEVER leak into bca queries).
    """
    db = SessionLocal()
    try:
        bca = UniversityNotice(
            id=uuid.uuid4(),
            title="BCA Semester-IV Date Sheet, June 2026",
            notice_type="date_sheet",
            filename="bca_s4.pdf",
            original_filename="DS_BCA_4.pdf",
            file_type="pdf",
            file_path="notices/bca_s4.pdf",
            programme_ids=_j(["bca"]),
            extraction_status="verified",
            is_verified=True,
            is_published=True,
        )
        db.add(bca)
        db.flush()
        db.add(DateSheetEntry(
            notice_id=bca.id, row_no=1, programme_id="bca", semester="4",
            exam_date="2026-06-12", day="Thursday", start_time="10:00", end_time="13:00",
            subject="Data Structures", paper_code="BCA401", venue="Room 5",
            extraction_status="verified",
        ))
        ghost = UniversityNotice(
            id=uuid.uuid4(),
            title="BOGUS BCA Semester-IV Date Sheet",
            notice_type="date_sheet",
            filename="bogus.pdf",
            original_filename="Bogus.pdf",
            file_type="pdf",
            file_path="notices/bogus.pdf",
            programme_ids=_j(["bca"]),
            extraction_status="pending_verification",
            is_verified=False,
            is_published=False,
        )
        db.add(ghost)
        db.flush()
        db.add(DateSheetEntry(
            notice_id=ghost.id, row_no=1, programme_id="bca", semester="4",
            exam_date="2026-07-01", day="Monday", start_time="09:00", end_time="12:00",
            subject="Imaginary", paper_code="BCA4XX", extraction_status="pending_verification",
        ))
        mca = UniversityNotice(
            id=uuid.uuid4(),
            title="MCA Semester-IV Date Sheet, June 2026",
            notice_type="date_sheet",
            filename="mca_s4.pdf",
            original_filename="DS_MCA_4.pdf",
            file_type="pdf",
            file_path="notices/mca_s4.pdf",
            programme_ids=_j(["mca"]),
            extraction_status="verified",
            is_verified=True,
            is_published=True,
        )
        db.add(mca)
        db.flush()
        db.add(DateSheetEntry(
            notice_id=mca.id, row_no=1, programme_id="mca", semester="4",
            exam_date="2026-06-15", day="Sunday", start_time="14:00", end_time="17:00",
            subject="DBMS", paper_code="MCA401", venue="Room 2",
            extraction_status="verified",
        ))
        db.commit()
        yield
    finally:
        db.query(DateSheetEntry).delete()
        db.query(UniversityNotice).delete()
        db.commit()
        db.close()


def _route(raw: str):
    ctx = ConversationContext()
    e = extract_entities(raw)
    return plan(raw, ctx, "rt-" + uuid.uuid4().hex[:8], e)


def _intent(mode, prog, sem, stream=None):
    return {
        "mode": mode, "programme": prog, "semester": sem,
        "stream": stream, "query": "test", "confidence": 0.9, "reason": "x",
    }


def _drain(db, intent):
    async def _run():
        events = []
        async for ev in _handle_university_notices(
            db, "eng-chat", ConversationState(chat_id="eng-chat"), intent,
        ):
            events.append(ev)
        return events
    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# 1. Planner routing (Rule 3a)
# ---------------------------------------------------------------------------

def test_planner_routes_date_sheet_to_university_notices():
    for raw in (
        "bca 4th sem date sheet",
        "bca datesheet",
        "date sheet",
        "btech cse 4th semester exam schedule",
        "when is the MCA time table out",
        "exam timetable 2026",
    ):
        p = _route(raw)
        assert p.action == "university_notices", f"{raw!r} -> {p.action}"
        assert p.extra.get("mode") in ("notice_list", "schedule")
        assert p.extra.get("query")


def test_planner_schedule_mode_requires_programme_and_semester():
    p = _route("bca 4th sem date sheet")
    assert p.extra.get("mode") == "schedule"
    assert p.extra.get("programme") == "bca"
    assert p.extra.get("semester") == 4

    p = _route("bca datesheet")
    assert p.extra.get("mode") == "notice_list"
    assert p.extra.get("programme") == "bca"
    assert p.extra.get("semester") is None

    p = _route("date sheet")
    assert p.extra.get("mode") == "notice_list"
    assert p.extra.get("programme") is None


def test_planner_detects_stream():
    p = _route("btech cse 4th semester exam schedule")
    assert p.extra.get("stream") == "cse"
    assert p.extra.get("programme") == "btech"
    assert p.extra.get("mode") == "schedule"


def test_planner_control_flows_untouched():
    assert _route("hello").action == "greeting"
    assert _route("latest notices").action == "news"
    # Bare navigation labels keep their existing option-button flow.
    assert _route("notices").action == "navigation"
    assert _route("circular for holidays").action == "news"


# ---------------------------------------------------------------------------
# 2. Engine handler — zero-hallucination gate
# ---------------------------------------------------------------------------

def test_schedule_shows_only_verified_published_rows():
    db = SessionLocal()
    try:
        events = _drain(db, _intent("schedule", "bca", 4))
        assert [e["type"] for e in events] == ["date_sheet_schedule", "done"]
        payload = events[0]
        assert payload["programme"] == "bca"
        assert payload["semester"] == 4
        assert len(payload["schedule"]) == 1
        row = payload["schedule"][0]
        assert row["subject"] == "Data Structures"
        assert row["paper_code"] == "BCA401"
        assert row["exam_date"] == "2026-06-12"
        # The OFFICIAL document is served for the verified notice only.
        assert all(d["file_url"].startswith("/api/notices/") for d in payload["documents"])
        assert "Imaginary" not in str(payload)
        assert "BOGUS" not in str(payload)
    finally:
        db.close()


def test_schedule_never_leaks_other_programme_rows():
    db = SessionLocal()
    try:
        events = _drain(db, _intent("schedule", "bca", 4))
        payload = events[0]
        rows = payload["schedule"]
        assert len(rows) == 1
        assert rows[0]["programme_id"] == "bca"
        assert rows[0]["subject"] == "Data Structures"
        assert all(r["programme_id"] == "bca" for r in rows)
    finally:
        db.close()


def test_no_verified_schedule_falls_back_to_honest_message():
    db = SessionLocal()
    try:
        events = _drain(db, _intent("schedule", "mca", 1))
        assert [e["type"] for e in events] == ["notice_list", "done"]
        msg = events[0]["message"]
        assert "No verified structured schedule is available yet for MCA semester 1" in msg
        # Documents shown are still only verified + published, no ghosts.
        titles = {c["title"] for c in events[0]["notices"]}
        assert "MCA Semester-IV Date Sheet, June 2026" in titles
        assert "BOGUS" not in str(titles)
        assert "Imaginary" not in str(titles)
    finally:
        db.close()


def test_notice_list_mode_serves_published_cards_only():
    db = SessionLocal()
    try:
        events = _drain(db, _intent("notice_list", "bca", None))
        assert [e["type"] for e in events] == ["notice_list", "done"]
        cards = events[0]["notices"]
        titles = {c["title"] for c in cards}
        assert "BCA Semester-IV Date Sheet, June 2026" in titles
        assert "BOGUS" not in str(titles)
        assert all(c["file_url"].startswith("/api/notices/") for c in cards)
    finally:
        db.close()
