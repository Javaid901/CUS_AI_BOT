"""
backend/app/multi_source/decompose.py

Deterministic query decomposition for multi-source answering.

Given a compound university question ("What is the MCA eligibility, duration
and exam fee?"), split it into independent sub-queries and select the most
authoritative university source for each one.

Design rules:
  * Alwys deterministic — no LLM, no embeddings, no database access. A
    question is decomposed purely from its wording.
  * A decomposition is returned ONLY when the question genuinely spans two or
    more source classes (programme facts / examination / notices / RAG).
    Single-source questions return ``None`` so the existing planner pipeline
    handles them exactly as it does today.
  * The trigger is conservative: at least two information-need fragments and
    at least two distinct source classes, at least one of which is a
    university source. This protects the existing single-service flows
    (examination fee, date sheets, division improvement, comparisons, ...).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, List, Optional


class SourceType(str, Enum):
    """University knowledge source classes the evidence collector can use."""

    PROGRAMME = "programme"        # structured catalogue / ProgrammeFacts
    EXAMINATION = "examination"    # exam fee / model papers / division improvement
    NOTICES = "notices"            # verified date sheets / reports
    RAG = "rag"                    # hybrid Chroma + BM25 documentary retrieval
    DOCUMENTS = "documents"        # verified + published official university documents
    WEBSITE = "website"            # verified crawler snapshot pages (official site)


_UNIVERSITY_SOURCES = {SourceType.PROGRAMME, SourceType.EXAMINATION, SourceType.NOTICES}

# Maximum number of sub-queries a single message may produce (LLM context cap).
_MAX_SUB_QUERIES = 4


@dataclass(frozen=True)
class SubQuery:
    """One independently answerable information need."""

    text: str
    source: SourceType

    def as_dict(self) -> dict[str, Any]:
        return {"text": self.text, "source": self.source.value}


# ---------------------------------------------------------------------------
# Question-ness and splitting
# ---------------------------------------------------------------------------

_WH_WORDS = re.compile(
    r"\b(what|when|how|why|who|which|where|how much|how many|how long|define|explain|describe)\b",
    re.IGNORECASE,
)
_QUESTION_HANDLES = re.compile(
    r"(.*\?$)|"
    r"(?:\b(?:list|show|give me|tell me|what are)\b)",
    re.IGNORECASE | re.DOTALL,
)

# Top-level conjunction splitting. "and" requires a leading space so "hand" /
# "stand" never split; commas / semicolons split anywhere.
_SPLIT_SEP = re.compile(
    r"(?:\s+and\b|\s+&+\s+|\s+plus\s+|\s+also\b|\s*,\s*|\s*;\s*|\s*\.\s+)",
    re.IGNORECASE,
)


_LEADING_CONJUNCTION = re.compile(r"^(?:and|also|plus|&)\s+", re.IGNORECASE)


def _split_fragments(text: str) -> list[str]:
    parts: list[str] = []
    for raw in _SPLIT_SEP.split(text):
        # A separator that consumes surrounding whitespace (", " / ". ")
        # leaves the next conjunction attached to the following fragment
        # ("...exam fee, and do you have model papers?" -> "and do you...").
        # Strip it so the fragment is a clean standalone information need.
        frag = _LEADING_CONJUNCTION.sub("", raw.strip()).strip()
        if frag:
            parts.append(frag)
    return parts


# ---------------------------------------------------------------------------
# Attribute / source vocabulary
# ---------------------------------------------------------------------------

# Programme-facts attributes that the structured ProgrammeFacts source can
# answer authoritatively.
_PROGRAMME_ATTR_RES = {
    "eligibility": re.compile(
        r"\beligibilit(y|ies)\b|\beligible\b|admission (criteria|requirements?|qualifications?)",
        re.IGNORECASE,
    ),
    "duration": re.compile(
        r"\bduration\b|\bhow long\b|\bhow many years\b|\bcourse length\b|\bprogramme length\b",
        re.IGNORECASE,
    ),
    "subjects": re.compile(
        r"\bsubjects?\b|\bcourses?\b|\bcurriculum\b|\bsyllabus\b|\bsyllabi\b|\bdisciplines?\b",
        re.IGNORECASE,
    ),
    "credits": re.compile(
        r"\bcredits?\b|credit (distribution|structure|system)",
        re.IGNORECASE,
    ),
    "documents": re.compile(
        r"\bdocuments?\b|\bpaperwork\b|required docs?|admission documents?",
        re.IGNORECASE,
    ),
    "fee": re.compile(
        r"\bfees?\b|\btuition\b|programme fee|course fee|admission fee|fee structure|how much( is| does)?",
        re.IGNORECASE,
    ),
}

_EXAM_WORD = re.compile(r"\bexam(ination)?\b", re.IGNORECASE)
_FEE_WORD = re.compile(r"\bfees?\b|\bcharges?\b|\bamount\b", re.IGNORECASE)

_EXAM_FEE_RE = re.compile(
    r"exam(ination)?[ -]?(fee|fees|charges)|fee[s]?\s+for\s+exam|examination charges",
    re.IGNORECASE,
)
_MODEL_PAPER_RE = re.compile(
    r"model paper|previous year paper|last year paper|question papers?|sample papers?",
    re.IGNORECASE,
)
_DIVISION_RE = re.compile(
    r"division improvement|improve.*division|divisio?n (upgrade|rule|policy|criteria)|"
    r"improvement exam|how to (get|be) (a )?better division|higher division",
    re.IGNORECASE,
)
_IMPROVE_PAPERS_RE = re.compile(
    r"(?:\bpapers?\b[^\n]{0,30}\b(?:improve|improvement|reappear|backlog)\b)|"
    r"(?:\b(?:improve|improvement|reappear|backlog)\b[^\n]{0,30}\bpapers?\b)",
    re.IGNORECASE,
)
_WHEN_EXAM_RE = re.compile(
    r"\bwhen\b[^\n]{0,40}\b(?:exams?|semesters?|terms?|papers?|schedules?|"
    r"examinations?|timetables?|date\s?sheets?)\b",
    re.IGNORECASE,
)
_DATESHEET_RE = re.compile(
    r"\bdate\s?sheets?\b|\bexam(ination)? (schedule|dates?|timetable|time[- ]?table)?\b|"
    r"\bexam(ination)? on\b|\bsemester (schedule|dates?)\b",
    re.IGNORECASE,
)

# Deadline vocabulary — a concrete "by when" information need that only the
# verified NOTICES source can answer ("last date", "deadline", "due date").
# Without this a "fee, documents and last date" compound question split its
# deadline off into the current-status funnel and answered it with an old
# notice; here it becomes its own NOTICES sub-question.
_DEADLINE_RE = re.compile(
    r"\blast\s?dates?\b|\bclosing\s?dates?\b|\bdeadline(s)?\b|\bdue\s?dates?\b|"
    r"\blast\s?days?\b|\bclosing\s?days?\b",
    re.IGNORECASE,
)

# Procedure / process vocabulary. A process fragment ("admission process",
# "how to apply", "revaluation process") is a knowledge need of its own — it is
# kept as a fragment and answered from the documentary RAG source (ProgrammeFacts
# carries NO admission process data, so it must never map to PROGRAMME).
_PROCESS_RE = re.compile(
    r"\badmission\s+(process|procedure)\b|\bapplication\s+(process|procedure)\b|"
    r"\bhow\s+to\s+apply\b|\b(?:re-?evaluation|revaluation|admission)\s+"
    r"(?:process|procedure)\b|\bprocedures?\b|\bprocesses?\b",
    re.IGNORECASE,
)


def is_deadline_text(text: str) -> bool:
    """True when the fragment/text asks for a concrete deadline date."""
    return bool(_DEADLINE_RE.search(str(text or "")))

# Vocabulary that ties a fragment to the university corpus (vs. generic text).
_UNI_VOCAB_RE = re.compile(
    r"\b(mca|mba|bca|bba|bsc|msc|mcom|bcom|btech|bachelor|master|programme|program|"
    r"university|cluster|semester|admission|examination|exam|college|degree|curriculum|"
    r"datesheet|eligible|eligibility|syllabus|faculty|department)\b",
    re.IGNORECASE,
)

# Known programme abbreviations used to reconstruct bare attribute fragments
# ("duration" -> "what is the MCA duration?").
_PROGRAMME_RE = re.compile(
    r"\b(mca|mba|bca|bba|bsc|b\.?sc|msc|m\.?sc|mcom|bcom|ma|ba|btech|b\.?tech|bed|med|"
    r"mphil|phd|integrated)\b",
    re.IGNORECASE,
)


def _detect_attribute(frag: str) -> str | None:
    """Return the programme attribute a fragment names, or None."""
    if _EXAM_FEE_RE.search(frag) or (_EXAM_WORD.search(frag) and _FEE_WORD.search(frag)):
        return "exam_fee"
    if _DIVISION_RE.search(frag) or _IMPROVE_PAPERS_RE.search(frag):
        return "division_improvement"
    if _MODEL_PAPER_RE.search(frag):
        return "model_papers"
    if _WHEN_EXAM_RE.search(frag) or _DATESHEET_RE.search(frag):
        return "schedule"
    # A deadline need ("last date", "deadline", "due date") routes to NOTICES.
    if _DEADLINE_RE.search(frag):
        return "deadline"
    # A procedure / process need is a kept knowledge fragment (RAG), never a
    # programme-profile attribute (ProgrammeFacts carries no admission process).
    if _PROCESS_RE.search(frag):
        return "process"
    for attr, regex in _PROGRAMME_ATTR_RES.items():
        if regex.search(frag):
            return attr
    return None


def _source_for(frag: str, full: str) -> SourceType:
    """Pick the authoritative source class for one fragment."""
    attr = _detect_attribute(frag)
    if attr == "exam_fee":
        return SourceType.EXAMINATION
    if attr == "division_improvement":
        return SourceType.EXAMINATION
    if attr == "model_papers":
        return SourceType.EXAMINATION
    if attr == "schedule":
        return SourceType.NOTICES
    if attr == "deadline":
        return SourceType.NOTICES
    if attr == "process":
        return SourceType.RAG
    # A BARE fee fragment ("what is the fee") is a programme-facts request —
    # only the programme fee is stored in ProgrammeFacts (the P2-D fix for
    # "eligibility, fee and last date" splitting its fee into the wrong place).
    if _FEE_WORD.search(frag):
        return SourceType.PROGRAMME
    if attr in _PROGRAMME_ATTR_RES:
        return SourceType.PROGRAMME
    return SourceType.RAG


def _looks_like_question(text: str) -> bool:
    if _WH_WORDS.search(text):
        return True
    return bool(_QUESTION_HANDLES.match(text.strip()))


def _programme_from(text: str) -> str | None:
    m = _PROGRAMME_RE.search(text)
    return m.group(1).lower() if m else None


_LEADING_VERB_RE = re.compile(
    r"^(?:do|does|did|is|are|was|were|can|could|will|would|should|"
    r"show|list|tell|give|provide|share)\b",
    re.IGNORECASE,
)


def _reconstruct(frag: str, full: str) -> str:
    """Turn a noun-phrase fragment into a full standalone sub-question."""
    stripped = frag.strip()
    if _WH_WORDS.search(stripped):
        return stripped.rstrip()
    # A fragment that already reads as a standalone question ("do you have
    # model papers?") must not be prefixed into broken grammar.
    if _LEADING_VERB_RE.match(stripped):
        return stripped.rstrip()
    prog = _programme_from(full) or _programme_from(frag)
    prefix = f"what is the {prog} " if prog else "what is the "
    return (prefix + stripped).rstrip()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def decompose_query(
    message: str,
    entities: Any | None = None,
    ctx: Any | None = None,
) -> list[SubQuery] | None:
    """
    Decompose a compound question into independent sub-queries.

    Returns ``None`` when the message is not a genuine multi-source knowledge
    question (single information need, comparison, service request, ...), so
    the existing planner pipeline handles it unchanged.

    ``entities`` / ``ctx`` are accepted for signature stability (the planner
    already passes them around); decomposition itself is purely lexical.
    """
    if not message or not isinstance(message, str):
        return None
    text = message.strip()
    if not text:
        return None
    if not _looks_like_question(text):
        return None

    fragments = _split_fragments(text)

    kept: list[str] = []
    for frag in fragments:
        # Keep only fragments that carry an explicit information need (a
        # wh-clause or a named attribute). Bare programme names ("mba" in a
        # comparison) add no need of their own and must not inflate the count.
        if _WH_WORDS.search(frag) or _detect_attribute(frag):
            kept.append(frag)
    if len(kept) < 2:
        return None

    subs: list[SubQuery] = []
    for frag in kept[:_MAX_SUB_QUERIES]:
        subs.append(SubQuery(text=_reconstruct(frag, text), source=_source_for(frag, text)))

    sources = {s.source for s in subs}
    # Multi-source requires at least two distinct source classes AND at least
    # one university source (PROGRAMME / EXAMINATION / NOTICES). Purely generic
    # retrievable aggregates ("what are mca and mba placements") are left to
    # the existing comparison / RAG flows.
    if len(sources) < 2:
        return None
    if not any(s in _UNIVERSITY_SOURCES for s in sources):
        return None
    return subs


def query_category(subs: list[SubQuery]) -> str:
    """Classify the overall query for the intelligence taxonomy.

    Returns one of:
      UNIVERSITY_KNOWLEDGE  — all sub-questions come from university sources
      MIXED_QUERY           — university + generic retrievable parts
      GENERAL_KNOWLEDGE     — no university source involved
    (PROTECTED_SERVICE_REQUEST is decided upstream by the planner and never
    reaches decomposition.)
    """
    sources = {s.source for s in subs}
    if not sources:
        return "GENERAL_KNOWLEDGE"
    if any(s in _UNIVERSITY_SOURCES for s in sources):
        return "UNIVERSITY_KNOWLEDGE" if len(sources - _UNIVERSITY_SOURCES) == 0 else "MIXED_QUERY"
    return "GENERAL_KNOWLEDGE"


def _build_programme_label(message: str, entities: Any, ctx: Any) -> str | None:
    """Resolve a programme for the intelligent fragment the same way the
    evidence collector does (sub-text > whole-message entities > context)."""
    try:
        from app.examination.metadata import extract_programme
    except Exception:
        return _programme_from(message)
    for candidate in (
        message,
        getattr(entities, "programme", None) or "",
        getattr(ctx, "programme", None) or "",
    ):
        prog = extract_programme(candidate)
        if prog:
            return str(prog)
    return None


def build_intelligent_subs(
    message: str,
    entities: Any | None = None,
    ctx: Any | None = None,
    kind: str = "knowledge",
) -> list[SubQuery]:
    """Address a single implicit intelligent fragment from bounded sources.

    The general student-assistant path keeps the question as ONE fragment (the
    message is never over-split); the evidence layer below collects per-source
    views of it. Each generated SubQuery carries its own source class and a
    DISTINCT sub-text so the evidence pool groups them cleanly and cross-source
    content divergence inside one intelligent answer is never mis-flagged as a
    conflict between two answers to the same sub-question.

    ``kind`` is ``"status"`` (current-status questions — notices first),
    ``"documents"`` (document/notice comparison — notices and dated
    notifications first) or ``"knowledge"`` (procedures / multi-aspect
    university knowledge — structured programme facts first).
    """
    text = (message or "").strip()
    if not text:
        return []
    prog = _build_programme_label(text, entities, ctx)
    prefix = f"{prog.upper()}: " if prog else ""

    subs: list[SubQuery] = []
    if kind == "status":
        # Status questions: the flagship sub-question is the notices view.
        subs.append(SubQuery(text=text, source=SourceType.NOTICES))
        if prog:
            subs.append(SubQuery(text=f"{prefix}{text}", source=SourceType.PROGRAMME))
    elif kind == "documents":
        # Document comparison questions: notices and dated notifications are
        # the sources whose dates decide "newer" / "still valid".
        subs.append(SubQuery(text=text, source=SourceType.NOTICES))
        if prog:
            subs.append(SubQuery(text=f"{prefix}{text}", source=SourceType.PROGRAMME))
    else:
        if prog:
            subs.append(SubQuery(text=f"{prefix}{text}", source=SourceType.PROGRAMME))
        subs.append(SubQuery(text=text, source=SourceType.NOTICES))
    # For comparison the documents view is phrased to surface dated
    # notifications/notices (whose published dates the answer can compare);
    # the status/knowledge wording is unchanged.
    docs_text = (
        f"Official CUS notices and notifications with their published dates "
        f"relevant to: {text}"
        if kind == "documents"
        else f"Official CUS documents relevant to: {text}"
    )
    subs.append(SubQuery(text=docs_text, source=SourceType.DOCUMENTS))
    subs.append(
        SubQuery(
            text=f"Pages on the official CUS website relevant to: {text}",
            source=SourceType.WEBSITE,
        )
    )
    subs.append(
        SubQuery(
            text=f"Supporting detail from the university knowledge base for: {text}",
            source=SourceType.RAG,
        )
    )
    return subs