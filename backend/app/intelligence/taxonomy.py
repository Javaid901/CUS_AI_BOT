"""
backend/app/intelligence/taxonomy.py

Deterministic query-kind taxonomy for the future Intelligent Answering layer.

This is NOT a second intent classifier. It is a lightweight, higher-level
semantic classification that sits ABOVE the existing intent system
(planner / intent_classifier / extractor) and reuses that system's own
vocabulary and deterministic detectors instead of inventing parallels.

Guarantees:
  - fully deterministic (pure rules + the existing deterministic detectors)
  - never calls an LLM, never loads an embedding model, never performs network
    calls, never touches the database

Entry points:

  classify(message, *, entities=None, existing_action=None) -> QueryKind
      When an existing planner Plan.action is supplied it is authoritative and
      simply labelled — the taxonomy describes the already-decided route and
      never alters routing. Without a plan, a conservative text-signal
      classifier is used.

  requires_aggregation(message, entities=None) -> bool
      Aggregation-specific signal ("list all ...", "how many ...") reused by
      the intelligence contract.
"""

from __future__ import annotations

import re
from enum import Enum
from typing import Any


class QueryKind(str, Enum):
    """High-level kinds of university questions.

    One level of abstraction ABOVE the existing fine-grained intents in
    app.orchestrator.contract.INTENTS.
    """

    NAVIGATION = "navigation"
    PERSONAL_SERVICE = "personal_service"
    DEDICATED_SERVICE = "dedicated_service"
    STRUCTURED_FACT = "structured_fact"
    DOCUMENT_LOOKUP = "document_lookup"
    AGGREGATION = "aggregation"
    ANALYTICAL = "analytical"
    EXPLORATORY = "exploratory"
    GENERAL_INFORMATION = "general_information"
    UNKNOWN = "unknown"


# ---------------------------------------------------------------------------
# Existing planner action -> kind (authoritative when a plan is available).
# Kept in sync with the actual Plan actions produced by app.orchestrator.planner
# and executed by app.orchestrator.engine.
# ---------------------------------------------------------------------------

_ACTION_TO_KIND: dict[str, QueryKind] = {
    "welcome": QueryKind.NAVIGATION,
    "greeting": QueryKind.NAVIGATION,
    "navigation": QueryKind.NAVIGATION,
    "structured": QueryKind.STRUCTURED_FACT,
    "catalogue": QueryKind.STRUCTURED_FACT,
    "comparison": QueryKind.ANALYTICAL,
    "rag": QueryKind.DOCUMENT_LOOKUP,
    "news": QueryKind.DOCUMENT_LOOKUP,
    "examination": QueryKind.DEDICATED_SERVICE,
    "authority": QueryKind.DEDICATED_SERVICE,
    "grievance": QueryKind.DEDICATED_SERVICE,
    "university_notices": QueryKind.DEDICATED_SERVICE,
    "student_service": QueryKind.PERSONAL_SERVICE,
}


# ---------------------------------------------------------------------------
# Deterministic text signals (only used when no planner action is available)
# ---------------------------------------------------------------------------

# Coarse navigation signals: greeting words / menu labels / back / reset.
_GREETINGS: frozenset[str] = frozenset({
    "hello", "hi", "hey", "hii", "hola", "namaste",
    "good morning", "good afternoon", "good evening",
    "welcome", "start", "menu", "home",
})

_BACK_RESET_WORDS: frozenset[str] = frozenset({"back", "reset"})

# Personal (auth-gated) student services. Mirrors the planner's gated families
# (results / admit_card / exam_form) in high-level vocabulary — the planner
# remains authoritative for routing.
_PERSONAL_SERVICE_MARKERS: tuple[str, ...] = (
    "student services", "student service", "student results", "student result",
    "student admit card", "student exam form",
    "my result", "my results", "show my result", "check my result",
    "my semester", "my admit card", "my hall ticket", "download admit card",
    "print admit card", "my exam form", "fill exam form", "print exam form",
)

# Dedicated structured service markers (examinations menu + notices/date-sheet).
_DEDICATED_SERVICE_MARKERS: tuple[str, ...] = (
    "model paper", "model papers", "model question paper",
    "exam fee", "examination fee", "division improvement",
    "improvement of division",
)

_NOTICES_MARKERS: tuple[str, ...] = (
    "date sheet", "date-sheet", "datesheet", "time table", "timetable",
    "exam schedule", "examination schedule",
)

# Document-reference frames ("what does the prospectus say ...") -> lookup.
_DOC_REFERENCE_PATTERNS: tuple[str, ...] = (
    r"\baccording to\b",
    r"\b(say|says) about\b",
    r"\brefer(?:s|ring)? to\b",
    r"\bwhat does (?:the |this )?(?:document|pdf|file|prospectus|handbook|brochure|manual)\b",
    r"\btell me about the (?:prospectus|handbook|brochure|manual)\b",
)

# Aggregation triggers: enumerate / count / list across a source.
_AGGREGATION_PATTERNS: tuple[str, ...] = (
    r"\blist\b",
    r"\benumerate\b",
    r"\bhow many\b",
    r"\bwhich all\b",
    r"\bare there\b",
    r"\bname (?:all|every)\b",
    r"\bshow me all\b",
    r"\btell me all\b",
    r"\ball (?:the )?(?:pg|ug|programme|programmes|courses|departments|colleges|schemes|subjects)\b",
    r"\bevery\b",
    r"\bavailable (?:pg|ug|programme|programmes|courses)\b",
)

