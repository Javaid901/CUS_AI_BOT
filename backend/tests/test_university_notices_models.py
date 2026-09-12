"""
P1 — UniversityNotice + DateSheetEntry models and table registration.

Confirms the new tables are created during create_all, the ORM relationship
(notice -> entries, delete-orphan) behaves, and the schedule fields round-trip.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import inspect

from app.database import SessionLocal, create_all, engine
from app.models import DateSheetEntry, UniversityNotice

create_all()


@pytest.fixture(scope="module", autouse=True)
def _cleanup():
    yield
    db = SessionLocal()
    try:
        db.query(DateSheetEntry).delete()
        db.query(UniversityNotice).delete()
        db.commit()
    finally:
        db.close()


def test_tables_created():
    """create_all must create the two new tables with the expected columns."""
    names = set(inspect(engine).get_table_names())
    assert "university_notices" in names
    assert "date_sheet_entries" in names
    entry_cols = {c["name"] for c in inspect(engine).get_columns("date_sheet_entries")}
    for col in ("exam_date", "day", "start_time", "end_time", "subject",
                "paper_code", "venue", "programme_id", "stream", "semester",
                "batch", "extraction_status", "validation_flags", "raw"):
        assert col in entry_cols


def test_notice_and_entries_roundtrip():
    db = SessionLocal()
    try:
        notice = UniversityNotice(
            id=uuid.uuid4(),
            title="BCA Semester-III Theory Examination, June 2026",
            notice_type="date_sheet",
            filename="bca_s3_june2026.pdf",
            original_filename="Date_Sheet_BCA_3rd_Sem.pdf",
            file_type="pdf",
            file_path="/tmp/notices/bca_s3_june2026.pdf",
            programme_ids='["bca"]',
            extraction_status="pending_verification",
        )
        db.add(notice)
        db.flush()
        entry = DateSheetEntry(
            notice_id=notice.id,
            row_no=1,
            programme_id="bca",
            semester="3",
            exam_date="2026-06-10",
            day="Wednesday",
            start_time="10:00",
            end_time="13:00",
            subject="Data Structures",
            paper_code="BCA301",
            raw="10-06-2026 | Data Structures | BCA301 | 10:00 AM - 01:00 PM",
            extraction_status="pending_verification",
        )
        db.add(entry)
        db.commit()

        got = db.query(UniversityNotice).filter(UniversityNotice.id == notice.id).one()
        assert got.notice_type == "date_sheet"
        assert len(got.entries) == 1
        assert got.entries[0].paper_code == "BCA301"
        assert got.entries[0].notice_id == notice.id

        db.delete(got)
        db.commit()
        assert db.query(DateSheetEntry).filter(DateSheetEntry.notice_id == notice.id).count() == 0
    finally:
        db.close()