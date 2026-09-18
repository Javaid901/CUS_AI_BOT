"""
backend/app/examination/metadata.py

Deterministic model-paper metadata derivation.

Production ``doc_meta`` does not usually carry ``subject`` / ``programme`` /
``semester`` / ``batch`` / ``academic_year`` (Phase 1 of the crawler writes
only content-level keys). This module derives those fields *deterministically*
from the paper's title / URL / stored filename so the Model Papers service can
honestly filter by programme, semester, subject, batch and academic year.

Authority order:
  1. ``doc_meta`` — the crawler-sourced value, when present (always wins),
  2. derived-from-text — title + URL + raw filename,
  3. absent — the field stays None and matches nothing for that constraint.

No fabrication: an unrecognized subject/word is left as None rather than
guessed. Detection is case- and punctuation-insensitive; titles are matched
on whole words so "Zoology Model Paper" is not confused by "Cyber Zoology".
"""

from __future__ import annotations

import re
from typing import Any

from app.orchestrator.context import PROGRAMME_ALIASES

# ---------------------------------------------------------------------------
# Subjects — every known canonical label + its aliases (all lowercased)
# ---------------------------------------------------------------------------

_SUBJECT_ALIASES: dict[str, str] = {
    "zoology": "Zoology",
    "botany": "Botany",
    "chemistry": "Chemistry",
    "physics": "Physics",
    "mathematics": "Mathematics",
    "math": "Mathematics",
    "maths": "Mathematics",
    "statistics": "Statistics",
    "computer science": "Computer Science",
    "computer applications": "Computer Applications",
    "information technology": "Information Technology",
    "information science": "Information Science",
    "english": "English",
    "hindi": "Hindi",
    "sanskrit": "Sanskrit",
    "kannada": "Kannada",
    "marathi": "Marathi",
    "economics": "Economics",
    "commerce": "Commerce",
    "accountancy": "Accountancy",
    "accounting": "Accounting",
    "business administration": "Business Administration",
    "business studies": "Business Studies",
    "management": "Management",
    "finance": "Finance",
    "banking": "Banking",
    "insurance": "Insurance",
    "history": "History",
    "geography": "Geography",
    "political science": "Political Science",
    "sociology": "Sociology",
    "psychology": "Psychology",
    "philosophy": "Philosophy",
    "journalism": "Journalism",
    "mass communication": "Mass Communication",
    "education": "Education",
    "physical education": "Physical Education",
    "nursing": "Nursing",
    "pharmacy": "Pharmacy",
    "agriculture": "Agriculture",
    "horticulture": "Horticulture",
    "forestry": "Forestry",
    "biotechnology": "Biotechnology",
    "microbiology": "Microbiology",
    "biochemistry": "Biochemistry",
    "genetics": "Genetics",
    "electronics": "Electronics",
    "electrical": "Electrical",
    "mechanical": "Mechanical",
    "civil": "Civil",
    "accounting taxation": "Accounting",
}

# Longest phrase first so "computer science" wins over "computer".
_SUBJECT_PATTERN = re.compile(
    r"\b(" + "|".join(
        re.escape(a) for a in sorted(_SUBJECT_ALIASES, key=len, reverse=True)
    ) + r")\b",
    re.IGNORECASE,
)


def normalize_subject(text: str | None) -> str | None:
    """Return the canonical subject label found in ``text``, or None."""
    if not text:
        return None
    low = text.lower()
    m = _SUBJECT_PATTERN.search(low)
    if not m:
        return None
    return _SUBJECT_ALIASES[m.group(1).lower()]


# ---------------------------------------------------------------------------
# Semester
# ---------------------------------------------------------------------------

_SEMESTER_PATTERNS = (
    re.compile(r"\b([0-9]{1,2})\s*(?:st|nd|rd|th)\s*sem(?:ester)?\b"),
    re.compile(r"\bsem(?:ester)?\s*[#:-]?\s*([0-9]{1,2})\b"),
    re.compile(r"\b([0-9]{1,2})\s*(?:st|nd|rd|th)\s*sem\b"),
    re.compile(r"\bsem(?:ester)?\s*of\s*([0-9]{1,2})\b"),
)


