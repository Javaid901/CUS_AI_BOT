"""
P8 — Deterministic date-sheet parser (no-fabrication audit).

The parser must copy facts verbatim out of source text (normalizing only date
and time formats) and leave anything it cannot locate NULL with a validation
flag, so admin review — never a guess — resolves it.
"""

from __future__ import annotations

from app.notices.parser import parse_date_sheet


def _page(text: str, page: int = 1, section: str | None = None) -> dict:
    return {"page": page, "section": section, "text": text}


def test_single_programme_table_lines():
    res = parse_date_sheet([_page(
        "UNIVERSITY EXAMINATION, JUNE 2026\n"
        "BCA Semester IV\n"
        "Date Sheet\n"
        "12-06-2026 | Thursday | Data Structures | BCA401 | 10:00 AM - 01:00 PM | Exam Hall A\n"
        "13-06-2026 | Friday | Operating Systems | BCA402 | 10:00 AM - 01:00 PM\n"
    )])
    assert res.notice_type == "date_sheet"
    assert "bca" in (res.programme_ids or [])
    assert len(res.rows) == 2

    r1 = res.rows[0]
    assert r1["exam_date"] == "2026-06-12"
    assert r1["day"] == "Thursday"
    assert r1["start_time"] == "10:00"
    assert r1["end_time"] == "13:00"
    assert r1["subject"] == "Data Structures"
    assert r1["paper_code"] == "BCA401"
    assert r1["venue"] == "Exam Hall A"
    assert r1["programme_id"] == "bca"
    assert r1["semester"] == "4"
    # ambiguous_day_month is expected: both 12 and 06 are <= 12, so the
    # parser flags it for human review (Indian day-first convention assumed).
    assert r1["validation_flags"] == ["ambiguous_day_month"]

    r2 = res.rows[1]
    assert r2["exam_date"] == "2026-06-13"
    assert r2["subject"] == "Operating Systems"
    assert r2["paper_code"] == "BCA402"
    assert r2["programme_id"] == "bca"


def test_programme_sections_keep_rows_apart():
    res = parse_date_sheet([_page(
        "Date Sheet\n"
        "BCA Sem IV\n"
        "12-06-2026 Data Structures BCA401 10:00 AM - 01:00 PM\n"
        "13-06-2026 Operating Systems BCA402 10:00 AM - 01:00 PM\n"
        "MCA Sem IV\n"
        "15-06-2026 DBMS MCA401 02:00 PM - 05:00 PM\n"
    )])
    assert len(res.rows) == 3
    assert [(r["programme_id"], r["semester"], r["paper_code"]) for r in res.rows] == [
        ("bca", "4", "BCA401"),
        ("bca", "4", "BCA402"),
        ("mca", "4", "MCA401"),
    ]
    for r in res.rows:
        assert r["exam_date"], r


def test_missing_subject_is_flagged_never_guessed():
    res = parse_date_sheet([_page(
        "BCA Semester IV Date Sheet\n"
        "12-06-2026 | 10:00 AM - 01:00 PM | BCA401\n"  # no subject verbatim
    )])
    assert len(res.rows) == 1
    row = res.rows[0]
    assert row["exam_date"] == "2026-06-12"
    assert row["start_time"] == "10:00"
    assert row["paper_code"] == "BCA401"
    assert row["subject"] is None
    assert "missing_subject" in row["validation_flags"]


def test_ambiguous_day_month_is_flagged_for_admin():
    res = parse_date_sheet([_page(
        "BCA Semester IV Date Sheet\n"
        "06/07/2026 Data Structures BCA401 10:00 AM - 01:00 PM\n"
    )])
    row = res.rows[0]
    assert row["exam_date"] == "2026-07-06"  # day-first convention
    assert "ambiguous_day_month" in row["validation_flags"]


def test_multi_programme_columns_never_guess_programme():
    res = parse_date_sheet([_page(
        "Date Sheet\n"
        "12-06-2026 | BCA401 MCA401 | 10:00 AM - 01:00 PM | Room 5\n"
    )])
    assert len(res.rows) == 1
    row = res.rows[0]
    # Two paper codes -> the row cannot be attributed to a single programme;
    # it is left unassigned for manual resolution rather than guessed.
    assert "multi_column_row" in row["validation_flags"]
    assert row["paper_code"] is None
    assert row["programme_id"] is None


def test_empty_scanned_content_degrades_to_extraction_failed():
    res = parse_date_sheet([_page("", page=None)])
    assert res.extraction_status == "extraction_failed"
    assert "scanned" in res.extraction_error.lower() or "no extractable" in res.extraction_error.lower()
    assert res.rows == []