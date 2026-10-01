"""
backend/app/orchestrator/current_status.py

Generic current-status detector + intelligent-candidate gate (P0).

This module answers TWO small questions, entirely deterministically, with no
LLM call and no database access:

  1. `classify_current_status(text)` — is the user asking about the CURRENT
     status of something at the university ("is admission open?", "has the
     notification been released?", "when is the last date?")? This is a
     *generic* detector: it keys on status vocabulary combined with an
     explicit university subject. It contains NO per-programme or per-process
     workflows (no `if MCA ...`, no `if admission: step 1 ...`).

  2. `gate_intelligent(text, entities, ctx)` — should this message take the
     general student-assistant path instead of the bare RAG fallback?
     Returns ``"status"``, ``"knowledge"``, or ``None``.

The gate is deliberately conservative:
  * it only fires for messages that are genuinely university-related
    (reusing ``is_university_related`` so "what is the capital of France?"
    and "xyzzy qwerty" pass straight through unchanged);
  * it never absorbs protected single-service requests — those are routed by
    the planner in earlier rules and never reach the gate;
  * fee / exam-fee shorthand without a status marker keeps the planner's
    existing examination-fee disambiguation untouched;
  * short, ambiguous fragments without context keep the existing clarify flow.

The planner applies the gate ONLY after every protected / specialised rule has
run, so a positive result can only change a message that would otherwise have
landed on the plain RAG path.
"""
from __future__ import annotations

import re
from typing import Any

# Status verbs — language that asks about an open/closed/announced state.
# The strong path requires one of these AND at least one status-capable
# subject, so "is the shop open?" never becomes a status question.
_STATUS_MARKER_RE = re.compile(
    r"\b(open|opens|opened|close|closes|closed|closing|started|starts|starting|start|"
    r"begin|begins|begun|commences?|commenced|ends?|ended|released|declared|"
    r"declaration|issued|issue|announced|announcement|published|uploaded|"
    r"available|extended|postponed|rescheduled|withdrawn|cancelled|out\b|yet\b|"
    r"last\s?date|closing\s?date|deadline|due\s?date|start\s?date|end\s?date|"
    r"expected|upcoming|soon)\b",
    re.IGNORECASE,
)

# Status-capable university subjects. Each regex maps to ONE canonical subject
# the evidence layer can align against (a notice about "exam" must never answer
# an "admission" status question). Deliberately excludes evergreen content
# terms (syllabus / curriculum / pattern / scheme / structure / ...) which are
# handled as blockers on the weak-cue path — "what is the current MCA syllabus?"
# must never become a current-status question.
_STATUS_SUBJECT_PAIRS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\badmissions?\b", re.IGNORECASE), "admission"),
    (re.compile(r"\bapply|applying\b", re.IGNORECASE), "application"),
    (re.compile(r"\bapplications?\b", re.IGNORECASE), "application"),
    (re.compile(r"\bregistrations?\b", re.IGNORECASE), "registration"),
    (re.compile(r"\bforms?\b", re.IGNORECASE), "form"),
    (re.compile(r"\bentrance\b", re.IGNORECASE), "entrance"),
    (re.compile(r"\bexams?\b|\bexaminations?\b", re.IGNORECASE), "examination"),
    (re.compile(r"\bresults?\b", re.IGNORECASE), "result"),
    (re.compile(r"\bdate\s?sheets?\b|\bschedules?\b|\btimetables?\b", re.IGNORECASE), "datesheet"),
    (re.compile(r"\bnotifications?\b|\bnotices?\b", re.IGNORECASE), "notification"),
    (re.compile(r"\bscholarships?\b", re.IGNORECASE), "scholarship"),
    (re.compile(r"\bseats?\b", re.IGNORECASE), "seat"),
    (re.compile(r"\bcounsell?ing\b", re.IGNORECASE), "counselling"),
    (re.compile(r"\bsemesters?\b", re.IGNORECASE), "semester"),
    (re.compile(r"\bclasses?\b", re.IGNORECASE), "classes"),
    (re.compile(r"\benroll?ments?\b", re.IGNORECASE), "enrolment"),
    (re.compile(r"\bfees?\b|\bcharges?\b|\btuitions?\b", re.IGNORECASE), "fee"),
    (re.compile(r"\bprospectus\b", re.IGNORECASE), "prospectus"),
)