# Analytical triggers: comparison / reconciliation / multi-aspect questions.
_ANALYTICAL_PATTERNS: tuple[str, ...] = (
    r"\b(?:vs|versus)\b",
    r"\bdifference between\b",
    r"\bcompare\b",
    r"\bcomparison\b",
)

_AGGREGATION_RE = re.compile(
    "|".join(f"(?:{p})" for p in _AGGREGATION_PATTERNS), re.IGNORECASE
)
_ANALYTICAL_RE = re.compile(
    "|".join(f"(?:{p})" for p in _ANALYTICAL_PATTERNS), re.IGNORECASE
)
_DOC_REFERENCE_RE = re.compile(
    "|".join(f"(?:{p})" for p in _DOC_REFERENCE_PATTERNS), re.IGNORECASE
)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def requires_aggregation(message: str, entities: Any = None) -> bool:
    """Deterministic aggregation signal for a message.

    A 2+ programme comparison (entities.programmes) is analytical, not
    aggregation — and the existing planner decides routing in either case.
    """
    if not message:
        return False
    if _AGGREGATION_RE.search(message):
        if entities is not None and len(getattr(entities, "programmes", None) or []) >= 2:
            return False
        return True
    return False


def classify(
    message: str,
    *,
    entities: Any = None,
    existing_action: str | None = None,
) -> QueryKind:
    """Classify a message into one high-level QueryKind (deterministic).

    Priority:
      1. an existing planner action (authoritative label of the real route)
      2. conservative text signals (navigation, general-information, personal
         service, dedicated service, document refs, analytical, structured
         facts, aggregation, exploratory, unknown)
    """
    text = (message or "").strip()
    if not text:
        return QueryKind.UNKNOWN

    # 1. The existing planner route is authoritative when it is mapped.
    if existing_action:
        kind = _ACTION_TO_KIND.get(existing_action)
        if kind is not None:
            return kind

    low = text.lower()

    # 2a. Navigation (back / reset / greeting).
    if low.strip().rstrip("?.,!;:") in _BACK_RESET_WORDS or _is_greeting(low):
        return QueryKind.NAVIGATION

    # 2b. Explicitly outside the university domain — reuses the existing
    #     safe-fallback scope gate (no parallel detector is built here).
    if _is_outside_scope(text):
        return QueryKind.GENERAL_INFORMATION

    # 2c. Personal (auth-gated) student services.
    if _matches_any(low, _PERSONAL_SERVICE_MARKERS):
        return QueryKind.PERSONAL_SERVICE

    # 2d. Dedicated structured services (examinations menu, notices/dates).
    if _matches_any(low, _DEDICATED_SERVICE_MARKERS) or _matches_any(low, _NOTICES_MARKERS):
        return QueryKind.DEDICATED_SERVICE

    # 2e. Explicit document references.
    if _DOC_REFERENCE_RE.search(text):
        return QueryKind.DOCUMENT_LOOKUP

    # 2f. Analytical / comparison (a scoped 2+ programme or comparison verbs).
    ent = entities
    if _ANALYTICAL_RE.search(text):
        return QueryKind.ANALYTICAL

    # Reuse the existing deterministic extractor for programme/level/semester
    # detection — never a parallel detector.
    if ent is None:
        try:
            from app.orchestrator.extractor import extract_entities
            ent = extract_entities(text)
        except Exception:
            ent = None
    if ent is not None and len(getattr(ent, "programmes", None) or []) >= 2:
        return QueryKind.ANALYTICAL

    # 2g. Structured facts (scoped programme/topic anchors).
    prog = getattr(ent, "programme", None) if ent is not None else None
    topic = getattr(ent, "topic", None) if ent is not None else None
    if prog or topic:
        return QueryKind.STRUCTURED_FACT

    # 2h. Aggregation / enumeration.
    if requires_aggregation(text, ent):
        return QueryKind.AGGREGATION

    # 2i. Exploratory / discovery questions.
    if _is_exploratory(text, ent):
        return QueryKind.EXPLORATORY

    return QueryKind.UNKNOWN


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _matches_any(low: str, markers: tuple[str, ...]) -> bool:
    return any(marker in low for marker in markers)


def _is_greeting(low: str) -> bool:
    clean = low.strip().rstrip("?.,!;: ")
    if clean in _GREETINGS:
        return True
    if len(clean.split()) <= 2 and clean.startswith(("hi ", "hello ")):
        return True
    return False


def _is_outside_scope(text: str) -> bool:
    try:
        from app.chat.fallback import _is_outside_scope as _scope
        return bool(_scope(text))
    except Exception:
        return False


def _is_exploratory(text: str, entities: Any) -> bool:
    low = text.strip().lower()
    first_word = low.split(" ")[0].strip("?.,!;:") if low else ""
    try:
        from app.orchestrator.context import DOMAIN_KEYWORDS, QUESTION_STARTERS
        if first_word in QUESTION_STARTERS:
            return True
        if low.rstrip("?.,!;:") in DOMAIN_KEYWORDS:
            return True
    except Exception:
        pass
    return bool(
        re.search(
            r"\b(?:how (?:to|do|can|does|is|are|w?ould)|what (?:is|are)|tell me|explain|describe|guide|help me|overview|details|information)\b",
            low,
        )
    )