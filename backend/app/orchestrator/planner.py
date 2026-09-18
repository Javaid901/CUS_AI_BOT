"""
backend/app/orchestrator/planner.py

Decision planner — decides the optimal execution path for each user message.

Planner output (Plan):
  action:   "structured" | "navigation" | "rag" | "llm" | "clarify" | "welcome" | "news" | "authority"
  target:   the object of the action (programme ID, topic, etc.)
  response: pre-built structured response (if action is "structured" or "navigation")
  confidence: 0.0–1.0
  reason:   human-readable explanation

Decision rules (applied in order):
   1. Reset signal → welcome
   2. Back → previous nav path
   3. Authority / office intent → authority (registrar, exam wing, "who
      handles exams" — checked before slot-fill can swallow them)
   4. News / website knowledge intent → news (notices, circulars,
      notifications, calendar, announcements — NEVER a dead-end menu)
   5. Academic catalogue request (data-gated) → catalogue
   6. Bare programme name (word count <= 2) → structured programme details
   5. Active programme context + topic → structured if data exists
   5b. Programme + topic in the SAME message → direct answer (structured/rag)
   6. College context + topic → college-specific data (structured/rag)
   7. Active college + short follow-up → college-specific response
   8. Active college + programme selection → college programme details
   9a. Course ↔ college discovery queries → navigation options
   9b. College + programme + topic → structured if data exists
   9. Known option selection (button click) → navigation
   10b. Topic WITHOUT programme (and no context) → slot-fill question (which programme?)
   10. Broad keyword / semantic intent → navigation (browse fallback)
   11. Programme + topic + no structured data → rag
   12. Programme switch detected → structured / rag / navigation
   13. Active programme + short follow-up → rag
   14. Broad question with programme context → rag
   15. Authority / escalation intent → authority (non-question forms)
   16. Broad question without context → rag
   17. Short ambiguous → clarify
   18. Everything else → rag

Slot-fill: when a concrete topic (fee, eligibility, duration, ...) is requested
without a programme, the assistant asks a single targeted question for the missing
slot instead of falling back to browse buttons. The answer continues the original
request without restarting the conversation.

The planner is entirely rule-based (no LLM calls) for speed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from app.chat.intent_router import (
    classify as classify_nav,
)
from app.chat.intent_router import (
    get_broad_response,
    get_nav_path,
    get_selection_response,
    is_option_selection,
)
from app.college import course_map as college_course_map
from app.college.aliases import is_college_reference
from app.college.aliases import resolve as resolve_college
from app.college.service import CollegeService
from app.catalogue.detect import (
    detect_catalogue_aspect,
    detect_catalogue_request,
    programme_overview_request,
)
from app.catalogue.service import programme_by_id
from app.grievance.detect import detect_grievance
from app.orchestrator.context import (
    COLLEGE_TOPICS,
    DOMAIN_KEYWORDS,
    PROGRAMME_ALIASES,
    ConversationContext,
    detect_programme_switch,
    is_referential_followup,
    is_university_related,
)
from app.orchestrator.contract import build_contract, detect_fee_type
from app.multi_source.decompose import decompose_query, query_category
from app.orchestrator.extractor import (
    extract_entities,
    is_informational_question,
)
from app.orchestrator.lookup import (
    lookup_field,
    lookup_programme,
)
from app.orchestrator.query_understanding import (
    process_query as process_query_understanding,
)

_college_svc = CollegeService()


# ---------------------------------------------------------------------------
# Unsupported personal student services
# ---------------------------------------------------------------------------
# The assistant has NO student-data pipeline: it cannot look up a student's
# own results, admit card, attendance, marks, transcript, etc. A bare or
# imperative request for one of these must never enter the catalogue
# programme-selection / slot-fill workflow, which would falsely imply the bot
# can fetch the student's personal details. Informational questions ("when
# will results be announced?", "where are results published?") keep flowing
# through the normal knowledge/RAG path.
_UNSUPPORTED_SERVICE_FAMILIES: dict[str, tuple[str, ...]] = {
    "results": ("results", "result"),
    "attendance": ("attendance", "attendence"),
    "admit_card": ("admit card", "admit_card", "admit-card"),
    "hall_ticket": ("hall ticket",),
    "internal_marks": ("internal marks", "internals"),
    "semester_result": ("semester result", "semester results", "exam roll number", "examination roll number", "roll number", "roll no", "roll_number"),
    "transcript": ("transcript", "marksheet", "mark sheet"),
    "backlog": ("backlogs", "backlog"),
    "exam_form": ("exam form", "examination form", "exam_form"),
    "student_portal": ("student portal",),
    "fee_receipt": ("fee receipt", "fee receipts"),
}
_UNSUPPORTED_SERVICE_PHRASES = tuple(sorted(
    (phrase for phrases in _UNSUPPORTED_SERVICE_FAMILIES.values() for phrase in phrases),
    key=len,
    reverse=True,
))
# Imperatives that turn a service mention into a direct request.
_UNSUPPORTED_SERVICE_ACTION_VERBS = (
    "download", "view", "print", "get", "show", "check", "need", "want",
    "request", "obtain", "see", "open", "fetch", "access", "generate",
)
# Wh-question frames. Even with an action verb ("Where can I check university
# results?"), these are general-knowledge questions, not private-data lookups
# — unless the personal pronoun "my" appears.
_UNSUPPORTED_QUESTION_FRAMES = (
    "what ", "why ", "when ", "where ", "which ", "who ", "how ", "whom ", "whose ",
)
# Extracted topics that name an unsupported personal service. Rule 10b must
# never slot-fill these into a "which programme?" workflow.
_UNSUPPORTED_TOPICS = {"results", "attendance"}

# Families that Step-1 moves behind the Student Services auth gate. A direct
# request for one of these → student_service (sign-in + placeholder), never a
# plain "unavailable" dead end.
_GATED_SERVICE_FAMILIES: frozenset[str] = frozenset(
    {"results", "admit_card", "exam_form"}
)


def _match_unsupported_service(text: str) -> str | None:
    """Map raw text to an unsupported-service family, or None."""
    for phrase in _UNSUPPORTED_SERVICE_PHRASES:
        if phrase in text:
            for family, phrases in _UNSUPPORTED_SERVICE_FAMILIES.items():
                if phrase in phrases:
                    return family
    return None


def _unsupported_service_request(text: str) -> str | None:
    """Return the family when the message is a direct request for an
    unsupported personal student service, else None.

    Direct-request signals: possession ("my …"), an imperative/action verb,
    or a bare / very short service mention ("results", "admit card"). Queries
    phrased for general knowledge ("when will results be announced?", "where
    does the university publish results?") are deliberately left alone so
    they keep using the normal knowledge/RAG path.
    """
    t = (text or "").strip().lower()
    if len(t) < 3:
        return None
    family = _match_unsupported_service(t)
    if not family:
        return None
    if is_informational_question(t):
        return None
    words = t.split()
    if "my" in words:
        return family
    if t.startswith(_UNSUPPORTED_QUESTION_FRAMES):
        return None
    if any(verb in t for verb in _UNSUPPORTED_SERVICE_ACTION_VERBS):
        return family
    if len(words) <= 2:
        return family
    return None


# An examination roll number in a message ("my exam roll number is 2300101",
# "roll no: 2300101"). Mirrors the engine's prefill pattern + the results
# service value rule, so only a VALID roll token routes — a bare "roll
# numbers" noun or a short "roll no 12" never hijacks a query.
_ROLL_TOKEN_RE = re.compile(
    r"\b(?:examination\s+)?roll\s*(?:number|no\.?)?\s*(?:is\s+|:)?\s*"
    r"([A-Za-z0-9][A-Za-z0-9-]{1,49})\b",
    re.IGNORECASE,
)
_ROLL_VALUE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{1,49}$")


def _roll_in_text(text: str) -> str | None:
    m = _ROLL_TOKEN_RE.search((text or "").strip())
    if m and _ROLL_VALUE_RE.fullmatch(m.group(1)):
        return m.group(1)
    return None


@dataclass
class Plan:
    action: str  # structured | navigation | rag | llm | clarify | welcome | news | authority
    target: str | None = None
    response: dict[str, Any] | None = None
    confidence: float = 0.0
    reason: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


def plan(
    message: str,
    ctx: ConversationContext,
    chat_id: str,
    entities: Any,  # ExtractedEntities
) -> Plan:
    """Public plan() — decides the execution path AND attaches the canonical
    query contract (see app.orchestrator.contract) to the plan.

    The contract is the single representation of what the user wants; the
    engine persists it on state so later turns inherit resolved fields.
    """
    result = _plan_inner(message, ctx, chat_id, entities)
    try:
        catalogue_req = None
        if result.action == "catalogue":
            catalogue_req = (result.extra or {}).get("req")
        semantic_intent = (result.extra or {}).get("semantic_intent")
        semantic_conf = float((result.extra or {}).get("semantic_confidence") or 0.0)
        contract = build_contract(
            message,
            entities,
            ctx=ctx,
            semantic_intent=semantic_intent,
            semantic_confidence=semantic_conf,
            catalogue_req=catalogue_req,
            plan_action=result.action,
            plan_target=result.target,
        )
        result.extra["contract"] = contract.as_dict()
        # Phase 1 (Safe Intelligence Foundation): attach advisory intelligence
        # metadata ONLY when INTELLIGENCE_ENABLED=True. When the flag is off
        # (default) nothing here runs — no extra imports, no retrieval, no model
        # calls, no user-visible change. The catch below means a failure can
        # never break planning.
        try:
            from app.config import settings as _ai_settings
            if getattr(_ai_settings, "INTELLIGENCE_ENABLED", False):
                from app.intelligence.contract_ext import build_intelligence_contract
                result.extra["intelligence"] = build_intelligence_contract(
                    message, entities, ctx=ctx, plan=result,
                ).as_dict()
        except Exception:
            pass  # intelligence metadata must never break planning
    except Exception:
        pass  # a contract must never break planning
    return result


def _plan_inner(
    message: str,
    ctx: ConversationContext,
    chat_id: str,
    entities: Any,  # ExtractedEntities
) -> Plan:
    """Decide the optimal execution path for a user message.

    Args:
        message: raw user message
        ctx: current conversation context
        chat_id: session ID for nav path lookup
        entities: pre-extracted entities

    Returns:
        Plan dataclass with action and optional pre-built response.
    """
    text = message.strip().lower()
    clean = text.rstrip("?.,!;:")
    e = entities

    # ---- Stage 0-pre: Raw-message intent (greeting / grievance) ----
    # These checks run on the RAW message, BEFORE query understanding. The
    # preprocessing pass rewrites short utterances via a fuzzy dictionary and
    # must never be allowed to mangle intent markers ("good morning" ->
    # "govt joining", "mera fee refund" -> "ma fee"). A pure greeting opens
    # the welcome menu; a complaint routes to the grievance intake composer.
    greeting_kind = _detect_greeting(text)
    if greeting_kind:
        return Plan(
            action="greeting",
            confidence=0.95,
            reason=f"{greeting_kind} detected",
            extra={"kind": greeting_kind},
        )
    try:
        det = detect_grievance(text)
        if det["is_grievance"]:
            return Plan(
                action="grievance",
                confidence=0.9,
                reason=f"Grievance intent detected ({det['reason']})",
                extra={
                    "category": det["category"],
                    "marker": det["marker"],
                    "query": message.strip(),
                },
            )
    except Exception:
        pass  # detector failures never break the normal flow

    # ---- Stage 0-pre-exam: Exam form natural-language intents ----
    # "fill exam form" / "print exam form" must reach the student_service
    # exam_form flow directly.  This runs BEFORE query-understanding
    # preprocessing which rewrites "fill" -> "final" and "print" -> "price",
    # destroying the action verb that the unsupported-service rule needs.
    _exam_nl = re.match(
        r"^\s*(fill|print)\s+(?:an?\s+)?(?:exam(?:ination)?\s+form)\s*[.?!]*\s*$",
        text,
    )
    if _exam_nl:
        _ev = _exam_nl.group(1).lower()
        return Plan(
            action="student_service",
            target="exam_form",
            confidence=0.97,
            reason=f"Exam form intent: {_ev}",
            extra={"service": "exam_form", "exam_intent": _ev},
        )

    # ---- Stage 0-pre-ms: Multi-source knowledge questions ----
    # A compound question spanning two or more university sources ("what is
    # the MCA eligibility, duration and exam fee?") is decomposed into
    # sub-questions and answered by collecting evidence from each authoritative
    # source. This gate runs on the RAW message — before query-understanding
    # preprocessing, which can mangle multi-clause sentences — and fires only
    # for GENUINE multi-source questions (>=2 fragments with >=2 distinct
    # source classes, at least one university source). Single-topic messages
    # return None here and flow through the existing pipeline unmodified, so
    # the dedicated examination / notices / catalogue / comparison / service
    # paths are never hijacked.
    try:
        subs = decompose_query(message, entities, ctx)
        if subs:
            return Plan(
                action="multi_source",
                target=clean,
                confidence=0.86,
                reason=f"Multi-source knowledge question ({len(subs)} sub-queries)",
                extra={
                    "multi_source": [s.as_dict() for s in subs],
                    "original_query": message.strip(),
                    "category": query_category(subs),
                },
            )
    except Exception:
        pass  # decomposition must never break the normal flow

    # ---- Stage 0: Query Understanding preprocessing ----
    # Lightweight normalization, spelling correction, alias expansion.
    # Only run on non-trivial messages (not single option selections).
    if len(text) > 2:
        qr = process_query_understanding(message)
        if qr["corrected"] or qr["expanded"] or qr["confidence"] < 0.9:
            ctx.query_original = qr["original"]
            ctx.query_clean = qr["clean"]
            ctx.query_corrected = qr["corrected"]
            # Use cleaned text for downstream rules
            text = qr["clean"].lower()
            clean = text.rstrip("?.,!;:")
            # Re-extract entities from cleaned text for better routing
            e = extract_entities(text)

    # ---- Stage 0b: Semantic intent classification ----
    # Run semantic intent classifier for debugging and enhanced routing.
    # This runs silently; if it fails, we continue with the existing rules.
    #
    # Skip entirely when the message IS a known navigation label (option id,
    # domain keyword, bare level). These are resolved deterministically and
    # the embedding model only ever distracts (and costs 20-100ms per call).
    _semantic_intent = None
    _semantic_confidence = 0.0
    _semantic_debug = {}
    if not _is_semantic_skippable(text):
        try:
            from app.orchestrator.intent_classifier import classify as classify_semantic
            _semantic_intent, _semantic_confidence, _semantic_debug = classify_semantic(text)
        except Exception:
            pass

    # ---- Stage 0c: Semantic topic enrichment ----
    # If entity extraction didn't find a topic but semantic classifier
    # strongly suggests one, use it. This enables follow-ups like
    # "Cost?" / "How much?" to resolve to "fee" within programme context.
    #
    # CRITICAL GUARD: enrichment is disabled whenever the message is its own
    # navigation label (a bot-rendered option id, a bare programme name, a
    # bare level keyword like "ug"/"undergraduate", or a domain keyword).
    # Otherwise the embedding model warps terse labels into a lookalike topic
    # ("ug" -> "results", "BCA" -> "eligibility", "phd" -> "authorities"),
    # which hijacks deterministic option selection and opens the wrong
    # workflow.
    _semantic_topic_map = {
        "fee": "fee", "eligibility": "eligibility", "scholarships": "fee",
        "datesheet": "dates", "examination": "examination",
        "results": "results", "contact": "contact",
    }
    if e.topic is None and _semantic_intent in _semantic_topic_map and _semantic_enrichment_allowed(text, e):
        e.topic = _semantic_topic_map[_semantic_intent]

    # ---- Stage 0d: Academic scheme awareness ----
    # If the user mentions an academic scheme (NEP 2020 / CBCS), record it
    # in conversation context so routing and RAG can use it. Persists until
    # the user changes it explicitly.
    if e.scheme and ctx.academic_scheme != e.scheme:
        ctx.academic_scheme = e.scheme

    # ---- Stage 0e: Semester awareness ----
    # Numeric semester references ("4th semester") are folded into context so
    # RAG and service routing can be semester-aware. Relative words
    # ("next semester") are resolved in the engine where the student record
    # is available.
    if e.semester is not None and ctx.semester != str(e.semester):
        ctx.semester = str(e.semester)

    # ---- Rule 1: Reset signal ----
    if e.is_reset:
        return Plan(
            action="welcome",
            confidence=1.0,
            reason="User requested reset",
        )

    # ---- Rule 2: Back ----
    if e.is_back:
        response = get_selection_response(chat_id, "back")
        if response.get("type") in ("options", "detail"):
            return Plan(
                action="navigation",
                response=response,
                confidence=1.0,
                reason="User clicked back",
            )
        return Plan(action="welcome", confidence=1.0, reason="No nav path to go back from")

    # ---- Rule 2b: Student Services entry (Student Services hub) ----
    # "student services" / "student service" and the welcome-chip id
    # "student_services" open the authenticated Student Services hub. This
    # deterministic rule runs BEFORE option selection / browsing so the chip
    # id resolves to the gate instead of falling through to RAG or the
    # catalogue. General questions like "What services are available?" are
    # NOT matched here and continue down the normal rules.
    _student_services_entry = clean.replace("_", " ").strip().strip("?.! ")
    if _student_services_entry in ("student services", "student service"):
        return Plan(
            action="student_service",
            target="hub",
            confidence=0.97,
            reason="Student Services hub entry",
            extra={"service": "hub"},
        )

    # ---- Rule 2c: Student Services hub option ids ----
    # The authenticated hub renders chips with ids student_results /
    # student_admit_card / student_exam_form. These must resolve back to the
    # gate/placeholder deterministically, BEFORE the semantic classifier can
    # warp the underscore-joined id into an unrelated topic.
    _student_opt = clean.replace("_", " ").strip().strip("?.! ")
    _student_opt_map = {
        "student results": "results",
        "student admit card": "admit_card",
        "student exam form": "exam_form",
    }
    if _student_opt in _student_opt_map:
        return Plan(
            action="student_service",
            target=_student_opt_map[_student_opt],
            confidence=0.97,
            reason=f"Student Services option: {_student_opt_map[_student_opt]}",
            extra={"service": _student_opt_map[_student_opt]},
        )

    # ---- Rule 2d: Examination roll number (results attempt) ----
    # Supplying your own roll number continues the Results flow; the engine
    # prefills it into the semester + roll form (single-turn, never stored).
    # Only a VALID roll + a personal/imperative frame routes here — a plain
    # "roll numbers" noun or an informational question ("how are examination
    # roll numbers generated?") keeps its normal catalogue/knowledge path.
    if _roll_in_text(clean) and _unsupported_service_request(text) in (
        "results",
        "semester_result",
    ):
        return Plan(
            action="student_service",
            target="results",
            confidence=0.9,
            reason="Examination roll number supplied while requesting results",
            extra={"service": "results", "raw_service": "semester_result"},
        )

    # ---- Rule 2e: Semester-scoped personal results request ----
    # "show my semester 2 results" must reach the Student Results flow BEFORE
    # the catalogue can swallow a semester + "results" message into a
    # programme-less curriculum path. Descriptive queries ("BBA 2nd semester
    # results") are NOT personal requests and keep their normal catalogue route.
    if getattr(e, "semester", None) is not None and _unsupported_service_request(text) in (
        "results",
        "semester_result",
    ):
        return Plan(
            action="student_service",
            target="results",
            confidence=0.93,
            reason="Semester-scoped personal results request",
            extra={"service": "results", "raw_service": "semester_result"},
        )

    # ---- Rule 3: Authority / office intent ----
    # Explicit office questions ("who is registrar", "who handles exams",
    # "registrar office contact") route to the authority card so office
    # nouns are never swallowed by the catalogue or RAG pipeline.
    authority_matches = _detect_authority_intent(text)
    if authority_matches:
        return Plan(
            action="authority",
            target=authority_matches[0].get("department_name", ""),
            confidence=0.85,
            reason=f"Authority intent detected: {authority_matches[0].get('authority_name', '')}",
            extra={"authorities": authority_matches, "original_query": text},
        )

    # ---- Rule 3c: Programme comparison (2+ programmes in one message) ----
    # "difference between BBA and BCA", "BBA vs MCA fee" — must run BEFORE the
    # single-programme catalogue rules so both targets survive. Structured
    # side-by-side data is rendered when both exist in the catalogue; the
    # engine falls back to scoped knowledge retrieval otherwise.
    if len(getattr(e, "programmes", None) or []) >= 2 and not is_college_reference(text):
        _comparison_topic = e.topic
        if _comparison_topic is None:
            try:
                _comparison_topic = detect_catalogue_aspect(text)
            except Exception:
                _comparison_topic = None
        ctx.programmes = list(e.programmes)
        ctx.programme = e.programmes[0]
        ctx.programme_id = e.programmes[0]
        _derive_level_for(ctx, e.programmes[0])
        return Plan(
            action="comparison",
            target="+".join(e.programmes),
            confidence=0.9,
            reason=f"Programme comparison: {' vs '.join(e.programmes)}",
            extra={"programmes": list(e.programmes), "topic": _comparison_topic},
        )

    # ---- Rule 3a: University notices / date sheet ----
    # "date sheet", "time table", "exam schedule", "bca 4th sem date sheet"
    # route to the structured public-notices pipeline (backend/app/notices).
    # This runs BEFORE the catalogue block so "bca datesheet" can never be
    # misread as a bare catalogue-overview request, and a bare "datesheet"
    # (also a registered navigation label) can never fall through to the
    # generic "which programme?" slot-fill — the browser-visible bug that the
    # old picker surfaced. _detect_notice_intent is keyword-gated on exam-
    # schedule vocabulary, so one-word navigation labels like "notices",
    # "examination" and "dates" return None and keep their normal flows.
    # Mode becomes `schedule` when an explicit semester is present (with a
    # programme it is that programme's filtered schedule; without one the
    # whole matching semester sheet is served); otherwise the engine lists
    # published date-sheet notices.
    notice_intent = _detect_notice_intent(text, e)
    if notice_intent:
            _pid = notice_intent.get("programme")
            if _pid:
                ctx.programme = _pid
                ctx.programme_id = _pid
            return Plan(
                action="university_notices",
                target=notice_intent.get("query") or text,
                confidence=notice_intent.get("confidence", 0.85),
                reason=notice_intent.get("reason", "University notices / date-sheet intent"),
                extra=notice_intent,
            )

    # ---- Rule 3aa: Examinations dedicated services ----
    # The Examinations menu hosts three dedicated services (Model Papers, Exam
    # Fee Structure, Division Improvement). They run their OWN structured
    # pipelines — never the generic RAG loop. This gate runs BEFORE the
    # catalogue block so "bca exam fee" / "bca model papers" can never be read
    # as catalogue fee requests, before the news block, and before option
    # selection (Rule 9): clicking these menu chips used to fall through to
    # get_selection_response (no entry -> {"type": "rag"}) and the semantic
    # classifier (model papers -> examination category -> menu loop). The
    # spec's exclusions live in app/examination/detect.py.
    exam_intent = _detect_examination_intent(text, e, message)
    if exam_intent:
        exam_pid = (exam_intent.get("programme") or "").strip().lower() or None
        if exam_pid:
            ctx.programme = exam_pid
            ctx.programme_id = exam_pid
        return Plan(
            action="examination",
            target=exam_intent["target"],
            confidence=exam_intent.get("confidence", 0.9),
            reason=exam_intent.get("reason", f"Examination service: {exam_intent['target']}"),
            extra=exam_intent,
        )

    # ---- Rule 3ab: Official notifications / other official documents ----
    # Student-facing read path over the canonical university_documents
    # repository. Runs AFTER the date-sheet / examination services (so a date
    # sheet or model paper still wins) and BEFORE the catalogue block (so
    # "official documents for MCA" is not misread as a catalogue "documents
    # required" request) and BEFORE the news block (so notification lookups
    # come from the verified repository, never free-form website knowledge).
    # Bare "notice"/"notices"/"circular" keep their existing news / button
    # flows — only notification(s), official notice(s) and official document(s)
    # route here.
    if not (e.word_count <= 1 and is_option_selection(text)):
        official_doc_intent = _detect_official_document_intent(text, e)
        if official_doc_intent:
            return Plan(
                action="official_documents",
                target=official_doc_intent.get("q") or text,
                confidence=official_doc_intent.get("confidence", 0.9),
                reason=official_doc_intent.get(
                    "reason", "Official documents / notifications intent"
                ),
                extra=official_doc_intent,
            )

    # ---- Rule 3b: Academic catalogue (NEP) ----
    # Structured catalogue data (programmes, subjects, VAC/SEC/AEC, credits,
    # outcomes, curriculum docs) takes priority over generic RAG. Detection is
    # strictly data-gated: no matching catalogue records -> None -> the
    # existing pipeline handles the message unchanged.
    try:
        catalogue_req = detect_catalogue_request(text, ctx, e)
        if catalogue_req:
            # Remember the resolved programme on context so conversational
            # follow-ups ("how much?" / "and subjects?") re-use it without the
            # user repeating the programme name.
            _pid = catalogue_req.get("programme")
            if _pid:
                try:
                    _prog = programme_by_id(_pid)
                    if _prog:
                        _code = str(_prog.get("code") or "").lower()
                        if _code:
                            ctx.catalogue_programme_code = _code
                            ctx.programme = _code
                            ctx.programme_id = _code
                except Exception:
                    pass
            return Plan(
                action="catalogue",
                target=catalogue_req.get("op"),
                confidence=0.97,
                reason=f"Catalogue route: {catalogue_req.get('op')}",
                extra={"req": catalogue_req},
            )
        # Bare programme name (word count <= 2): prefer the catalogue overview
        # when the programme exists in the catalogue, else the legacy flow.
        if e.word_count <= 2 and e.programme and not e.topic:
            overview_req = programme_overview_request(e.programme)
            if overview_req:
                try:
                    _prog = programme_by_id(overview_req.get("programme"))
                    if _prog:
                        _code = str(_prog.get("code") or "").lower()
                        if _code:
                            ctx.catalogue_programme_code = _code
                            ctx.programme = _code
                            ctx.programme_id = _code
                except Exception:
                    pass
                return Plan(
                    action="catalogue",
                    target="overview",
                    confidence=0.96,
                    reason=f"Catalogue overview for: {e.programme}",
                    extra={"req": overview_req},
                )
    except Exception:
        pass

    # ---- Rule 3b: News / website knowledge intent ----
    # Current notices, circulars, notifications and the academic calendar
    # live in the synced website knowledge base. Answer from retrieved /
    # summarised knowledge — never show a dead-end menu or a slot-fill loop.
    # Bare navigation labels ("notices" clicked as an option) stay with the
    # existing button flow (Rule 9).
    if not (e.word_count <= 1 and is_option_selection(text)):
        news_query = _detect_news_intent(text)
        if news_query:
            return Plan(
                action="news",
                target=news_query,
                confidence=0.88,
                reason=f"News / website knowledge intent: {news_query}",
                extra={"is_news": True, "original_query": text},
            )

    # ---- Rule 4: Bare programme name (programme switch without topic) ----
    # Detect when user types just a programme name like "MBA" or "BCA"
    # Must come before option selection so it's treated as a switch, not nav.
    if e.word_count <= 2 and e.programme and not e.topic:
        if not ctx.programme or e.programme != ctx.programme:
            detail = lookup_programme(e.programme)
            if detail:
                ctx.programme = e.programme  # Update context pre-emptively
                return Plan(
                    action="structured",
                    response={
                        "type": "detail",
                        "title": detail.get("title", e.programme.upper()),
                        "fields": detail.get("fields", []),
                        "actions": detail.get("actions", _build_actions(e.programme, None)),
                        "context": _build_context_dict(ctx),
                    },
                    target=e.programme,
                    confidence=0.95,
                    reason=f"Bare programme name: {e.programme}",
                )

    # ---- Rule 5: Programme + topic with structured data (MUST come before option selection) ----
    # When context has an active programme and message is a known topic,
    # structured lookup takes priority over navigation. Skipped when the message
    # names a DIFFERENT programme — that is a switch handled by Rule 5b/12.
    if ctx.programme and e.topic and not (e.programme and e.programme != ctx.programme):
        # Fee type disambiguation: if fee_type is explicitly "examination", route to examination service
        if e.topic == "fee" and getattr(ctx, "fee_type", None) == "examination":
            return Plan(
                action="examination",
                target="fee_structure_exam",
                confidence=0.95,
                reason=f"Examination fee for {ctx.programme} (from context fee_type)",
                extra={"programme": ctx.programme},
            )
        value = lookup_field(ctx.programme, e.topic)
        if value:
            detail = lookup_programme(ctx.programme)
            title = detail.get("title", ctx.programme.upper()) if detail else ctx.programme.upper()
            fields = _build_topic_fields(e.topic, value, ctx.programme)
            return Plan(
                action="structured",
                response={
                    "type": "detail",
                    "title": f"{title} — {e.topic.replace('_', ' ').title()}",
                    "fields": fields,
                    "actions": _build_actions(ctx.programme, e.topic),
                    "context": _build_context_dict(ctx),
                },
                target=f"{ctx.programme}/{e.topic}",
                confidence=0.98,
                reason=f"Structured field lookup: {ctx.programme}/{e.topic}",
                )

    # ---- Rule 5b: Programme + topic in the SAME message → direct answer ----
    # Conversational assistant: when a single message carries both the programme
    # and the topic, answer directly instead of showing navigation buttons.
    # (e.g. "Fee structure of BCA", "What is the eligibility for B.Sc Computer
    # Science?"). Also persists the programme into conversation memory so
    # follow-ups ("what about the fee?") resolve to the same programme.
    if e.programme and e.topic and not is_college_reference(text):
        ctx.programme = e.programme
        ctx.programme_id = e.programme
        _derive_level_for(ctx, e.programme)
        # Fee type disambiguation: explicit examination fee or context fee_type
        if e.topic == "fee" and (detect_fee_type(message, e, ctx) == "examination" or getattr(ctx, "fee_type", None) == "examination"):
            return Plan(
                action="examination",
                target="fee_structure_exam",
                confidence=0.95,
                reason=f"Examination fee for {e.programme} (explicit or context)",
                extra={"programme": e.programme},
            )
        value = lookup_field(e.programme, e.topic)
        if value:
            detail = lookup_programme(e.programme)
            title = detail.get("title", e.programme.upper()) if detail else e.programme.upper()
            fields = _build_topic_fields(e.topic, value, e.programme)
            return Plan(
                action="structured",
                response={
                    "type": "detail",
                    "title": f"{title} — {e.topic.replace('_', ' ').title()}",
                    "fields": fields,
                    "actions": _build_actions(e.programme, e.topic),
                    "context": _build_context_dict(ctx),
                },
                target=f"{e.programme}/{e.topic}",
                confidence=0.98,
                reason=f"Programme + topic direct answer: {e.programme}/{e.topic}",
            )
        return Plan(
            action="rag",
            target=_augment_for_rag_from_prog(e.programme, e.topic),
            confidence=0.85,
            reason=f"Programme + topic (no structured data): {e.programme}/{e.topic}",
            extra={"original_query": text, "augmented_query": _augment_for_rag_from_prog(e.programme, e.topic)},
        )

    # ---- Rule 6: College context pre-check (before option selection) ----
    # College rules must come before option selection / broad keywords so that
    # queries like "fee" within college context show college-specific data.
    college_ref = is_college_reference(text)
    if college_ref:
        college_id = resolve_college(text)
        college = _college_svc.get_college(college_id) if college_id else None
        if college:
            college_topic = None
            if e.topic:
                college_topic = e.topic
            if not college_topic:
                for kw, mapped in COLLEGE_TOPICS.items():
                    if kw in text:
                        college_topic = mapped
                        break
            if college_topic:
                if college_topic == "fee":
                    # Fee type disambiguation: if explicitly examination fee, route to examination service
                    if detect_fee_type(message, e, ctx) == "examination" or getattr(ctx, "fee_type", None) == "examination":
                        return Plan(
                            action="examination",
                            target="fee_structure_exam",
                            confidence=0.9,
                            reason=f"Examination fee in college context ({college_id})",
                            extra={"programme": ctx.programme},
                        )
                    fees = _college_svc.get_fees(college_id)
                    return Plan(
                        action="structured",
                        response={
                            "type": "detail",
                            "title": f"{college['name']} — Fee Structure",
                            "fields": [{"label": p.upper(), "value": f} for p, f in fees.items()] if fees else [{"label": "Fee", "value": "Contact college for current fee structure"}],
                            "actions": _build_college_actions(college_id, college_topic),
                        },
                        target=f"college/{college_id}/{college_topic}",
                        confidence=0.95,
                        reason=f"College + topic: {college_id}/{college_topic}",
                        extra={"college_id": college_id},
                    )
                if college_topic == "departments":
                    depts = _college_svc.get_departments(college_id)
                    return Plan(
                        action="structured",
                        response={
                            "type": "detail",
                            "title": f"{college['name']} — Departments",
                            "fields": [{"label": "Department", "value": d} for d in depts] if depts else [{"label": "Departments", "value": "Information not available"}],
                            "actions": _build_college_actions(college_id, college_topic),
                        },
                        target=f"college/{college_id}/departments",
                        confidence=0.95,
                        reason=f"College departments: {college_id}",
                        extra={"college_id": college_id},
                    )
                if college_topic == "courses":
                    progs = _college_svc.get_programmes(college_id)
                    options = [{"id": p["id"], "label": p["name"]} for p in progs] if progs else []
                    return Plan(
                        action="navigation",
                        response={
                            "type": "options",
                            "title": f"{college['name']} — Programmes",
                            "message": "Select a programme to see details.",
                            "options": options,
                            "context": {"breadcrumbs": [college["name"], "Programmes"]},
                        },
                        target=f"college/{college_id}/courses",
                        confidence=0.95,
                        reason=f"College programmes: {college_id}",
                        extra={"college_id": college_id},
                    )
                if college_topic == "about":
                    return Plan(
                        action="structured",
                        response={
                            "type": "detail",
                            "title": college["name"],
                            "fields": [
                                {"label": "About", "value": college.get("about", "")},
                                {"label": "Type", "value": college.get("type", "").title()},
                                {"label": "Established", "value": str(college.get("established", "N/A"))},
                                {"label": "NAAC Grade", "value": college.get("naac", "N/A")},
                                {"label": "Principal", "value": college.get("principal", "N/A")},
                                {"label": "Address", "value": college.get("address", "")},
                                {"label": "District", "value": college.get("district", "")},
                            ],
                            "actions": _build_college_actions(college_id, None),
                        },
                        target=f"college/{college_id}/about",
                        confidence=0.95,
                        reason=f"College about: {college_id}",
                        extra={"college_id": college_id},
                    )
                if college_topic == "contact":
                    contact = _college_svc.get_contact(college_id)
                    fields = [{"label": k.replace("_", " ").title(), "value": v} for k, v in contact.items()] if contact else []
                    return Plan(
                        action="structured",
                        response={
                            "type": "detail",
                            "title": f"{college['name']} — Contact",
                            "fields": fields,
                            "actions": _build_college_actions(college_id, "contact"),
                        },
                        target=f"college/{college_id}/contact",
                        confidence=0.95,
                        reason=f"College contact: {college_id}",
                        extra={"college_id": college_id},
                    )
                if college_topic == "facilities":
                    facilities = _college_svc.get_facilities(college_id)
                    fields = [{"label": "Facility", "value": f} for f in facilities] if facilities else [{"label": "Facilities", "value": "Information not available"}]
                    return Plan(
                        action="structured",
                        response={
                            "type": "detail",
                            "title": f"{college['name']} — Facilities",
                            "fields": fields,
                            "actions": _build_college_actions(college_id, "facilities"),
                        },
                        target=f"college/{college_id}/facilities",
                        confidence=0.95,
                        reason=f"College facilities: {college_id}",
                        extra={"college_id": college_id},
                    )
                if college_topic == "eligibility":
                    el = _college_svc.get_eligibility(college_id)
                    fields = [{"label": k.upper(), "value": v} for k, v in el.items()] if isinstance(el, dict) else [{"label": "Eligibility", "value": str(el)}]
                    return Plan(
                        action="structured",
                        response={
                            "type": "detail",
                            "title": f"{college['name']} — Eligibility",
                            "fields": fields,
                            "actions": _build_college_actions(college_id, "eligibility"),
                        },
                        target=f"college/{college_id}/eligibility",
                        confidence=0.95,
                        reason=f"College eligibility: {college_id}",
                        extra={"college_id": college_id},
                    )
                if college_topic == "principal":
                    return Plan(
                        action="structured",
                        response={
                            "type": "detail",
                            "title": f"{college['name']} — Principal",
                            "fields": [{"label": "Principal", "value": college.get("principal", "Not available")}],
                            "actions": _build_college_actions(college_id, None),
                        },
                        target=f"college/{college_id}/principal",
                        confidence=0.95,
                        reason=f"College principal: {college_id}",
                        extra={"college_id": college_id},
                    )
                return Plan(
                    action="rag",
                    target=f"{college['name']} {college_topic.replace('_', ' ')}",
                    confidence=0.7,
                    reason=f"College + topic (fallback to RAG): {college_id}/{college_topic}",
                    extra={"college_id": college_id},
                )
            overview = _college_svc.get_overview(college_id)
            if overview:
                fields = [
                    {"label": "Type", "value": overview.get("type", "").title()},
                    {"label": "Established", "value": str(overview.get("established", "N/A"))},
                    {"label": "NAAC Grade", "value": overview.get("naac", "N/A")},
                    {"label": "Principal", "value": overview.get("principal", "N/A")},
                    {"label": "District", "value": overview.get("district", "")},
                ]
                if overview.get("phone"):
                    fields.append({"label": "Phone", "value": overview["phone"]})
                if overview.get("email"):
                    fields.append({"label": "Email", "value": overview["email"]})
                return Plan(
                    action="structured",
                    response={
                        "type": "detail",
                        "title": college["name"],
                        "fields": fields,
                        "actions": _build_college_actions(college_id, None),
                        "context": {"breadcrumbs": [college["name"]], "college": college_id},
                    },
                    target=f"college/{college_id}",
                    confidence=0.95,
                    reason=f"College selected: {college_id}",
                    extra={"college_id": college_id},
                )

    # ---- Rule 7: Active college context + short follow-up ----
    if ctx.college and _is_college_followup(text, e, ctx):
        college = _college_svc.get_college(ctx.college)
        college_name = college["name"] if college else ctx.college_name or ctx.college
        for kw, mapped in COLLEGE_TOPICS.items():
            if _word_in_text(kw, text):
                if mapped == "fee":
                    # Fee type disambiguation: if explicitly examination fee, route to examination service
                    if detect_fee_type(message, e, ctx) == "examination" or getattr(ctx, "fee_type", None) == "examination":
                        return Plan(
                            action="examination",
                            target="fee_structure_exam",
                            confidence=0.9,
                            reason=f"Examination fee follow-up in college context ({ctx.college})",
                            extra={"programme": ctx.programme},
                        )
                    fees = _college_svc.get_fees(ctx.college)
                    return Plan(
                        action="structured",
                        response={
                            "type": "detail",
                            "title": f"{college_name} — Fee Structure",
                            "fields": [{"label": p.upper(), "value": f} for p, f in fees.items()] if fees else [{"label": "Fee", "value": "Contact college"}],
                            "actions": _build_college_actions(ctx.college, "fee"),
                            "context": {"breadcrumbs": [college_name, "Fee"]},
                        },
                        target=f"college/{ctx.college}/fee",
                        confidence=0.95,
                        reason=f"College context follow-up: {ctx.college}/{mapped}",
                        extra={"college_id": ctx.college},
                    )
                if mapped == "departments":
                    depts = _college_svc.get_departments(ctx.college)
                    return Plan(
                        action="structured",
                        response={
                            "type": "detail",
                            "title": f"{college_name} — Departments",
                            "fields": [{"label": "Department", "value": d} for d in depts] if depts else [],
                            "actions": _build_college_actions(ctx.college, "departments"),
                            "context": {"breadcrumbs": [college_name, "Departments"]},
                        },
                        target=f"college/{ctx.college}/departments",
                        confidence=0.95,
                        reason=f"College departments follow-up: {ctx.college}",
                        extra={"college_id": ctx.college},
                    )
                if mapped == "courses":
                    progs = _college_svc.get_programmes(ctx.college)
                    options = [{"id": p["id"], "label": p["name"]} for p in progs] if progs else []
                    return Plan(
                        action="navigation",
                        response={
                            "type": "options",
                            "title": f"{college_name} — Programmes",
                            "message": "Select a programme to see details.",
                            "options": options,
                            "context": {"breadcrumbs": [college_name, "Programmes"]},
                        },
                        target=f"college/{ctx.college}/courses",
                        confidence=0.95,
                        reason=f"College programmes follow-up: {ctx.college}",
                        extra={"college_id": ctx.college},
                    )
                if mapped == "contact":
                    contact = _college_svc.get_contact(ctx.college)
                    fields = [{"label": k.replace("_", " ").title(), "value": v} for k, v in contact.items()] if contact else []
                    return Plan(
                        action="structured",
                        response={
                            "type": "detail",
                            "title": f"{college_name} — Contact",
                            "fields": fields,
                            "actions": _build_college_actions(ctx.college, "contact"),
                            "context": {"breadcrumbs": [college_name, "Contact"]},
                        },
                        target=f"college/{ctx.college}/contact",
                        confidence=0.95,
                        reason=f"College contact follow-up: {ctx.college}",
                        extra={"college_id": ctx.college},
                    )
                if mapped == "principal":
                    return Plan(
                        action="structured",
                        response={
                            "type": "detail",
                            "title": f"{college_name} — Principal",
                            "fields": [{"label": "Principal", "value": college.get("principal", "Not available") if college else "N/A"}],
                            "actions": _build_college_actions(ctx.college, None),
                            "context": {"breadcrumbs": [college_name, "Principal"]},
                        },
                        target=f"college/{ctx.college}/principal",
                        confidence=0.95,
                        reason=f"College principal follow-up: {ctx.college}",
                        extra={"college_id": ctx.college},
                    )
                if mapped == "facilities":
                    facilities = _college_svc.get_facilities(ctx.college)
                    fields = [{"label": "Facility", "value": f} for f in facilities] if facilities else []
                    return Plan(
                        action="structured",
                        response={
                            "type": "detail",
                            "title": f"{college_name} — Facilities",
                            "fields": fields,
                            "actions": _build_college_actions(ctx.college, "facilities"),
                            "context": {"breadcrumbs": [college_name, "Facilities"]},
                        },
                        target=f"college/{ctx.college}/facilities",
                        confidence=0.95,
                        reason=f"College facilities follow-up: {ctx.college}",
                        extra={"college_id": ctx.college},
                    )
                if mapped == "about":
                    return Plan(
                        action="structured",
                        response={
                            "type": "detail",
                            "title": college_name,
                            "fields": [
                                {"label": "About", "value": college.get("about", "") if college else ""},
                                {"label": "Type", "value": college.get("type", "").title() if college else ""},
                                {"label": "Established", "value": str(college.get("established", "N/A")) if college else "N/A"},
                                {"label": "NAAC Grade", "value": college.get("naac", "N/A") if college else "N/A"},
                            ],
                            "actions": _build_college_actions(ctx.college, None),
                            "context": {"breadcrumbs": [college_name, "About"]},
                        },
                        target=f"college/{ctx.college}/about",
                        confidence=0.95,
                        reason=f"College about follow-up: {ctx.college}",
                        extra={"college_id": ctx.college},
                    )
                if mapped == "eligibility":
                    el = _college_svc.get_eligibility(ctx.college)
                    fields = [{"label": k.upper(), "value": v} for k, v in el.items()] if isinstance(el, dict) else [{"label": "Eligibility", "value": str(el)}]
                    return Plan(
                        action="structured",
                        response={
                            "type": "detail",
                            "title": f"{college_name} — Eligibility",
                            "fields": fields,
                            "actions": _build_college_actions(ctx.college, "eligibility"),
                            "context": {"breadcrumbs": [college_name, "Eligibility"]},
                        },
                        target=f"college/{ctx.college}/eligibility",
                        confidence=0.95,
                        reason=f"College eligibility follow-up: {ctx.college}",
                        extra={"college_id": ctx.college},
                    )
                if mapped == "notices":
                    return Plan(
                        action="rag",
                        target=f"{college_name} notices notifications",
                        confidence=0.7,
                        reason=f"College notices (RAG): {ctx.college}",
                        extra={"college_id": ctx.college},
                    )
                if mapped == "prospectus":
                    return Plan(
                        action="rag",
                        target=f"{college_name} prospectus admission brochure",
                        confidence=0.7,
                        reason=f"College prospectus (RAG): {ctx.college}",
                        extra={"college_id": ctx.college},
                    )
                return Plan(
                    action="rag",
                    target=f"{college_name} {mapped.replace('_', ' ')}",
                    confidence=0.7,
                    reason=f"College context follow-up: {ctx.college}/{mapped} (RAG)",
                    extra={"college_id": ctx.college},
                )
        return Plan(
            action="rag",
            target=f"{college_name} {text}",
            confidence=0.65,
            reason=f"College context general follow-up: {ctx.college}",
            extra={"college_id": ctx.college},
        )

    # ---- Rule 8: Active college + programme option selection ----
    if ctx.college and e.programme and _college_svc.has_programme(ctx.college, e.programme):
        college = _college_svc.get_college(ctx.college)
        college_name = college["name"] if college else ctx.college_name or ctx.college
        prog_fees = _college_svc.get_programme_fees(ctx.college, e.programme)
        fields = [{"label": "Programme", "value": e.programme.upper()}]
        if prog_fees:
            fields.append({"label": "Fee", "value": prog_fees})
        return Plan(
            action="structured",
            response={
                "type": "detail",
                "title": f"{college_name} — {e.programme.upper()}",
                "fields": fields,
                "actions": [
                    {"id": "fee", "label": "Fee Details"},
                    {"id": "eligibility", "label": "Eligibility"},
                    {"id": "contact", "label": "Contact"},
                ],
                "context": {"breadcrumbs": [college_name, e.programme.upper()], "college": ctx.college},
            },
            target=f"college/{ctx.college}/{e.programme}",
            confidence=0.9,
            reason=f"College programme: {ctx.college}/{e.programme}",
            extra={"college_id": ctx.college},
        )

    # ---- Rule 9a: College-course discovery ----
    # "which colleges offer BCA" / "what courses are offered in GCW"
    # Detect college-course mapping queries
    course_discovery_match = _detect_course_college_query(text, ctx)
    if course_discovery_match:
        qtype = course_discovery_match["type"]
        if qtype == "course_to_colleges":
            pid = course_discovery_match["programme_id"]
            colleges_list = college_course_map.get_colleges_for_programme(pid)
            if colleges_list:
                ctx.last_selected_entity = "programme"
                options = [{"id": c["id"], "label": f"{c['name']} ({c['district']})"} for c in colleges_list]
                from app.orchestrator.context import _get_programme_label
                prog_name = _get_programme_label(pid) or pid.upper()
                return Plan(
                    action="navigation",
                    response={
                        "type": "options",
                        "title": f"Colleges offering {prog_name}",
                        "message": f"The following colleges offer {prog_name}:",
                        "options": options,
                        "context": {"breadcrumbs": [prog_name, "Colleges"]},
                    },
                    target=f"course/{pid}/colleges",
                    confidence=0.95,
                    reason=f"Course→colleges: {pid}",
                )
            return Plan(
                action="rag",
                target=f"which colleges offer {pid}",
                confidence=0.6,
                reason=f"Course→colleges fallback (no data): {pid}",
            )

        if qtype == "college_to_courses":
            cid = course_discovery_match["college_id"]
            college = _college_svc.get_college(cid)
            if college:
                programmes = college_course_map.get_college_programmes(cid)
                options = [{"id": p["id"], "label": p["name"]} for p in programmes]
                ctx.last_selected_entity = "college"
                return Plan(
                    action="navigation",
                    response={
                        "type": "options",
                        "title": f"Programmes at {college['name']}",
                        "message": "Select a programme to see details.",
                        "options": options,
                        "context": {"breadcrumbs": [college['name'], "Programmes"]},
                    },
                    target=f"college/{cid}/courses",
                    confidence=0.95,
                    reason=f"College→courses: {cid}",
                    extra={"college_id": cid},
                )

    # ---- Rule 9b: Follow-up with active programme context and college context ----
    # When user has both active programme AND college: fee/eligibility/documents/etc
    # should apply to the college's programme, not the generic programme.
    if ctx.college and ctx.programme and e.topic:
        # Check if the college has this programme
        if college_course_map.has_college_programme(ctx.college, ctx.programme):
            # Fee type disambiguation
            if e.topic == "fee" and (detect_fee_type(message, e, ctx) == "examination" or getattr(ctx, "fee_type", None) == "examination"):
                return Plan(
                    action="examination",
                    target="fee_structure_exam",
                    confidence=0.95,
                    reason=f"Examination fee for college {ctx.college} programme {ctx.programme}",
                    extra={"programme": ctx.programme, "college_id": ctx.college},
                )
            value = lookup_field(ctx.programme, e.topic)
            if value:
                college = _college_svc.get_college(ctx.college)
                college_name = college["name"] if college else ctx.college_name or ctx.college
                fields = _build_topic_fields(e.topic, value, ctx.programme)
                return Plan(
                    action="structured",
                    response={
                        "type": "detail",
                        "title": f"{college_name} — {ctx.programme.upper()} — {e.topic.replace('_', ' ').title()}",
                        "fields": fields,
                        "actions": _build_actions(ctx.programme, e.topic),
                        "context": {"breadcrumbs": [college_name, ctx.programme.upper(), e.topic.replace('_', ' ').title()]},
                    },
                    target=f"college/{ctx.college}/{ctx.programme}/{e.topic}",
                    confidence=0.95,
                    reason=f"College+programme+topic: {ctx.college}/{ctx.programme}/{e.topic}",
                    extra={"college_id": ctx.college},
                )

    # ---- Rule 9: Option selection (known ID) ----
    # A bare concrete topic keyword ("fee", "eligibility") without any
    # programme is a conversational ask, not a button click: let Rule 10b
    # slot-fill the missing programme instead of opening browse options.
    _bare_topic_ask = e.topic and not e.programme and not ctx.programme and e.word_count <= 2
    if is_option_selection(text) and not _bare_topic_ask:
        response = get_selection_response(chat_id, text)
        if response.get("type") in ("options", "detail"):
            return Plan(
                action="navigation",
                response=response,
                confidence=0.95,
                reason=f"Option selection: {text}",
            )

    # ---- Rule 9b: Bare level keyword → level navigation ----
    # The extractor already recognises "undergraduate", "pg", "phd", etc. as a
    # level entity, but the planner never used it — so these messages fell into
    # the semantic classifier, which warps them into unrelated categories
    # ("ug" -> "results", "undergraduate" -> "colleges"). Route a bare level
    # keyword to its own navigation response deterministically.
    if (
        e.level is not None
        and not e.programme
        and not e.topic
        and e.word_count <= 2
        and not ctx.college
    ):
        level_response = get_broad_response(e.level)
        if level_response.get("type") in ("options", "detail"):
            return Plan(
                action="navigation",
                response=level_response,
                target=e.level,
                confidence=0.92,
                reason=f"Level keyword: {e.level}",
            )

    # ---- Rule 10a: Unsupported / gated personal student-service request ----
    # A direct request for a service the bot cannot perform (student results,
    # admit card, attendance, marks, transcript, ...) must not degrade into a
    # programme picker / slot-fill that falsely implies the service exists.
    # Families like attendance / hall ticket / fee receipt stay a plain
    # "unavailable" answer. The Step-1 gated families (results / admit card /
    # exam form) instead enter the Student Services auth gate. Informational
    # questions about any of these are intentionally NOT matched here and
    # continue to the knowledge / RAG path below.
    _unavailable_service = _unsupported_service_request(text)
    if _unavailable_service:
        # "semester result" (contiguous phrase) is the same personal service as
        # "results" — fold it into the results family so it reaches the same
        # auth gate + result flow instead of a dead "unavailable" answer.
        gated_target = (
            "results"
            if _unavailable_service in ("results", "semester_result")
            else _unavailable_service
        )
        if gated_target in _GATED_SERVICE_FAMILIES:
            return Plan(
                action="student_service",
                target=gated_target,
                confidence=0.95,
                reason=f"Student service request requires sign-in: {gated_target}",
                extra={"service": gated_target, "raw_service": _unavailable_service},
            )
        return Plan(
            action="unavailable_service",
            target=_unavailable_service,
            confidence=0.95,
            reason=f"Unsupported student-service request: {_unavailable_service}",
            extra={"service": _unavailable_service},
        )

    # ---- Rule 10b-1: Fee ambiguity clarification ----
    # When the user asks about "fee" without specifying which fee type
    # (programme/course/tuition vs examination), and no fee_type can be
    # inferred from the message or inherited from context, ask for clarification.
    # Explicit fee types: "examination fee" -> examination, "programme fee" -> programme.
    if e.topic == "fee" or ctx.topic == "fee":
        fee_type = detect_fee_type(message, e, ctx)
        if fee_type is None:
            # Check if we have a programme context to disambiguate
            # If no programme anywhere, let Rule 10b handle "which programme?"
            # If we have a programme but fee_type is still ambiguous, clarify fee type.
            has_programme = bool(e.programme or ctx.programme or ctx.catalogue_programme_code)
            if has_programme:
                return Plan(
                    action="clarify",
                    target="fee_type",
                    confidence=0.85,
                    reason=f"Fee type ambiguous for programme {e.programme or ctx.programme} — clarify programme vs examination fee",
                    extra={"slot": "fee_type", "pending_topic": "fee", "pending_programme": e.programme or ctx.programme},
                )
        else:
            # Fee type was determined (explicit or from context). Route accordingly.
            # If examination fee, route to examination service (works without programme).
            # If programme fee, fall through to programme slot-fill (needs programme).
            if fee_type == "examination":
                return Plan(
                    action="examination",
                    target="fee_structure_exam",
                    confidence=0.9,
                    reason=f"Examination fee (fee_type={fee_type} from {'context' if getattr(ctx, 'fee_type', None) == 'examination' else 'explicit message'})",
                    extra={"programme": e.programme or ctx.programme},
                )

    # ---- Rule 10b: Topic without programme → targeted slot-fill question ----
    # A concrete topic (fee, eligibility, duration, documents, ...) with no
    # programme anywhere (message, context, domain, college) can't be answered
    # directly. Instead of browse buttons, ask the single missing slot and
    # remember the pending topic so the next message continues the request.
    # Unsupported personal-service topics (results, attendance) are excluded:
    # they have no data behind them and must never pretend to offer lookup.
    if (
        e.topic
        and e.topic not in _UNSUPPORTED_TOPICS
        and not e.programme
        and not ctx.programme
        and not ctx.college
        and not ctx.domain
    ):
        return Plan(
            action="slot_fill",
            target="programme",
            confidence=0.88,
            reason=f"Topic '{e.topic}' without programme — targeted slot question",
            extra={"slot": "programme", "pending_topic": e.topic},
        )

    # ---- Rule 10: Broad keyword / semantic intent ----
    intent_type, category = classify_nav(text)
    if intent_type == "broad" and category:
        response = get_broad_response(category)
        if response.get("type") in ("options", "detail"):
            extra = {}
            if _semantic_intent and _semantic_intent != category:
                extra["semantic_intent"] = _semantic_intent
                extra["semantic_confidence"] = _semantic_confidence
            return Plan(
                action="navigation",
                response=response,
                target=category,
                confidence=0.9,
                reason=f"Broad intent: {category}",
                extra=extra,
            )

    # ---- Rule 11: Programme + topic without structured data → RAG ----
    # Skipped when the message names a different programme (switch → Rule 5b/12).
    if ctx.programme and e.topic and not (e.programme and e.programme != ctx.programme):
        # Fee type disambiguation: if fee_type is "examination", route to examination service
        if e.topic == "fee" and getattr(ctx, "fee_type", None) == "examination":
            return Plan(
                action="examination",
                target="fee_structure_exam",
                confidence=0.9,
                reason=f"Examination fee for {ctx.programme} (context fee_type, no structured data)",
                extra={"programme": ctx.programme},
            )
        # No structured data found, try RAG
        augmented = _augment_for_rag(ctx, e.topic, e)
        return Plan(
            action="rag",
            target=augmented,
            confidence=0.85,
            reason=f"Programme {ctx.programme} + topic {e.topic} (no structured data)",
            extra={"original_query": text, "augmented_query": augmented},
        )

    # ---- Rule 12: Programme switch detected (with topic, already handled bare name in Rule 4) ----
    prog_switch = detect_programme_switch(message)
    if prog_switch and (not ctx.programme or prog_switch != ctx.programme):
        # If the switch is accompanied by a topic, handle as structured
        if e.topic:
            # Fee type disambiguation for programme switch
            if e.topic == "fee" and (detect_fee_type(message, e, ctx) == "examination" or getattr(ctx, "fee_type", None) == "examination"):
                return Plan(
                    action="examination",
                    target="fee_structure_exam",
                    confidence=0.95,
                    reason=f"Examination fee for {prog_switch} (explicit or context)",
                    extra={"programme": prog_switch},
                )
            value = lookup_field(prog_switch, e.topic)
            if value:
                detail = lookup_programme(prog_switch)
                title = detail.get("title", prog_switch.upper()) if detail else prog_switch.upper()
                fields = _build_topic_fields(e.topic, value, prog_switch)
                return Plan(
                    action="structured",
                    response={
                        "type": "detail",
                        "title": f"{title} — {e.topic.replace('_', ' ').title()}",
                        "fields": fields,
                        "actions": _build_actions(prog_switch, e.topic),
                        "context": _build_context_dict(ctx),
                    },
                    target=f"{prog_switch}/{e.topic}",
                    confidence=0.95,
                    reason=f"Programme switch to {prog_switch} with topic {e.topic}",
                )
            return Plan(
                action="rag",
                target=_augment_for_rag_from_prog(prog_switch, e.topic),
                confidence=0.85,
                reason=f"Programme switch to {prog_switch} + topic {e.topic} (no structured data)",
            )
        # Pure programme switch → show programme details
        detail = lookup_programme(prog_switch)
        if detail:
            return Plan(
                action="structured",
                response={
                    "type": "detail",
                    "title": detail.get("title", prog_switch.upper()),
                    "fields": detail.get("fields", []),
                    "actions": detail.get("actions", _build_actions(prog_switch, None)),
                    "context": _build_context_dict(ctx),
                },
                target=prog_switch,
                confidence=0.95,
                reason=f"Programme switch to {prog_switch}",
            )
        return Plan(
            action="navigation",
            target=prog_switch,
            confidence=0.8,
            reason=f"Programme switch to {prog_switch} (fallback to nav)",
        )

    # ---- Rule 13: Level + topic ----

    # ---- Rule 14: Short follow-up with programme context ----
    if ctx.programme and _is_short_followup(text, e):
        # Fee type disambiguation: if fee_type is "examination", route to examination service
        if (e.topic == "fee" or ctx.topic == "fee") and getattr(ctx, "fee_type", None) == "examination":
            return Plan(
                action="examination",
                target="fee_structure_exam",
                confidence=0.85,
                reason=f"Examination fee follow-up for {ctx.programme} (context fee_type)",
                extra={"programme": ctx.programme},
            )
        augmented = _augment_for_rag(ctx, clean, e)
        return Plan(
            action="rag",
            target=augmented,
            confidence=0.75,
            reason=f"Short follow-up with programme {ctx.programme}: '{text}'",
            extra={"original_query": text, "augmented_query": augmented},
        )

    # ---- Rule 15: Broad question with context → RAG ----
    if e.is_question and ctx.programme:
        # Fee type disambiguation: if fee_type is "examination", route to examination service
        if (e.topic == "fee" or ctx.topic == "fee") and getattr(ctx, "fee_type", None) == "examination":
            return Plan(
                action="examination",
                target="fee_structure_exam",
                confidence=0.8,
                reason=f"Examination fee question for {ctx.programme} (context fee_type)",
                extra={"programme": ctx.programme},
            )
        augmented = _augment_for_rag(ctx, text, e)
        return Plan(
            action="rag",
            target=augmented,
            confidence=0.7,
            reason=f"Question with programme context: {text}",
            extra={"original_query": text, "augmented_query": augmented},
        )

    # ---- Rule 16: Broad question without context → RAG ----
    if e.is_question and not ctx.domain:
        return Plan(
            action="rag",
            target=text,
            confidence=0.6,
            reason=f"Question without context, using RAG: {text}",
        )

    # ---- Rule 17: Short ambiguous → clarify ----
    if e.word_count <= 3 and not ctx.domain:
        # See if it matches any domain
        return Plan(
            action="clarify",
            target="domain",
            confidence=0.5,
            reason=f"Short ambiguous message: '{text}'",
        )

    # ---- Rule 18: Everything else → RAG ----
    augmented = _augment_for_rag(ctx, text, e) if ctx.programme else text
    extra = {"original_query": text, "augmented_query": augmented}
    if _semantic_intent and _semantic_intent != "unknown":
        extra["semantic_intent"] = _semantic_intent
        extra["semantic_confidence"] = _semantic_confidence
    return Plan(
        action="rag",
        target=augmented,
        confidence=0.5,
        reason="Fallback to RAG",
        extra=extra,
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_KNOWN_UG_PROGRAMMES = {"ba", "bsc", "bcom", "bba", "bca", "btech", "bed"}
_KNOWN_PG_PROGRAMMES = {"ma", "msc", "mcom", "mba", "mca", "med"}


def _derive_level_for(ctx: ConversationContext, programme: str) -> None:
    """Derive the academic level (ug/pg/phd) from a programme ID."""
    if programme in _KNOWN_UG_PROGRAMMES:
        ctx.level = "ug"
    elif programme in _KNOWN_PG_PROGRAMMES:
        ctx.level = "pg"
    elif programme == "phd":
        ctx.level = "phd"


# Labels that are resolved deterministically — the semantic classifier must
# never run for them (it misclassifies terse tokens: "ug" -> results, "phd" ->
# authorities) and skipping it saves an embedding call on every nav click.
_NAV_LABEL_TOKENS = DOMAIN_KEYWORDS | {
    "undergraduate", "under graduate", "postgraduate", "post graduate",
    "doctorate", "design your degree",
}


def _is_semantic_skippable(text: str) -> bool:
    """True when a message is its own navigation label (no semantic needed)."""
    clean = text.strip().lower().rstrip("?.,!;:")
    return clean in _NAV_LABEL_TOKENS or is_option_selection(text)


def _semantic_enrichment_allowed(text: str, e: Any) -> bool:
    """Whether the semantic classifier may inject a topic into this message.

    Disabled whenever the message is itself a resolved navigation label
    (a known option id, a bare programme, a bare level keyword, a domain
    keyword, or an existing explicit service). These tokens are ground truth
    and must route deterministically, not through embedding similarity.
    """
    if e.programme or e.level or e.service:
        return False
    if is_option_selection(text):
        return False
    clean = text.strip().lower().rstrip("?.,!;:")
    if clean in DOMAIN_KEYWORDS:
        return False
    return True


def _is_short_followup(text: str, entities: Any) -> bool:
    """Check if this is a short follow-up that benefits from context."""
    if entities.word_count > 4:
        return False
    if entities.is_question:
        return False
    clean = text.rstrip("?.,!;:")
    if clean in DOMAIN_KEYWORDS:
        return False
    return not (entities.word_count == 1 and clean in PROGRAMME_ALIASES)


def _build_topic_fields(topic: str, value: str, programme: str) -> list[dict[str, str]]:
    """Build a fields list for a topic-specific detail response."""
    label_map = {
        "fee": "Fee Structure",
        "eligibility": "Eligibility Criteria",
        "duration": "Programme Duration",
        "admission_mode": "Admission Mode",
        "documents": "Documents Required",
        "specializations": "Specializations",
        "syllabus": "Syllabus",
        "dates": "Important Dates",
        "prospectus": "Prospectus",
        "seats": "Seats / Intake",
        "placement": "Placement",
        "career": "Career Options",
    }
    label = label_map.get(topic, topic.replace("_", " ").title())
    return [{"label": label, "value": value}]


def _build_actions(programme: str | None, current_topic: str | None) -> list[dict[str, str]]:
    """Build action buttons, excluding the current topic."""
    all_actions = [
        {"id": "fee", "label": "Fee Structure"},
        {"id": "eligibility", "label": "Eligibility"},
        {"id": "duration", "label": "Duration"},
        {"id": "dates", "label": "Important Dates"},
        {"id": "documents", "label": "Documents"},
        {"id": "prospectus", "label": "Prospectus"},
    ]
    if current_topic:
        return [a for a in all_actions if a["id"] != current_topic]
    return all_actions


def _build_context_dict(ctx: ConversationContext) -> dict[str, Any]:
    """Build a context dict for frontend breadcrumbs."""
    crumbs = []
    if ctx.domain:
        crumbs.append(ctx.domain.title() if ctx.domain != "admissions" else "Admissions")
    if ctx.level:
        crumbs.append(ctx.level.upper())
    if ctx.programme:
        from app.orchestrator.context import _get_programme_label
        label = _get_programme_label(ctx.programme) or ctx.programme.upper()
        crumbs.append(label)
    result = {"programme": ctx.programme}
    if ctx.academic_scheme:
        from app.orchestrator.context import scheme_label
        crumbs.append(scheme_label(ctx.academic_scheme) or ctx.academic_scheme.upper())
        result["academic_scheme"] = ctx.academic_scheme
    if crumbs:
        result["breadcrumbs"] = crumbs
    return result


def _augment_for_rag(ctx: ConversationContext, text: str, entities: Any | None = None) -> str:
    """Build a context-augmented query string for RAG.

    Context is only folded in when the current message is about the university
    or points back at an earlier entity (a referential follow-up). A clear
    off-domain question ("what is the capital of France?") must never be scoped
    to the last programme just because it happens to be in conversation memory.
    """
    if (
        entities is not None
        and getattr(entities, "is_question", False)
        and not is_university_related(text, entities)
        and not is_referential_followup(text)
    ):
        return text
    from app.orchestrator.context import _get_programme_label
    parts = []
    if ctx.programme:
        label = _get_programme_label(ctx.programme) or ctx.programme.upper()
        parts.append(label)
    if ctx.level:
        parts.append(ctx.level.upper())
    if ctx.domain:
        parts.append(ctx.domain.title() if ctx.domain != "admissions" else "Admissions")
    parts.append(text)
    return " ".join(parts)


def _augment_for_rag_from_prog(programme: str, topic: str) -> str:
    from app.orchestrator.context import _get_programme_label
    label = _get_programme_label(programme) or programme.upper()
    return f"{label} {topic.replace('_', ' ')}"


def _build_college_actions(college_id: str, current_topic: str | None) -> list[dict[str, str]]:
    """Build action buttons for a college, excluding the current topic."""
    all_actions = [
        {"id": "about", "label": "About"},
        {"id": "courses", "label": "Courses"},
        {"id": "departments", "label": "Departments"},
        {"id": "admissions", "label": "Admissions"},
        {"id": "fee", "label": "Fee Structure"},
        {"id": "eligibility", "label": "Eligibility"},
        {"id": "facilities", "label": "Facilities"},
        {"id": "contact", "label": "Contact"},
        {"id": "principal", "label": "Principal"},
    ]
    if current_topic:
        return [a for a in all_actions if a["id"] != current_topic]
    return all_actions


def _is_college_followup(text: str, entities: Any, ctx: ConversationContext) -> bool:
    """Check if a short message is a follow-up within college context."""
    if not ctx.college:
        return False
    if entities.word_count > 5:
        return False
    if entities.is_reset or entities.is_back:
        return False
    clean = text.strip().lower().rstrip("?.,!;:")
    # Allow DOMAIN_KEYWORDS that are also valid college topics (e.g. "fee" in college context)
    if clean in DOMAIN_KEYWORDS and clean not in COLLEGE_TOPICS:
        return False
    if clean in PROGRAMME_ALIASES:
        return False
    # Exclude course-college discovery queries so Rule 9a can handle them
    return not _detect_course_college_query(text, ctx)


def _word_in_text(word: str, text: str) -> bool:
    """Check if word appears as a whole word (word-boundary match) in text."""
    return bool(re.search(r"\b" + re.escape(word.lower()) + r"\b", text.strip().lower()))


_COURSE_COLLEGE_PATTERNS: list[tuple[str, str]] = [
    (r"(?:which|what)\s+colleges?\s+(?:offer|offers|offering|have|has|provide|teach|run)\s+(\w+)", "course_to_colleges"),
    (r"(?:which|what)\s+(?:courses?|programmes?)\s+(?:are\s+)?(?:offered|available|taught|run)\s+(?:at|in|by)\s+(.+)", "college_to_courses"),
    (r"(?:list|show|tell)\s+(?:all\s+)?colleges?\s+(?:with|offering|having|for)\s+(\w+)", "course_to_colleges"),
    (r"(?:list|show|tell)\s+(?:all\s+)?(?:courses?|programmes?)\s+(?:at|in|for|offered\s+by)\s+(.+)", "college_to_courses"),
    (r"what\s+(?:courses?|programmes?)\s+(?:does|do)\s+(.+)\s+(?:offer|offers|have|has|provide|teach|run)", "college_to_courses"),
    (r"(\w+)\s+(?:colleges?|college\s+offering)", "course_to_colleges"),
    (r"colleges?\s+(?:that\s+)?(?:offer|offering|offers)\s+(\w+)", "course_to_colleges"),
]


def _detect_course_college_query(text: str, ctx: ConversationContext) -> dict | None:
    """Detect if the message is asking about college-course mappings.

    Returns a dict with {'type': str, 'programme_id': str|None, 'college_id': str|None}
    or None if not a course-college query.
    """
    from app.college.aliases import resolve as resolve_college_alias

    for pattern, qtype in _COURSE_COLLEGE_PATTERNS:
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            key = m.group(1).strip().lower()

            if qtype == "course_to_colleges":
                # key should be a programme ID or name
                from app.orchestrator.context import PROGRAMME_ALIASES
                pid = PROGRAMME_ALIASES.get(key)
                if pid:
                    return {"type": qtype, "programme_id": pid, "college_id": None}
                # Check if it matches a known topic that happens to be a programme
                known_progs = {"ba", "bsc", "bcom", "bba", "bca", "btech", "bed", "ma", "msc", "mcom", "mba", "mca", "med"}
                if key in known_progs:
                    return {"type": qtype, "programme_id": key, "college_id": None}
                return None

            if qtype == "college_to_courses":
                cid = resolve_college_alias(key)
                if cid:
                    return {"type": qtype, "programme_id": None, "college_id": cid}
                return None

    return None


_GREETING_WORDS = frozenset({
    "hi", "hello", "hey", "hii", "hiii", "hiya", "yo", "there", "bot",
    "salam", "salaam", "assalam", "assalamualaikum", "assalamu", "alaikum",
    "namaste", "namaskar", "adaab", "ji",
    "good", "morning", "afternoon", "evening", "day",
    "how", "are", "you", "r", "u", "kaise", "ho", "kya", "haal",
})

_GREETING_EXACT = {
    "hi": "greeting", "hello": "greeting", "hey": "greeting",
    "hello there": "greeting", "hi there": "greeting", "hi bot": "greeting",
    "hello ji": "greeting",
    "assalamualaikum": "greeting", "assalamu alaikum": "greeting",
    "salam": "greeting", "salaam": "greeting",
    "namaste": "greeting", "namaskar": "greeting", "adaab": "greeting",
    "good morning": "greeting", "good afternoon": "greeting",
    "good evening": "greeting", "good day": "greeting",
    "how are you": "greeting", "how r u": "greeting", "how are u": "greeting",
    "kaise ho": "greeting", "kya haal hai": "greeting",
    "thank you": "courtesy", "thanks": "courtesy", "thanku": "courtesy",
    "thank u": "courtesy", "thx": "courtesy", "thankyou": "courtesy",
    "shukriya": "courtesy", "dhanyavad": "courtesy",
}


def _detect_greeting(text: str) -> str | None:
    """Return 'greeting' | 'courtesy' | None for pure greeting messages.

    Only messages made ENTIRELY of greeting/courtesy words (max 4 tokens)
    qualify, so mixed messages ("hello, what is the MCA fee?") keep flowing
    through the normal pipeline.
    """
    t = text.strip().lower().rstrip("!?.,;: ")
    t = re.sub(r"\s+", " ", t)
    if not t or len(t) > 40:
        return None
    if t in _GREETING_EXACT:
        return _GREETING_EXACT[t]
    words = t.split()
    if len(words) <= 4 and all(w in _GREETING_WORDS for w in words):
        return "greeting"
    return None


def _detect_authority_intent(message: str) -> list[dict]:
    """Check if the user is asking about a university office or requesting human contact.

    Triggers when:
      - a strong keyword-overlap match exists (score > 2.0), or
      - an explicit escalation pattern fires (score >= 0.7), or
      - the message asks an office question ("who is registrar", "who handles
        exams") that resolves to a known department via the alias maps.
    This prevents broad keyword overlap from stealing RAG queries while still
    catching thin-but-explicit office questions before slot-fill can.
    """
    from app.authority.matcher import (
        DEPARTMENT_ALIASES as _AUTHORITY_DEPT_ALIASES,
    )
    from app.authority.matcher import (
        SERVICE_ROUTES as _AUTHORITY_SERVICE_ROUTES,
    )
    from app.authority.matcher import (
        detect_escalation_intent,
        find_authority,
    )
    try:
        # Only accept authority matches with high confidence AND explicit
        # authority evidence in the message (office question markers or
        # office nouns). Keyword-overlap alone is too noisy ("documents
        # required for admission" must not become an Admissions contact card).
        matches = find_authority(message, top_k=3)
        strong_matches = [m for m in matches if m.get("_match_score", 0) >= 2.0]
        if strong_matches and _has_authority_evidence(message):
            return strong_matches
        # Escalation intent requires explicit human contact patterns
        if detect_escalation_intent(message) >= 0.7:
            matches = find_authority(message, top_k=2)
            strong_matches = [m for m in matches if m.get("_match_score", 0) >= 2.0]
            if strong_matches and _has_authority_evidence(message):
                return strong_matches
        # Explicit office question → direct department routing
        if _is_authority_question(message):
            low = message.strip().lower()
            for alias, dept in {**_AUTHORITY_DEPT_ALIASES, **_AUTHORITY_SERVICE_ROUTES}.items():
                if re.search(r"\b" + re.escape(alias) + r"\b", low):
                    row = _authority_by_department(dept)
                    if row:
                        return [row]
    except Exception:
        pass
    return []


_AUTHORITY_QUESTION_MARKERS = (
    "who", "whose", "whom",
    "handles", "handling", "deals", "dealing", "manages", "managing",
    "oversees", "supervises",
    "in charge", "in-charge",
    "office", "offices", "officer", "officers",
    "contact", "speak to", "talk to",
    "department", "wing", "cell",
)


def _is_authority_question(message: str) -> bool:
    """True when the message reads as an explicit office question.

    Markers are intentionally narrow: they require an explicit authority
    posture (who / in charge / office / deals with ...) so genuine student
    services ("my results", "admission fee") are never captured.
    """
    low = message.strip().lower()
    return any(m in low for m in _AUTHORITY_QUESTION_MARKERS)


# Office nouns that themselves signal an authority query even without a
# question marker ("registrar", "controller of examinations", ...).
_AUTHORITY_NOUNS = (
    "registrar", "registrars", "chancellor", "chancellors", "vice chancellor",
    "vice-chancellor", "controller", "coe", "dean", "deans", "warden",
    "librarian", "principal", "director", "secretary", "in charge",
    "in-charge", "authority", "authorities", "helpline",
)


def _has_authority_evidence(message: str) -> bool:
    """True when an authority match is backed by explicit office evidence.

    Gates the keyword-overlap score path: a high score alone is not enough
    ("admission requirements" frequently overlaps an Admissions office's
    keyword pool). The message must read as an office request.
    """
    if _is_authority_question(message):
        return True
    low = message.strip().lower()
    return any(n in low for n in _AUTHORITY_NOUNS)


def _authority_by_department(department_name: str) -> dict | None:
    """Return the first cached authority for an exact department name."""
    from app.authority.service import authority_service
    low = str(department_name).lower()
    for row in authority_service.list_active():
        if str(row.get("department_name") or "").lower() == low:
            return row
    return None


# ---------------------------------------------------------------------------
# News / website knowledge
# ---------------------------------------------------------------------------

# Nouns that mark a current-information / website-knowledge query. A single
# noun is enough ("circular"), but pure navigation labels are excluded at the
# call site so the existing option-button flow keeps working.
_NEWS_NOUNS = (
    "notice", "notices", "notification", "notifications", "circular",
    "circulars", "calendar", "calendars", "announcement", "announcements",
    "news", "newsletter", "newsletters", "bulletin", "bulletins",
    "holiday", "holidays", "closure", "re-opening", "reopening",
    "update", "updates",
)


def _detect_news_intent(text: str) -> str | None:
    """Detect website-knowledge / news queries (current notices & circulars).

    Returns a news-scoped retrieval query, or None when the message is not
    a news query. The returned query keeps the user's wording and appends
    the news vocabulary so the retriever surfaces synced notice documents.
    """
    low = text.strip().lower()
    if not any(n in low for n in _NEWS_NOUNS):
        return None
    parts = [low]
    for kw in ("notices", "circulars", "notifications", "announcements"):
        if kw not in low:
            parts.append(kw)
    return " ".join(parts) if len(parts) > 1 else low


# ---------------------------------------------------------------------------
# University notices (public date sheets)
# ---------------------------------------------------------------------------

# Phrases that mark a university-notices / date-sheet request. Deliberately
# narrower than _NEWS_NOUNS: only exam-schedule vocabulary routes to the
# structured notices pipeline; bare "notices"/"circular" stays with the
# website-knowledge / news flow (Rule 3b).
_SCHEDULE_KEYWORDS = (
    "date sheet", "date-sheet", "datesheet",
    "time table", "time-table", "timetable",
    "exam schedule", "examination schedule", "schedule of exam",
    "exam date", "examination date",
    "exam timetable", "examination timetable",
)

# Stream/sub-branch mentions that further narrow a date-sheet request
# ("btech cse 4th sem date sheet"). Short ambiguous tokens ("it", "ce",
# "ee", "me") are deliberately excluded so ordinary English is never
# misread as a stream.
_STREAM_TERMS = (
    ("cse", "cse"),
    ("computer science", "cse"),
    ("ece", "ece"),
    ("electronics and communication", "ece"),
    ("eee", "eee"),
    ("electrical and electronics", "eee"),
    ("mechanical", "mechanical"),
    ("civil", "civil"),
    ("electronics", "electronics"),
    ("electrical", "electrical"),
)


def _detect_stream(text: str) -> str | None:
    """Extract a stream/branch mention (cse, ece, ...) from the message."""
    low = text.lower()
    for term, stream in _STREAM_TERMS:
        if term in low:
            return stream
    return None


def _detect_batch(text: str) -> str | None:
    """Extract an explicit batch mention like "2024 batch" / "batch of 2024".

    Batch is only ever inferred when the user explicitly says "batch" (or its
    fuzzy-normalised twin "back", which query-understanding produces for
    "…2024 batch…") — a bare year elsewhere is never guessed, so a query can
    never silently over-filter on an unstated batch.
    """
    low = text.strip().lower()
    if "batch" in low:
        for pattern in (
            r"batch[^0-9a-z]{0,24}?(20\d{2}(?:\s*[-/]\s*\d{2,4})?)",
            r"(20\d{2}(?:\s*[-/]\s*\d{2,4})?)[^0-9a-z]{0,24}?batch",
        ):
            m = re.search(pattern, low)
            if m:
                return m.group(1).replace(" ", "")
        m = re.search(r"\b(20\d{2})\b", low)
        if m:
            return m.group(1)
        return None
    # Fuzzy normalisation rewrites "2024 batch" -> "2024 back": a year directly
    # adjacent to that mangled token is still an explicit batch mention.
    m = re.search(r"(20\d{2}(?:\s*[-/]\s*\d{2,4})?)[^0-9a-z]{0,24}?back\b", low)
    if m:
        return m.group(1).replace(" ", "")
    m = re.search(r"back\b[^0-9a-z]{0,24}?(20\d{2}(?:\s*[-/]\s*\d{2,4})?)", low)
    if m:
        return m.group(1).replace(" ", "")
    return None


# Discipline/programme mentions that pin a date-sheet request to a specific
# subject column (pattern, canonical programme id). Long/specific patterns are
# tested first; the slugs must exactly match DateSheetEntry.programme_id as
# produced by the notices parser (see parser._COLUMN_PROGRAMME_MAP), so
# "AI and ML …" filters programme_id == "ai-ml" rather than the degree family
# (pg/msc/…) the extractor would otherwise return.
_NOTICE_PROGRAMME_ALIASES = (
    (r"\bbusiness\s+administration\b|\bbba\b", "business-administration"),
    (r"\bcomputer\s+applications?\b", "computer-applications"),
    (r"\bcomputer\s+science\b", "computer-science"),
    (r"\bartificial\s+intelligence\b|\bmachine\s+learning\b|\bai\s*(?:and|&)\s*ml\b",
     "ai-ml"),
    (r"\bdata\s+science\b", "data-science"),
    (r"\benvironmental\s+science\b", "environmental-science"),
    (r"\bpolitical\s+science\b", "political-science"),
    (r"\bjournalism[^\s]*", "journalism-mass-communication"),
    (r"\bbio[- ]?chemistry\b|\bbiochemistry\b", "bio-chemistry"),
    (r"\bhome\s+science\b", "home-science"),
    (r"\bphysics\b", "physics"),
    (r"\bchemistry\b", "chemistry"),
    (r"\bbotany\b", "botany"),
    (r"\bzoology\b", "zoology"),
    (r"\bhistory\b", "history"),
    (r"\beconomics\b", "economics"),
    (r"\bgeography\b", "geography"),
    (r"\benglish\b", "english"),
    (r"\bmusic\b", "music"),
    (r"\beducation\b", "education"),
)


def _detect_programme_discipline(text: str) -> str | None:
    """Extract a discipline/programme-column mention (ai-ml, physics, ...)."""
    low = text.lower()
    for pattern, pid in _NOTICE_PROGRAMME_ALIASES:
        if re.search(pattern, low):
            return pid
    return None


def _detect_notice_intent(text: str, e: Any) -> dict | None:
    """Detect a university-notices / date-sheet request (Rule 3a).

    Returns a dict (mode `notice_list` | `schedule`, programme, semester,
    stream, batch, query, confidence, reason) or None when the message is not
    a date-sheet/schedule request. Mode becomes `schedule` whenever an
    explicit semester is present — with a programme the engine serves that
    programme's VERIFIED DateSheetEntry rows; without one it serves the whole
    matching semester sheet. Programme/semesterless requests list the
    published notices instead. The LLM never generates any schedule values.
    """
    low = text.strip().lower()
    if not any(k in low for k in _SCHEDULE_KEYWORDS):
        return None
    programmes = list(getattr(e, "programmes", None) or [])
    programme = programmes[0] if programmes else (getattr(e, "programme", None) or None)
    semester = getattr(e, "semester", None) or None
    stream = _detect_stream(low)
    batch = _detect_batch(low)
    discipline = _detect_programme_discipline(low)
    # A discipline mention (subject column) overrides any degree family the
    # extractor returned (pg/msc/…): verified schedule rows are keyed by the
    # column programme id (ai-ml, physics, …), never the degree family.
    if discipline:
        programme = discipline
    if semester is not None:
        mode = "schedule"
        conf = 0.92
        reason = (
            f"Date-sheet schedule for {programme.upper()} semester {semester}"
            if programme else
            f"Date-sheet schedule for semester {semester}"
        )
    elif programme:
        mode = "notice_list"
        conf = 0.85
        reason = f"Published date-sheet notices for {programme.upper()}"
    else:
        mode = "notice_list"
        conf = 0.8
        reason = "Published date-sheet notices"
    return {
        "mode": mode,
        "programme": programme,
        "semester": semester,
        "stream": stream,
        "batch": batch,
        "query": low,
        "confidence": conf,
        "reason": reason,
    }


# ---------------------------------------------------------------------------
# Official notifications / other official documents (canonical repository)
# ---------------------------------------------------------------------------

# Vocabulary for the student-facing official-document read path (Rule 3ab).
# Deliberately narrower than _NEWS_NOUNS: bare "notice"/"notices"/"circular"
# keep their existing news / option flows, so only notification(s), official
# notice(s) and official document(s) are routed to the verified repository.
_OFFICIAL_DOC_PATTERNS = (
    re.compile(r"\bnotifications?\b"),
    re.compile(r"\bofficial\s+notices?\b"),
    re.compile(r"\bofficial\s+documents?\b"),
)
_OFFICIAL_DOC_OTHER_RE = re.compile(r"\bother\s+official\b")

# Words stripped from a request to leave only the topic used to search document
# titles/filenames. Anything not listed is treated as topic content.
_OFFICIAL_DOC_STOPWORDS = frozenset({
    "official", "document", "documents", "notification", "notifications",
    "notice", "notices", "university", "latest", "recent", "new", "newest",
    "show", "list", "me", "all", "the", "a", "an", "please", "give", "get",
    "find", "download", "open", "any", "for", "of", "about", "related",
    "to", "with", "and", "in", "on", "some", "doc", "docs", "file", "files",
    "other", "official's", "everything", "them",
})


def _detect_official_document_intent(text: str, e: Any) -> dict | None:
    """Detect an official-notification / official-document request (Rule 3ab).

    Returns a dict (doc_types, programme, q, query, confidence, reason) or None.
    Generic requests search BOTH ``official_notification`` and
    ``other_official_document``; an explicit "other official document" narrows to
    that family only. The LLM never invents a document — the engine serves only
    published + verified rows, with an honest fallback when none match.
    """
    low = text.strip().lower()
    if not any(p.search(low) for p in _OFFICIAL_DOC_PATTERNS):
        return None
    programmes = list(getattr(e, "programmes", None) or [])
    programme = programmes[0] if programmes else (getattr(e, "programme", None) or None)
    discipline = _detect_programme_discipline(low)
    if discipline:
        programme = discipline
    if _OFFICIAL_DOC_OTHER_RE.search(low):
        doc_types = ["other_official_document"]
    else:
        doc_types = ["official_notification", "other_official_document"]
    prog_tokens = set(re.findall(r"[a-z0-9]+", str(programme or "").replace("-", " ").lower()))
    tokens = [
        t for t in re.findall(r"[a-z0-9]+", low)
        if t not in _OFFICIAL_DOC_STOPWORDS and t not in prog_tokens
    ]
    q = " ".join(tokens).strip() or None
    return {
        "doc_types": doc_types,
        "programme": programme,
        "q": q,
        "query": low,
        "confidence": 0.9,
        "reason": "Official university documents / notifications",
    }


def _detect_examination_intent(text: str, e: Any, raw: str | None = None) -> dict | None:
    """Detect the Examinations menu's dedicated services (Rule 3aa).

    Delegates to app/examination/detect.py, which is keyword/phrase-gated and
    enforces the spec's exclusions (PYQ / previous-year / non-exam fees /
    bare marks-improvement are NOT routed here). Any failure degrades to None
    so the rest of the pipeline handles the message unchanged. ``raw`` is the
    original user message, used for deterministic filter-constraint extraction
    so the query-understanding rewrite never drops a programme/semester/
    subject/batch/academic-year filter.
    """
    try:
        from app.examination.detect import detect_examination_intent
        return detect_examination_intent(text, e, raw=raw)
    except Exception:
        return None
