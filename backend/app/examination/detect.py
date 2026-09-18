"""
backend/app/examination/detect.py

Deterministic intent detection for the Examinations menu's three dedicated
services:

  model_papers           - Model Papers (verified Controller-of-Examinations corpus)
  fee_structure_exam     - Exam Fee Structure (official fee notifications)
  division_improvement   - Division Improvement (official policy documents)

Detection is keyword/phrase gated and runs in the planner BEFORE the
programme-catalogue, news and generic-RAG flows so these intents can never be
swallowed by fee slot-fill, the semantic classifier ("model papers" currently
maps into the examination category -> the menu loop) or the broad RAG
fallback.

Exclusions (spec):
  - PYQ / previous-year / past papers / question-paper pattern / marking
    scheme / entrance / syllabus / date sheet / admit card are NOT model
    papers.
  - admission / tuition / hostel / registration fee is NOT an exam fee unless
    the message explicitly names the examination.
  - "improve my marks" without examination/division context is NOT division
    improvement.
"""

from __future__ import annotations

import re
from typing import Any


from app.examination.metadata import (
    normalize_semester as _normalize_semester,
    normalize_subject as _normalize_subject,
    extract_batch as _extract_batch,
    extract_academic_year as _extract_academic_year,
)


def _norm(text: str) -> str:
    """Case-fold and collapse separators (- and _ become spaces).

    The bot renders its examination menu chips with snake_case ids
    ("model_papers", "fee_structure_exam", "division_improvement"); normalising
    those separators lets a chip click and a typed phrase resolve identically.
    """
    return re.sub(r"[\s_\-]+", " ", (text or "").strip().lower())


def _in(phrase: str, norm: str) -> bool:
    """Whole-phrase substring match (unicode-safe word boundaries)."""
    return bool(re.search(r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])", norm))


# --- Model Papers -----------------------------------------------------------

_MODEL_PAPER_POSITIVE = (
    "model paper",
    "model papers",
    "model question paper",
    "model question papers",
    "model qp",
    "model qps",
    "sample paper",
    "sample papers",
    "sample question paper",
    "sample question papers",
    "practice paper",
    "practice papers",
)

_MODEL_PAPER_NEGATIVE = (
    "previous year paper",
    "previous year question paper",
    "previous paper",
    "previous papers",
    "past paper",
    "past papers",
    "past year",
    "pyq",
    "question paper pattern",
    "marking scheme",
    "syllabus",
    "entrance",
    "entrance exam",
    "entrance test",
    "entrance examination",
    "date sheet",
    "datesheet",
    "admit card",
    "improvement",
    "backlog",
)


def _detect_model_papers(text: str, norm: str, entities: Any, hint_norm: str) -> dict[str, Any] | None:
    if any(_in(p, norm) for p in _MODEL_PAPER_NEGATIVE):
        return None
    if any(_in(p, norm) for p in _MODEL_PAPER_POSITIVE):
        intent: dict[str, Any] = {
            "target": "model_papers",
            "confidence": 0.93,
            "reason": "Model papers request",
        }
        # Deterministic filter constraints (strict AND, see service.py).
        # Constraint hints are derived from the RAW user message, not the
        # query-understanding rewrite, so words the preprocessor rewrites
        # (e.g. "batch" -> "back") never silently drop a filter.
        intent["semester"] = _semester_hint(entities) or _semester_hint_text(hint_norm)
        subject = _subject_hint(hint_norm)
        if subject:
            intent["subject"] = subject
        batch = _batch_hint(hint_norm)
        if batch:
            intent["batch"] = batch
        academic_year = _academic_year_hint(hint_norm)
        if academic_year:
            intent["academic_year"] = academic_year
        return intent
    return None


# --- Exam Fee Structure -----------------------------------------------------

_FEE_EXAM_PHRASES = (
    "exam fee",
    "fees of exam",
    "fee for exam",
    "examination fee",
    "fees of examination",
    "fee for examination",
    "examination fee structure",
    "exam fee structure",
    "exam form fee",
    "examination form fee",
    "form fee",
    "form fees",
    "form charges",
    "examination charges",
    "exam charges",
    "examination fee details",
)

# "exam" is its own token (an "examination" never matches the "exam" boundary).
_EXAM_WORDS = ("exam", "examination")
_FEE_WORDS = ("fee", "fees", "charges", "amount")