# Weak currentness cues — English + Hinglish words that, together with a
# status-capable subject, express "the current state of X" without a strong
# status verb ("what is the current admission status?", "MCA ka form abhi bhar
# sakte hain?").
_CURRENTNESS_CUE_RE = re.compile(
    r"\bcurrent(ly)?\b|\bnow\b|\bstill\b|\bstatus\b|"
    r"\babhi\b|\b(?:aa|a)\s+gaya\b|\baaya\b|\bshuru\b|\bkhatam\b|\bband\b|\bchalu\b|"
    r"\bkhul\w*\b|\bkhol\w*\b|"
    r"\bmil\s+gaya\b|\bbhar\s+sakte?\b|\bho\s+gaya\b",
    re.IGNORECASE,
)

# Evergreen reference content. A weak currentness cue ("current") aimed at
# evergreen material ("current MCA syllabus", "current examination pattern",
# "current duration / eligibility / process") is NOT a status question.
EVERGREEN_TARGETS = frozenset({
    "syllabus", "syllabi", "curriculum", "curricula", "pattern", "patterns",
    "scheme", "schemes", "structure", "structures", "framework", "frameworks",
    "policy", "policies", "module", "modules", "credit", "credits",
    "duration", "durations", "eligibility", "eligibilities", "process",
    "processes", "procedure", "procedures",
})
_EVERGREEN_TARGET_RE = re.compile(
    r"\b(" + "|".join(sorted(EVERGREEN_TARGETS)) + r")\b",
    re.IGNORECASE,
)

# The fee subject is status-bearing only on the STRONG-marker path ("is the MCA
# fee announced?") — never via a weak currentness cue ("what is the current MCA
# fee?" stays a fee question). When every matched subject is "fee", a weak cue
# must not turn it into a status question.
_WEAK_FEE_ONLY_BLOCK = frozenset({"fee"})


def status_subjects_of(text: str) -> frozenset[str]:
    """Return the canonical status-capable subjects named by ``text``.

    Used both by the status detector and by the evidence layer to align a
    current-status/deadline sub-question with the official evidence about the
    SAME subject (a result question must never be grounded in an admission
    notice, and vice versa).
    """
    low = str(text or "").lower()
    found: set[str] = set()
    for regex, canonical in _STATUS_SUBJECT_PAIRS:
        if regex.search(low):
            found.add(canonical)
    return frozenset(found)

# Knowledge flavor — complex procedures / multi-aspect university questions
# today answered only by a bare RAG retrieval.  Kept deliberately tight:
# bare "apply for" / "what documents" stay on their existing catalogue /
# slot-fill routes, and things like "who can apply for MCA?" (eligibility)
# or "how many semesters..." (programme profile) never match here.
_KNOWLEDGE_RE = re.compile(
    r"\b(procedure|process|steps?|step ?-?by ?-?step|how (do|does|can|could|"
    r"to|should)|explain|describe|entrance (test|exam)|"
    r"documents (required|needed) for|required for|needed for)\b",
    re.IGNORECASE,
)

_FEE_RE = re.compile(r"\bfees?\b|\bcharges?\b|\bamount\b", re.IGNORECASE)

