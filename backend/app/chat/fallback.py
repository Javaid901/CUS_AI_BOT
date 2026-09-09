"""
backend/app/chat/fallback.py

Useful fallback builder — used whenever the chatbot cannot ground an answer
(no retrieved chunks, generator failure, or the LLM itself reports that the
information is not in the knowledge base).

Rules (never hallucinate):
  1. State clearly that the information is not currently available.
  2. When a confident match exists, recommend the relevant ACTIVE authority
     as a contact card (real records only — never invented names/emails).
  3. Offer concrete next steps (file a grievance / browse official topics)
     as option chips that the existing SSE pipeline renders.
  4. For completely outside‑scope questions (e.g. weather, general knowledge),
     do not recommend a university authority — just guide the user back to
     supported university‑related queries.
"""

from __future__ import annotations

from typing import Any

from app.ingest.prompts import FALLBACK_MESSAGE
from app.authority.matcher import (
    DEPARTMENT_ALIASES,
    SERVICE_ROUTES,
    detect_escalation_intent,
    find_authority,
)
from app.authority.service import authority_service

# Minimum keyword-overlap score (same gate the planner uses for authority
# intent) — prevents weak token overlap from painting a random card.
_AUTHORITY_MATCH_SCORE = 2.0

# Alias/service routes that resolve a department, used as authority evidence
# for escalation-flagged messages (e.g. "contact the examination branch").
_ALIAS_MAPS = {**DEPARTMENT_ALIASES, **SERVICE_ROUTES}

# Keywords and patterns that indicate a question is clearly outside the
# university chatbot's supported scope. Matched case‑insensitively.
_OUTSIDE_SCOPE_PATTERNS: tuple[str, ...] = (
    "weather",
    "stock",
    "market",
    "currency",
    "horoscope",
    "general knowledge",
    "who is the",
    "what is the capital",
    "prime minister",
    "president",
    "sports score",
    "result table",  # generic sports/competition table
)


def _is_outside_scope(message: str) -> bool:
    """Return True when the user query is clearly outside the university
    chatbot's supported domain. No LLM calls — pure keyword/match checks."""
    lower = message.lower()
    for pattern in _OUTSIDE_SCOPE_PATTERNS:
        if pattern in lower:
            return True
    return False


def _next_steps_text(outside_scope: bool) -> str:
    """Return the appropriate next-steps suffix based on query type."""
    if outside_scope:
        return " I can help you with information related to Cluster University and its available services."
    return (
        " I can still help you reach the right office, or you can file a formal "
        "grievance about it — choose an option below. If you prefer, rephrase "
        "your question and I will try again."
    )


_FALLBACK_OPTIONS: dict[str, Any] = {
    "type": "options",
    "selector": "fallback",
    "title": "How would you like to proceed?",
    "message": "",  # message set per-query in build_fallback_response
    "no_back": True,
    "options": [
        {"id": "grievance", "label": "📋 File a Grievance"},
        {"id": "admissions", "label": "🎓 Admissions"},
        {"id": "courses", "label": "📚 Courses"},
        {"id": "examination", "label": "🗓 Examinations"},
    ],
}

_NEXT_STEPS_CARD = (
    "The relevant office below can help with this. You can also file a formal "
    "grievance — tap the button beneath the card."
)


def _authority_card(authority: dict[str, Any]) -> dict[str, Any]:
    """Build a detail-card event for the matched authority (same shape the
    engine renders for explicit authority queries)."""
    return {
        "type": "detail",
        "title": authority.get("authority_name", "University Office"),
        "message": authority.get("description") or _NEXT_STEPS_CARD,
        "fields": [
            {"label": "Department", "value": authority.get("department_name", "")},
            {"label": "Officer", "value": authority.get("designation") or authority.get("authority_name", "")},
            {"label": "Phone", "value": authority.get("phone", "")},
            {"label": "Email", "value": authority.get("email", "")},
            {"label": "Office Timing", "value": authority.get("office_timings") or ""},
            {"label": "Working Days", "value": authority.get("working_days") or ""},
            {"label": "Address", "value": authority.get("office_address") or ""},
        ],
        "actions": [
            {"id": f"grievance_{authority.get('id', '')}", "label": "📋 File a Grievance"},
            {"id": f"call_{authority.get('id', '')}", "label": f"Call {authority.get('phone', '')}", "type": "phone"},
            {"id": f"email_{authority.get('id', '')}", "label": f"Email {authority.get('email', '')}", "type": "email"},
        ],
    }


def _recommended_authority(message: str) -> dict[str, Any] | None:
    """Find a confident, real, ACTIVE authority for the user message.

    Accepts keyword-overlap matches (score >= 2.0) or escalation requests
    ("speak to someone", "who do I contact") that additionally name a
    routed department/service — the office is resolved directly from the
    ACTIVE cache, so a zero-token-overlap request still routes correctly.
    """
    try:
        matches = find_authority(message, top_k=1)
        if matches:
            best = matches[0]
            if float(best.get("_match_score", 0.0)) >= _AUTHORITY_MATCH_SCORE:
                return best
        if detect_escalation_intent(message) >= 0.7:
            dept = _department_from_alias(message)
            if dept:
                active = authority_service.list_active()
                by_dept = [a for a in active if (a.get("department_name") or "").lower() == dept.lower()]
                if len(by_dept) == 1:
                    return by_dept[0]
    except Exception:
        return None
    return None


def _department_from_alias(message: str) -> str | None:
    """Longest matching alias/service-route department (len >= 3 guard)."""
    low = message.lower()
    best: tuple[str, int] | None = None
    for alias, dept in _ALIAS_MAPS.items():
        if len(alias) < 3 or alias not in low:
            continue
        if best is None or len(alias) > best[1]:
            best = (dept, len(alias))
    return best[0] if best else None


def build_fallback_response(
    message: str,
    base_text: str | None = None,
) -> dict[str, Any]:
    """Build the helpful fallback payload for an unanswered message.

    Returns:
        {
          "text":    fallback text sent to the user,
          "card":    optional detail event for the recommended authority,
          "options": options event with concrete next steps,
        }
    """
    outside_scope = _is_outside_scope(message)
    authority = _recommended_authority(message)

    # For outside-scope questions: never recommend a university authority.
    # Include the appropriate next-step suffix that guides the user back
    # to supported university information without exposing a specific office.
    authority_for_card = authority if not outside_scope else None

    result: dict[str, Any] = {
        "text": (base_text or FALLBACK_MESSAGE)
        + " "
        + _next_steps_text(outside_scope),
        "options": _FALLBACK_OPTIONS,
    }

    if authority_for_card:
        result["card"] = _authority_card(authority_for_card)
    return result