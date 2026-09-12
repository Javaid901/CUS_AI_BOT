"""
P8 battery — regression suite over the REAL CUS date-sheet PDFs (formats A/B/E)
plus planner discipline-alias routing for verified schedule queries.

The parser must reproduce the ground-truth facts verbatim from each official
PDF: programme columns, dates, times, subjects and batch — never synthesized.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.notices.parser import parse_date_sheet
from app.orchestrator.planner import (
    _detect_notice_intent,
    _detect_programme_discipline,
)
from app.utils.files import extract_pages_with_tables

_FIXTURES = Path(__file__).resolve().parents[1] / "app" / "data" / "notices"

PG = _FIXTURES / "ea5f05edd467_pg1stsemregularbatch2025.pdf"
BED_3RD = _FIXTURES / "0de6275f0c43_b_ed_3rdsemster2025aug2026.pdf"
BED_SUPP = _FIXTURES / "41c38a2cc3cf_b_ed_2ndsemsterbatch2025supp.pdf"

AI_ML_PAPERS = [
    ("2026-03-30", "Computational Mathematics"),
    ("2026-04-02", "Advanced Database Management Systems"),
    ("2026-04-06", "Artificial Intelligence Fundamentals"),
    ("2026-04-09", "Data Structure and Algorithms"),
    ("2026-04-13", "Problem Solving using Python"),
]

BA_PAPERS = [
    ("2026-03-30", "Business Accounting"),
    ("2026-04-02", "Business Statistics"),
    ("2026-04-04", "Ethics and Corporate Governance"),
    ("2026-04-06", "Computer Applications for Business"),
    ("2026-04-09", "Principles of Management & Organizational Behavior"),
    ("2026-04-13", "Managerial Economics"),
]


def _rows(path: Path):
    pages = extract_pages_with_tables(str(path), "pdf")
    return parse_date_sheet(pages)


def test_pg_real_pdf_two_table_grid_fidelity():
    res = _rows(PG)
    assert res.notice_type == "date_sheet"
    assert res.exam_type == "semester"
    assert len(res.rows) == 82
    assert "pg" in (res.programme_ids or [])
    for pid in ("ai-ml", "data-science", "physics", "bio-chemistry",
                "computer-applications", "business-administration", "education"):
        assert pid in (res.programme_ids or []), pid


def test_pg_real_pdf_ai_ml_papers_exact():
    res = _rows(PG)
    ai = sorted(
        ((r["exam_date"], r["subject"]) for r in res.rows if r["programme_id"] == "ai-ml")
    )
    assert ai == AI_ML_PAPERS
    for r in res.rows:
        if r["programme_id"] == "ai-ml":
            assert r["semester"] == "1"
            assert r["batch"] == "2025"
            assert r["start_time"] == "13:00"


def test_pg_real_pdf_business_administration_column():
    res = _rows(PG)
    ba = sorted(
        ((r["exam_date"], r["subject"]) for r in res.rows
         if r["programme_id"] == "business-administration")
    )
    assert ba == BA_PAPERS


def test_pg_real_pdf_data_science_cells_not_truncated():
    res = _rows(PG)
    ds = {r["subject"] for r in res.rows if r["programme_id"] == "data-science"}
    assert "Data Science Fundamentals" in ds
    assert "Data Structure and Algorithms" in ds


def test_bed_3rd_real_pdf_row_bands():
    res = _rows(BED_3RD)
    assert res.exam_type == "semester"
    assert (res.programme_ids or []) == ["bed"]
    assert len(res.rows) == 11
    assert res.rows[0]["subject"] == "School Management"
    assert res.rows[0]["semester"] == "3"
    same_day = [r["subject"] for r in res.rows if r["exam_date"] == "2026-08-24"]
    assert same_day == ["Measurement Evaluation and Assessment",
                        "Educational Measurement and Evaluation"]
    teaching = [r["subject"] for r in res.rows if r["exam_date"] == "2026-09-02"]
    assert teaching == ["Teaching of Bio-Science", "Teaching of History & Civics",
                        "Teaching of Mathematics", "Teaching of Geography"]


def test_bed_supp_real_pdf_inline_subject_split():
    res = _rows(BED_SUPP)
    assert res.exam_type == "supplementary"
    assert (res.programme_ids or []) == ["bed"]
    assert len(res.rows) == 6
    assert all(r["semester"] == "2" for r in res.rows)
    tail = [(r["exam_date"], r["subject"]) for r in res.rows[-2:]]
    assert tail == [("2026-09-18", "Teaching of English"), ("2026-09-18", "Teaching of Hindi")]
    assert {r["start_time"] for r in res.rows} == {"10:30"}


def test_programme_discipline_alias_map():
    assert _detect_programme_discipline("AI and ML PG 1st semester date sheet") == "ai-ml"
    assert _detect_programme_discipline("data science pg first sem timetable") == "data-science"
    assert _detect_programme_discipline("MSc Physics semester 1 date sheet") == "physics"
    assert _detect_programme_discipline("business administration pg date sheet") == "business-administration"
    assert _detect_programme_discipline("computer applications date sheet") == "computer-applications"
    assert _detect_programme_discipline("BEd 3rd semester date sheet") is None


def test_notice_intent_discipline_overrides_degree_family():
    e = SimpleNamespace(programmes=["pg"], programme=None, semester=1)
    intent = _detect_notice_intent("AI and ML PG 1st semester date sheet", e)
    assert intent["mode"] == "schedule"
    assert intent["programme"] == "ai-ml"
    assert intent["semester"] == 1


def test_notice_intent_no_discipline_keeps_degree_programme():
    e = SimpleNamespace(programmes=["bed"], programme=None, semester=3)
    intent = _detect_notice_intent("BEd 3rd semester date sheet", e)
    assert intent["mode"] == "schedule"
    assert intent["programme"] == "bed"
    assert intent["semester"] == 3


def test_notice_intent_schedule_requires_semester():
    e = SimpleNamespace(programmes=["pg"], programme=None, semester=None)
    intent = _detect_notice_intent("physics date sheet", e)
    assert intent["mode"] == "notice_list"
    assert intent["programme"] == "physics"


def test_notice_intent_unrelated_stays_none():
    assert _detect_notice_intent("tell me about CUS admissions", SimpleNamespace()) is None