def _detect_fee_structure_exam(text: str, norm: str, entities: Any = None, hint_norm: str | None = None) -> dict[str, Any] | None:
    if any(_in(p, norm) for p in _FEE_EXAM_PHRASES):
        return {
            "target": "fee_structure_exam",
            "confidence": 0.93,
            "reason": "Examination fee request (explicit phrase)",
        }
    has_exam = any(_in(w, norm) for w in _EXAM_WORDS)
    has_fee = any(_in(w, norm) for w in _FEE_WORDS)
    # A fee is an EXAM fee only when the message itself names the examination;
    # "admission fee", "tuition fee", "hostel fee" etc. have no exam word and
    # fall through to the normal pipeline unchanged.
    if has_exam and has_fee:
        return {
            "target": "fee_structure_exam",
            "confidence": 0.88,
            "reason": "Examination fee request (exam + fee terms)",
        }
    return None


# --- Division Improvement ---------------------------------------------------

_DIVISION_POSITIVE = (
    "division improvement",
    "improve my division",
    "improve division",
    "division upgrade",
    "upgrade my division",
    "upgrade division",
    "better division",
    "higher division",
    "division rules",
    "division criteria",
    "division policy",
    "division improvement exam",
    "improvement exam",
    "improvement examination",
    "how to get a better division",
    "how to improve my division",
    "how to upgrade my division",
)

_DIVISION_NEGATIVE = (
    "improve my marks",
    "improve marks",
    "improving marks",
    "marks improvement",
    "how to improve marks",
    "improve my scores",
    "improve scores",
)


def _detect_division_improvement(text: str, norm: str, entities: Any = None, hint_norm: str | None = None) -> dict[str, Any] | None:
    if any(_in(p, norm) for p in _DIVISION_POSITIVE):
        return {
            "target": "division_improvement",
            "confidence": 0.93,
            "reason": "Division improvement request",
        }
    # "improve my marks" is NOT a division-improvement request (spec): leave it
    # for the main pipeline, which can still pick it up as knowledge/RAG.
    if any(_in(p, norm) for p in _DIVISION_NEGATIVE):
        return None
    return None


# --- Entry point ------------------------------------------------------------

def _programme_hint(entities: Any) -> str | None:
    if entities is None:
        return None
    prog = getattr(entities, "programme", None)
    if prog:
        return str(prog)
    programmes = getattr(entities, "programmes", None) or []
    return str(programmes[0]) if programmes else None


def _programme_hint_text(text: str) -> str | None:
    """Re-derive the programme straight from the normalized message."""
    from app.examination.metadata import extract_programme
    return extract_programme(text)


def _semester_hint(entities: Any) -> int | None:
    if entities is None:
        return None
    try:
        sem = getattr(entities, "semester", None)
        return int(sem) if sem is not None else None
    except (TypeError, ValueError):
        return None


def _semester_hint_text(text: str) -> int | None:
    """Re-derive a semester number straight from the normalized message."""
    return _normalize_semester(text)


def _subject_hint(text: str) -> str | None:
    return _normalize_subject(text)


def _batch_hint(text: str) -> str | None:
    return _extract_batch(text)


def _academic_year_hint(text: str) -> str | None:
    return _extract_academic_year(text)


def detect_examination_intent(text: str, entities: Any = None, raw: str | None = None) -> dict[str, Any] | None:
    """Return an intent dict or None.

    Intent dict: {"target": one of the three ids, "programme", "semester",
    "confidence", "reason"}. Detectors run in order (model papers, exam fee,
    division improvement); the first match wins. A failure to understand the
    message returns None so the rest of the pipeline handles it unchanged.

    ``text`` is the (possibly rewritten) message used for INTENT detection;
    ``raw`` is the original message used for filter-constraint extraction so
    preprocessor rewrites (e.g. "batch" -> "back") never drop a constraint.
    """
    if not text or not text.strip():
        return None
    norm = _norm(text)
    hint_norm = _norm(raw) if raw else norm
    for detector in (
        _detect_model_papers,
        _detect_fee_structure_exam,
        _detect_division_improvement,
    ):
        intent = detector(text, norm, entities, hint_norm)
        if intent:
            intent["programme"] = _programme_hint(entities) or _programme_hint_text(hint_norm)
            intent["semester"] = _semester_hint(entities) or _semester_hint_text(hint_norm)
            return intent
    return None