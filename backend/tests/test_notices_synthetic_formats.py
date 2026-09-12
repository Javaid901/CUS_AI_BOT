"""
P8 battery — synthetic single-sheet PDFs for the CUS formats we have no real
file for (C UG 4th NEP, D UG 6th NEP, F PG Backlog, G older UG 2nd), plus the
zero-fabrication guarantees they must keep.

The fixtures are generated at test time (not committed binaries) and every
layout is labeled ``(SYNTHETIC FORMAT C - SINGLE PROGRAMME TIME TABLE)`` etc.
Assertions pin DETERMINISTIC behaviour: doc-level times applied verbatim,
stacked/continuation subjects split into rows, footer/noise blocks never
turn into schedule rows, and missing facts stay None + flagged — never guessed.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.notices.parser import parse_date_sheet
from app.utils.files import extract_pages_with_tables

_COPY_TO_BLOCK = [
    "Copy to:",
    "1. Dean College Development Cell",
    "2. Principal, Govt. College for Women, Maulana Azad Road",
    "3. Office File",
]


def _make_pdf(path: Path, lines: list[str]) -> None:
    import fitz

    doc = fitz.open()
    page = doc.new_page(width=595, height=842)  # A4 portrait
    y = 70.0
    for ln in lines:
        page.insert_text((60.0, y), ln, fontsize=11, fontname="helv")
        y += 20.0
    doc.save(str(path))
    doc.close()


def _parse(path: Path):
    pages = extract_pages_with_tables(str(path), "pdf")
    return parse_date_sheet(pages)


@pytest.fixture(scope="module")
def c_fixture(tmp_path_factory):
    p = tmp_path_factory.mktemp("synthetic") / "format_c_ug4_nep.pdf"
    _make_pdf(p, [
        "UNIVERSITY EXAMINATION, DECEMBER 2026",
        "(SYNTHETIC FORMAT C - SINGLE PROGRAMME TIME TABLE)",
        "Bachelor of Arts Semester 4th (NEP-2024)",
        "Date Sheet",
        "Examination Time: 02:00 PM to 05:00 PM",
        "13-01-2027 | Plays of Shakespeare | BA401",
        "14-01-2027 | Literary Criticism | BA402",
        "15-01-2027 | Comparative Literature | BA403",
        *_COPY_TO_BLOCK,
    ])
    return p


@pytest.fixture(scope="module")
def d_fixture(tmp_path_factory):
    p = tmp_path_factory.mktemp("synthetic") / "format_d_ug6_nep.pdf"
    _make_pdf(p, [
        "UNIVERSITY EXAMINATION, JUNE 2026",
        "(SYNTHETIC FORMAT D - UG 6TH NEP STACKED SUBJECTS)",
        "Bachelor of Science Semester 6th (NEP-2024)",
        "Time Table",
        "13-01-2026 | Genetics | BSC601 | 10:00 AM - 01:00 PM",
        "14-01-2026 | Microbiology | BSC602 | 10:00 AM - 01:00 PM",
        "16-01-2026 | Principles of Plant Physiology | 10:00 AM - 01:00 PM",
        "Plant Molecular Biology",
    ])
    return p


@pytest.fixture(scope="module")
def f_fixture(tmp_path_factory):
    p = tmp_path_factory.mktemp("synthetic") / "format_f_pg_backlog.pdf"
    _make_pdf(p, [
        "UNIVERSITY EXAMINATION, JUNE 2026",
        "(SYNTHETIC FORMAT F - PG BACKLOG / REPEATER SHEET)",
        "MBA 2nd Semester (Backlog) Batch 2025",
        "Date Sheet",
        "Theory papers for repeaters (backlog papers listed below):",
        "13-06-2026 | Strategic Management | MBA201 | 01:00 PM - 04:00 PM",
        "15-06-2026 | Business Research Methods | MBA202",
        "20-06-2026 | Organizational Behaviour | MBA203",
        "2nd Classwork for Next Day",
        *_COPY_TO_BLOCK,
    ])
    return p


@pytest.fixture(scope="module")
def g_fixture(tmp_path_factory):
    p = tmp_path_factory.mktemp("synthetic") / "format_g_older_ug2.pdf"
    _make_pdf(p, [
        "UNIVERSITY EXAMINATION, MARCH 2026",
        "(SYNTHETIC FORMAT G - 2ND YEAR ANNUAL SHEET)",
        "Bachelor of Commerce Semester 2nd (Annual System)",
        "Date Sheet",
        "13-03-2026 | Tuesday | Financial Accounting | BCOM201",
        "20-03-2026 | Tuesday | Business Law | BCOM202",
        *_COPY_TO_BLOCK,
        "25-03-2026 | Tuesday | Company Law | BCOM203",
    ])
    return p


def test_format_c_doc_level_time_and_noise_ignored(c_fixture):
    res = _parse(c_fixture)
    assert res.extraction_status == "pending_verification"
    assert res.notice_type == "date_sheet"
    assert (res.programme_ids or []) == ["ba"]
    assert len(res.rows) == 3
    expected = [
        ("2027-01-13", "Plays of Shakespeare"),
        ("2027-01-14", "Literary Criticism"),
        ("2027-01-15", "Comparative Literature"),
    ]
    assert [(r["exam_date"], r["subject"]) for r in res.rows] == expected
    for r in res.rows:
        assert r["programme_id"] == "ba"
        assert r["semester"] == "4"
        assert r["start_time"] == "14:00"
        assert r["end_time"] == "17:00"
        assert "missing_time" not in r["validation_flags"]
    assert "december 2026" in str(res.exam_session_label).lower()


def test_format_d_inline_subject_and_continuation_split(d_fixture):
    res = _parse(d_fixture)
    assert len(res.rows) == 4
    assert [(r["exam_date"], r["subject"]) for r in res.rows] == [
        ("2026-01-13", "Genetics"),
        ("2026-01-14", "Microbiology"),
        ("2026-01-16", "Principles of Plant Physiology"),
        ("2026-01-16", "Plant Molecular Biology"),
    ]
    assert res.rows[2]["start_time"] == "10:00"
    assert res.rows[2]["end_time"] == "13:00"
    # The continuation line carries no clock time — the parser must NOT invent
    # one: it stays None and is flagged for admin review.
    assert res.rows[3]["start_time"] is None
    assert res.rows[3]["end_time"] is None
    assert "missing_time" in res.rows[3]["validation_flags"]


def test_format_f_backlog_rows_and_batch(f_fixture):
    res = _parse(f_fixture)
    assert len(res.rows) == 3
    assert [r["subject"] for r in res.rows] == [
        "Strategic Management",
        "Business Research Methods",
        "Organizational Behaviour",
    ]
    assert all(r["programme_id"] == "mba" for r in res.rows)
    assert all(r["semester"] == "2" for r in res.rows)
    assert all(r["batch"] == "2025" for r in res.rows)
    assert res.rows[0]["start_time"] == "13:00"
    assert res.rows[0]["end_time"] == "16:00"
    # Rows without any time are never timed by guessing.
    for r in res.rows[1:]:
        assert r["start_time"] is None
        assert r["end_time"] is None
        assert "missing_time" in r["validation_flags"]


def test_format_g_annual_rows_between_noise_lines(g_fixture):
    res = _parse(g_fixture)
    assert res.exam_type == "annual"
    assert "bcom" in (res.programme_ids or [])
    assert len(res.rows) == 3
    assert [r["subject"] for r in res.rows] == [
        "Financial Accounting",
        "Business Law",
        "Company Law",
    ]
    for r in res.rows:
        assert r["programme_id"] == "bcom"
        assert r["exam_date"]
        assert r["start_time"] is None
        assert "missing_time" in r["validation_flags"]


def test_synthetic_formats_are_deterministic(c_fixture):
    first = _parse(c_fixture)
    second = _parse(c_fixture)
    assert len(first.rows) == len(second.rows)
    for a, b in zip(first.rows, second.rows):
        assert (a["exam_date"], a["subject"], a["start_time"], a["end_time"]) == (
            b["exam_date"], b["subject"], b["start_time"], b["end_time"])