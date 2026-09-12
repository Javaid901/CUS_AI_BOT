"""
P6b — Date-sheet chatbot behavior on the public University Notices pipeline.

Locks the browser-visible contract with the new system:

  CASE 1  "datesheet" (bare)  -> notice_list of published date-sheet docs,
                                 NO "which programme?" slot-fill picker and NO
                                 hardcoded programme buttons.
  CASE 2  "BCA datesheet"     -> only that programme's verified published
                                 notices.
  CASE 3  "BCA 3rd sem datesheet" -> date_sheet_schedule rows for BCA sem 3
                                 (Date|Day|Time|Subject|Paper Code), never the
                                 whole PDF and never another programme.
  CASE 4  "3rd sem datesheet" -> the WHOLE matching semester sheet: schedule
                                 rows across programmes for that semester, no
                                 programme forced, no "which programme?".
  CASE 5  programme+semester+batch ("MCA 3rd sem 2024 batch datesheet")
                                 -> schedule filtered by verified batch.
  CASE 6  "AI and ML PG 1st semester date sheet" -> schedule for ai-ml sem 1.

Regression: none of these surfaces an "options" picker and the old programme
picker (built from _build_slot_fill_question) never renders for date-sheet
vocabulary.
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
    """Published seed: BCA (sem 3 + 4) and MCA (sem 4 + sem 3 in two batches).

    A ghost (pending_verification) notice must NEVER surface anywhere.
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
        ghost = UniversityNotice(
            id=uuid.uuid4(),
            title="BOGUS Semester Date Sheet",
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
        db.add_all([bca, mca, ghost])
        db.flush()
        db.add_all([
            DateSheetEntry(
                notice_id=bca.id, row_no=1, programme_id="bca", semester="3",
                exam_date="2026-05-20", day="Wednesday", start_time="10:00",
                end_time="13:00", subject="Data Structures II",
                paper_code="BCA301", extraction_status="verified",
            ),
            DateSheetEntry(
                notice_id=bca.id, row_no=2, programme_id="bca", semester="4",
                exam_date="2026-06-12", day="Thursday", start_time="10:00",
                end_time="13:00", subject="Data Structures",
                paper_code="BCA401", extraction_status="verified",
            ),
            DateSheetEntry(
                notice_id=mca.id, row_no=1, programme_id="mca", semester="3",
                exam_date="2026-05-21", day="Thursday", start_time="14:00",
                end_time="17:00", subject="Advanced DBMS", paper_code="MCA301",
                batch="2024", extraction_status="verified",
            ),
            DateSheetEntry(
                notice_id=mca.id, row_no=2, programme_id="mca", semester="3",
                exam_date="2026-05-22", day="Friday", start_time="14:00",
                end_time="17:00", subject="Design Patterns", paper_code="MCA318",
                batch="2023", extraction_status="verified",
            ),
            DateSheetEntry(
                notice_id=mca.id, row_no=3, programme_id="mca", semester="4",
                exam_date="2026-06-15", day="Sunday", start_time="14:00",
                end_time="17:00", subject="DBMS", paper_code="MCA401",
                extraction_status="verified",
            ),
            DateSheetEntry(
                notice_id=ghost.id, row_no=1, programme_id="bca", semester="4",
                exam_date="2026-07-01", day="Monday", start_time="09:00",
                end_time="12:00", subject="Imaginary", paper_code="BCA4XX",
                extraction_status="pending_verification",
            ),
        ])
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
    return plan(raw, ctx, "ds-" + uuid.uuid4().hex[:8], e)


def _intent(mode, prog, sem, stream=None, batch=None):
    return {
        "mode": mode, "programme": prog, "semester": sem,
        "stream": stream, "batch": batch, "query": "test",
        "confidence": 0.9, "reason": "x",
    }


def _drain(db, intent):
    async def _run():
        events = []
        async for ev in _handle_university_notices(
            db, "ds-chat", ConversationState(chat_id="ds-chat"), intent,
        ):
            events.append(ev)
        return events
    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# CASE 1+2 — bare / programme-only datesheet: notice documents, never a picker
# ---------------------------------------------------------------------------

def test_bare_datesheet_never_asks_which_programme():
    for raw in ("datesheet", "datesheet please", "show datesheet", "date sheet"):
        p = _route(raw)
        assert p.action == "university_notices", f"{raw!r} -> {p.action}"
        assert p.extra.get("mode") == "notice_list"
        # The old picker must be unreachable for date-sheet vocabulary.
        assert p.action not in ("slot_fill", "clarify")
        assert p.extra.get("contract", {}).get("clarification_field") is None, raw


def test_bare_datesheet_engine_lists_published_cards():
    db = SessionLocal()
    try:
        events = _drain(db, _intent("notice_list", None, None))
        assert [e["type"] for e in events] == ["notice_list", "done"]
        titles = {c["title"] for c in events[0]["notices"]}
        assert "BCA Semester-IV Date Sheet, June 2026" in titles
        assert "MCA Semester-IV Date Sheet, June 2026" in titles
        assert all("BOGUS" not in c["title"] for c in events[0]["notices"])
        assert [c["file_url"].startswith("/api/notices/") for c in events[0]["notices"]]
    finally:
        db.close()


def test_programme_only_datesheet_filters_that_programme():
    p = _route("bca datesheet")
    assert p.action == "university_notices"
    assert p.extra.get("mode") == "notice_list"
    assert p.extra.get("programme") == "bca"

    db = SessionLocal()
    try:
        events = _drain(db, _intent("notice_list", "bca", None))
        titles = {c["title"] for c in events[0]["notices"]}
        assert "BCA Semester-IV Date Sheet, June 2026" in titles
        assert "MCA Semester-IV Date Sheet, June 2026" not in titles
    finally:
        db.close()


# ---------------------------------------------------------------------------
# CASE 3 — programme + semester: verified schedule rows only
# ---------------------------------------------------------------------------

def test_programme_semester_datesheet_renders_schedule():
    p = _route("bca 3rd sem datesheet")
    assert p.action == "university_notices"
    assert p.extra.get("mode") == "schedule"
    assert p.extra.get("programme") == "bca"
    assert p.extra.get("semester") == 3

    db = SessionLocal()
    try:
        events = _drain(db, _intent("schedule", "bca", 3))
        assert [e["type"] for e in events] == ["date_sheet_schedule", "done"]
        payload = events[0]
        assert payload["programme"] == "bca"
        assert payload["semester"] == 3
        rows = payload["schedule"]
        assert len(rows) == 1
        row = rows[0]
        assert row["subject"] == "Data Structures II"
        assert row["paper_code"] == "BCA301"
        assert row["exam_date"] == "2026-05-20"
        # The verified row carries the paper code & time — never LLM-invented.
        assert all(r["programme_id"] == "bca" for r in rows)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# CASE 4 — semester only: the WHOLE matching semester sheet, no forced programme
# ---------------------------------------------------------------------------

def test_semester_only_datesheet_shows_whole_sheet():
    p = _route("3rd sem datesheet")
    assert p.action == "university_notices"
    assert p.extra.get("mode") == "schedule"
    assert p.extra.get("programme") is None
    assert p.extra.get("semester") == 3
    # No "which programme?" on a bare-semester query either.
    assert p.extra.get("contract", {}).get("clarification_field") is None

    db = SessionLocal()
    try:
        events = _drain(db, _intent("schedule", None, 3))
        assert [e["type"] for e in events] == ["date_sheet_schedule", "done"]
        payload = events[0]
        assert payload["programme"] is None
        assert payload["semester"] == 3
        # Whole sheet: rows from every published notice that has semester 3.
        rows = payload["schedule"]
        assert {r["programme_id"] for r in rows} == {"bca", "mca"}
        assert len(rows) == 3
        docs = {d["id"] for d in payload["documents"]}
        assert len(docs) == 2  # only the sheets that actually contributed rows
        assert all(d["file_url"].startswith("/api/notices/") for d in payload["documents"])
    finally:
        db.close()


# ---------------------------------------------------------------------------
# CASE 5 — programme + semester + explicit batch
# ---------------------------------------------------------------------------

def test_programme_semester_batch_datesheet_filters_batch():
    p = _route("mca 3rd sem 2024 batch datesheet")
    assert p.action == "university_notices"
    assert p.extra.get("mode") == "schedule"
    assert p.extra.get("programme") == "mca"
    assert p.extra.get("semester") == 3
    assert p.extra.get("batch") == "2024"

    db = SessionLocal()
    try:
        events = _drain(db, _intent("schedule", "mca", 3, batch="2024"))
        payload = events[0]
        assert payload["schedule"][0]["subject"] == "Advanced DBMS"
        assert payload["schedule"][0]["batch"] == "2024"
        assert len(payload["schedule"]) == 1
        # The 2023 batch row must never leak in.
        assert "Design Patterns" not in {r["subject"] for r in payload["schedule"]}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# CASE 6 — discipline alias ("AI and ML PG 1st semester date sheet")
# ---------------------------------------------------------------------------

def test_discipline_alias_schedule_intent():
    p = _route("ai and ml pg 1st semester date sheet")
    assert p.action == "university_notices"
    assert p.extra.get("mode") == "schedule"
    assert p.extra.get("programme") == "ai-ml"
    assert p.extra.get("semester") == 1


# ---------------------------------------------------------------------------
# Regression — the old programme picker never renders for date-sheet requests
# ---------------------------------------------------------------------------

def test_old_programme_picker_never_renders_for_date_sheet_requests():
    for raw in (
        "datesheet", "date sheet", "BCA datesheet", "BCA 3rd sem datesheet",
        "3rd sem datesheet", "MCA 3rd sem 2024 batch datesheet", "date-sheet",
    ):
        p = _route(raw)
        assert p.action != "slot_fill", f"{raw!r} reached the old picker"
        assert p.action != "clarify", f"{raw!r} asked a clarification picker"
        assert p.action == "university_notices", f"{raw!r} -> {p.action}"