def normalize_semester(text: str | None) -> int | None:
    """Extract a semester number (1-10) from text, or None."""
    if not text:
        return None
    low = text.lower()
    for pattern in _SEMESTER_PATTERNS:
        m = pattern.search(low)
        if m:
            try:
                sem = int(m.group(1))
            except (ValueError, IndexError):
                continue
            if 1 <= sem <= 10:
                return sem
    # Fallback: a bare first-semester word ("first semester", "2nd semester")
    m = re.search(r"\b(first|1st|2nd|second|3rd|third|4th|fourth|5th|fifth|"
                  r"6th|sixth|7th|seventh|8th|eighth|9th|ninth|10th|tenth)\s+sem(?:ester)?\b",
                  low)
    if m:
        words = {
            "first": 1, "1st": 1, "2nd": 2, "second": 2, "3rd": 3, "third": 3,
            "4th": 4, "fourth": 4, "5th": 5, "fifth": 5, "6th": 6, "sixth": 6,
            "7th": 7, "seventh": 7, "8th": 8, "eighth": 8, "9th": 9, "ninth": 9,
            "10th": 10, "tenth": 10,
        }
        return words.get(m.group(1).lower())
    return None


# ---------------------------------------------------------------------------
# Batch / academic year
# ---------------------------------------------------------------------------

_BATCH_PATTERN = re.compile(r"\b(?:batch|admission\s+batch)\s*[:#-]?\s*([0-9]{4})\b")
_YEAR_PATTERN = re.compile(r"\b(20\d{2})\b")


def extract_batch(text: str | None) -> str | None:
    """Extract an explicit batch year like ``2025``, or None."""
    if not text:
        return None
    m = _BATCH_PATTERN.search(text.lower())
    if m:
        return m.group(1)
    return None


def extract_academic_year(text: str | None) -> str | None:
    """Extract the first four-digit academic year (>=2000), or None.

    Confined to adjacency with year-like words so a bare ISBN/serial number
    is not misread as an academic year.
    """
    if not text:
        return None
    for m in _YEAR_PATTERN.finditer(text):
        year = m.group(1)
        if year < "2000":
            continue
        before = text[max(0, m.start() - 18):m.start()].lower()
        after = text[m.end():m.end() + 18].lower()
        if any(
            token in before or token in after
            for token in ("year", "session", "academic", "batch", "cohort", "class of", "admission")
        ):
            return year
    return None


# ---------------------------------------------------------------------------
# Programme
# ---------------------------------------------------------------------------

_PROGRAMME_PATTERN = re.compile(
    r"\b(" + "|".join(
        re.escape(a) for a in sorted(PROGRAMME_ALIASES, key=len, reverse=True)
    ) + r")\b",
    re.IGNORECASE,
)


def extract_programme(text: str | None) -> str | None:
    """Return the canonical programme ID found in ``text``, or None."""
    if not text:
        return None
    m = _PROGRAMME_PATTERN.search(text.lower())
    if not m:
        return None
    return PROGRAMME_ALIASES.get(m.group(1).lower())


# ---------------------------------------------------------------------------
# Row enrichment
# ---------------------------------------------------------------------------

def _text_source(url: str, title: str, raw_path: str) -> str:
    """Concatenate the fields that describe a model-paper row on disk."""
    url_tail = ""
    if url:
        url_tail = url.rstrip("/").split("/")[-1].replace("_", " ").replace("-", " ")
    parts = [t for t in (title, url_tail, raw_path) if t]
    return " ".join(parts)


def enrich_paper(d: dict[str, Any], url: str = "", title: str = "", raw_path: str = "") -> dict[str, Any]:
    """Fill any missing metadata fields deterministically from text.

    ``doc_meta`` values (already present on ``d``) always win; only fields
    that are absent get derived from the title/URL/raw-filename text.
    """
    source = _text_source(url, title, raw_path)
    out = dict(d)
    if not (out.get("programme") or "").strip():
        prog = extract_programme(source)
        if prog:
            out["programme"] = prog
    if out.get("semester") in (None, ""):
        sem = normalize_semester(source)
        if sem is not None:
            out["semester"] = str(sem)
    if not (out.get("subject") or "").strip():
        subject = normalize_subject(source)
        if subject:
            out["subject"] = subject
    if not (out.get("batch") or "").strip():
        batch = extract_batch(source)
        if batch:
            out["batch"] = batch
    if not (out.get("academic_year") or "").strip():
        year = extract_academic_year(source)
        if year:
            out["academic_year"] = year
    return out