# P1-D — document/notice comparison & currentness flavor (routed to the
# intelligent evidence path so "which notice is newer?" is answered from dated
# official evidence, not a bare RAG snippet). Deliberately requires an explicit
# document vocabulary token ("notice", "notification", "document") so ordinary
# questions never enter this branch; date sheets / exam services are already
# handled by the protected rules that run BEFORE this gate.
_DOCUMENT_COMPARISON_RE = re.compile(
    r"(?:\b(?:which|what)\b[^\n?]{0,30}\b(?:notice|notification|document)s?"
    r"\b\s+(?:is|are|was)\b[^\n?]{0,20}\b(?:newer|newest|latest|later|more recent|"
    r"revised|changed|superseded)\b)|"
    r"(?:\b(?:is|are|was)\b[^\n?]{0,40}\b(?:notice|notification|document)s?"
    r"\b\s+(?:still\s+(?:valid|applicable|in\s+force)|revised|withdrawn|cancelled|"
    r"superseded)\b)|"
    r"(?:\b(?:compare|comparison of|difference between)\b[^\n?]{0,40}\b"
    r"(?:notices?|notifications?|documents?)\b)",
    re.IGNORECASE,
)


def classify_current_status(text: str) -> bool:
    """True when the message asks about the current open/announced status of an
    explicit university subject. Generic, deterministic, no per-process rules.

    Two mutually exclusive paths:

      * STRONG path — a status verb (open / closed / released / started /
        declared / last date / deadline / ...) together with a status-capable
        university subject ("is MCA admission open?", "when is the last date
        for the exam form?"). Fee questions count here ("is the MCA fee
        announced?") because the verb makes the current state explicit.
      * WEAK-cue path — a currentness cue (current / now / status / abhi /
        shuru / band / bhar sakte ...) together with a status-capable subject,
        but NEVER aimed at evergreen reference content (syllabus / pattern /
        duration / eligibility / process) and NEVER with "fee" as its subject:
        "what is the current MCA fee?" stays a fee question.
    """
    low = str(text or "")
    if _STATUS_MARKER_RE.search(low) and status_subjects_of(low):
        return True
    if not _CURRENTNESS_CUE_RE.search(low):
        return False
    if _EVERGREEN_TARGET_RE.search(low):
        return False
    subjects = status_subjects_of(low)
    if not subjects:
        return False
    if subjects <= _WEAK_FEE_ONLY_BLOCK:
        return False
    return True


def _knowledge_candidate(text: str, entities: Any, ctx: Any) -> bool:
    low = str(text or "")
    if not re.search(_KNOWLEDGE_RE, low):
        return False
    # Short ambiguous fragments without a programme stay in the clarify /
    # slot-fill flow ("documents required for admission", "how to apply").
    word_count = getattr(entities, "word_count", None)
    has_programme = bool(
        getattr(entities, "programme", None)
        or getattr(entities, "programmes", None)
        or getattr(ctx, "programme", None)
    )
    if word_count is not None and word_count <= 4 and not has_programme:
        return False
    return True


def gate_intelligent(
    text: str,
    entities: Any = None,
    ctx: Any = None,
) -> str | None:
    """Decide whether a message takes the general student-assistant path.

    Returns ``"status"``, ``"knowledge"``, ``"documents"``, or ``None``.
    ``None`` means the existing planner flow keeps the message exactly as today.
    """
    from app.orchestrator.context import is_university_related

    text = str(text or "").strip()
    if not text:
        return None
    if not is_university_related(text, entities):
        return None
    # P1-D: document/notice comparison & currentness questions take the
    # evidence-gathered intelligent path (comparison mode). Checked BEFORE the
    # status detector so "is this notice still valid?" / "are these two notices
    # still in force?" (document vocabulary + currentness cue) stay on the
    # documents evidence path instead of collapsing into "status".
    if _DOCUMENT_COMPARISON_RE.search(text):
        return "documents"
    if classify_current_status(text):
        return "status"
    # Fee shorthand with no status marker must keep the existing
    # examination-fee disambiguation in the planner.
    if re.search(_FEE_RE, text):
        return None
    if _knowledge_candidate(text, entities, ctx):
        return "knowledge"
    return None