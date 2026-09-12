"""
backend/app/orchestrator/engine.py

AI Orchestration Engine V2 — planner-driven, fast, context-aware.

Flow:
  1. Get conversation state + context
  2. Extract entities (fast rule-based)
  3. Planner decides execution path
  4. Execute plan (structured / navigation / rag / clarify / news / authority)
  5. Update context
  6. Yield SSE-compatible events

Every stage is timed and logged via the metrics module.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import time
import uuid
from collections.abc import AsyncGenerator
from typing import Any

from sqlalchemy.orm import Session

from app.analytics.collector import (
    collect_event,
    collect_knowledge_gap,
)
from app.chat.intent_router import (
    _PROGRAMME_DETAILS,
    WELCOME_OPTIONS,
)
from app.chat.service import run_chat
from app.college.service import CollegeService
from app.orchestrator.context import (
    PROGRAMME_ALIASES,
    ConversationContext,
    clear_college_context,
    update_context_for_college,
)
from app.orchestrator.extractor import extract_entities
from app.orchestrator.metrics import log_stage, stage_timer
from app.orchestrator.planner import plan
from app.orchestrator.state import (
    Breadcrumb,
    ConversationState,
    clear_state,
    get_state,
    push_breadcrumb,
)
from app.student.gate import (
    auth_form_event,
    auth_gate_message,
    coming_soon_event,
    expired_gate_message,
    hub_options,
    logged_out_gate_message,
)


def _anon_session_id(chat_id: str) -> str:
    """Derive a stable anonymous session ID from a chat_id (SHA1 prefix)."""
    return hashlib.sha1(chat_id.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------


async def process(
    db: Session,
    user_id: str,
    message: str,
    chat_id: str,
    student_session: dict[str, Any] | None = None,
    student_auth_kind: str = "none",
) -> AsyncGenerator[dict[str, Any], None]:
    """
    Main orchestration entry point. Yields SSE-compatible event dicts.

    `student_session` is the resolved, server-validated student identity for
    this request (dict from app.student.session.resolve_session, or None when
    the browser holds no valid session cookie). It is the ONLY source of truth
    for Student Services auth — the browser is never trusted with the
    decision. Credentials never travel through the chat pipeline.

    `student_auth_kind` is a WORDING-ONLY hint used when no valid session
    resolves. One of:
      - "none"     no session cookie at all (first sign-in) -> generic ask
      - "expired"  cookie present but time-expired/invalid  -> "has expired"
      - "revoked"  session was explicitly logged out         -> "has ended"
    The access decision is still made solely by `student_session`; the hint
    only changes the sign-in message so a logged-out student is never falsely
    told their session newly "expired".
    """
    with stage_timer("total"):
        state = await get_state(chat_id)
        text = message.strip()
        ctx = state.context

        # ----- Stage 1: Entity extraction -----
        with stage_timer("entity_extraction"):
            entities = extract_entities(text)
        log_stage("entity_extraction", f"prog={entities.programme} topic={entities.topic}")

        # ----- Stage 1b: Catalogue picker continuation -----
        # A previous catalogue turn (scheme picker, level picker, semester /
        # minor / curriculum-doc selector) left state.catalogue_pending. The
        # next message is that picker's option id (a scheme UUID, "level:ug",
        # "menu:fee", "semester:2", ...) — continue the flow directly instead
        # of planning it as free text.
        if state.catalogue_pending:
            with stage_timer("catalogue_continue"):
                try:
                    from app.catalogue.backend import continue_pending
                    events = await continue_pending(db, user_id, text, chat_id, state)
                except Exception as exc:
                    from app.utils.logging import log as _log
                    _log.error("catalogue continue failed chat=%s: %s", chat_id, exc)
                    events = None
            if events:
                for event in events:
                    yield event
                return
            state.catalogue_pending = None  # unresolvable -> normal flow

        # ----- Stage 1c: Slot-fill continuation -----
        # The planner asked for a missing slot ("Which programme?") and stored
        # the pending topic on state. If this message provides the missing
        # entity (and brings no topic of its own), resolve the ORIGINAL
        # request directly ("MCA" after "fee structure of which programme?"
        # must answer MCA fees, not show the MCA overview).
        if state.slot_topic and not entities.topic:
            resolved_slot = await _try_resolve_slot_fill(db, text, chat_id, state)
            if resolved_slot is not None:
                async for event in resolved_slot:
                    yield event
                return
        elif state.slot_topic and entities.topic:
            # User restated a topic of their own — the pending slot expires.
            state.slot_topic = None
            state.slot_request = None


        # ----- Stage 4: Planner -----
        plan_t0 = time.perf_counter()
        planner_plan = plan(text, ctx, chat_id, entities)
        planner_latency_ms = int((time.perf_counter() - plan_t0) * 1000)
        log_stage("planning", f"action={planner_plan.action} target={planner_plan.target} confidence={planner_plan.confidence:.2f} reason={planner_plan.reason} ({planner_latency_ms}ms)")

        # ----- Stage 5: Execute plan -----
        async for event in _execute_plan(
            db, user_id, text, chat_id, state, ctx, entities, planner_plan,
            planner_latency_ms=planner_latency_ms,
            student_session=student_session,
            student_auth_kind=student_auth_kind,
        ):
            yield event

        # ----- Stage 6: Persist canonical query contract -----
        # The last contract lets later turns inherit resolved fields (e.g. the
        # catalogue programme row UUID) without re-resolving from raw text.
        try:
            contract = (planner_plan.extra or {}).get("contract")
            if isinstance(contract, dict):
                state.last_contract = contract
                ctx._last_contract = contract
        except Exception:
            pass


async def _execute_plan(
    db: Session,
    user_id: str,
    message: str,
    chat_id: str,
    state: ConversationState,
    ctx: ConversationContext,
    entities: Any,
    plan_result: Any,
    planner_latency_ms: int | None = None,
    student_session: dict[str, Any] | None = None,
    student_auth_kind: str = "none",
) -> AsyncGenerator[dict[str, Any], None]:
    """Execute a plan and yield SSE events."""
    action = plan_result.action
    anon_session = _anon_session_id(chat_id)
    t0 = time.perf_counter()

    # Auth-gate invariant: the Student Services gate is only "armed" while the
    # planner stays on the student_service action. Any other action leaves the
    # flow and must clear the gate so a later turn starts clean.
    if action != "student_service":
        state.student_gate = None

    def _make_event(**kw: Any) -> dict[str, Any]:
        elapsed_ms = int((time.perf_counter() - t0) * 1000)
        base = {
            "anon_session_id": anon_session,
            "conversation_id": chat_id,
            "planner_action": action,
            "detected_intent": state.last_intent,
            "response_time_ms": elapsed_ms,
            "planner_latency_ms": planner_latency_ms,
            "detected_programme": entities.programme or ctx.programme,
            "detected_topic": entities.topic or ctx.topic,
            "detected_college": ctx.college,
            "detected_level": entities.level or ctx.level,
            "query_original": ctx.query_original,
            "query_corrected": ctx.query_corrected,
        }
        base.update(kw)
        return {k: v for k, v in base.items() if v is not None}

    if action == "welcome":
        yield WELCOME_OPTIONS
        yield {"type": "done", "chat_id": chat_id, "cited_chunks": []}
        state.last_intent = "navigation"
        asyncio.ensure_future(collect_event(**_make_event(
            response_source="welcome", route_chosen="welcome",
            conversation_completed=True,
        )))
        return

    if action == "greeting":
        yield {"type": "token", "text": "Hello! Welcome to the CUS AI Assistant. I can help you with admissions, courses, fee details, exam schedules, and more. Select a topic below or type your question."}
        yield WELCOME_OPTIONS
        yield {"type": "done", "chat_id": chat_id, "cited_chunks": []}
        state.last_intent = "navigation"
        asyncio.ensure_future(collect_event(**_make_event(
            response_source="greeting", route_chosen=action,
            conversation_completed=True,
        )))
        return

    if action == "structured":
        response = plan_result.response
        if response:
            _update_context_from_plan(ctx, plan_result)
            _add_context_to_response(response, ctx)
            yield response
            yield {"type": "done", "chat_id": chat_id, "cited_chunks": []}
            state.last_intent = "navigation"
            asyncio.ensure_future(collect_event(**_make_event(
                response_source="structured", route_chosen=action,
                structured_lookup_used=True,
                conversation_completed=True,
            )))
        return

    if action == "navigation":
        response = plan_result.response
        if response:
            _update_context_from_plan(ctx, plan_result)
            await _update_nav_breadcrumb(chat_id, state, response, message)
            _add_context_to_response(response, ctx)
            yield response
            yield {"type": "done", "chat_id": chat_id, "cited_chunks": []}
            state.last_intent = "navigation"
            asyncio.ensure_future(collect_event(**_make_event(
                response_source="navigation", route_chosen=action,
                conversation_completed=True,
            )))
        else:
            await clear_state(chat_id)
            yield WELCOME_OPTIONS
            yield {"type": "done", "chat_id": chat_id, "cited_chunks": []}
        return


    if action == "rag":
        query = plan_result.target or message
        _update_context_from_rag(ctx, entities, query)
        state.last_intent = "knowledge"
        rag_t0 = time.perf_counter()
        async for event in run_chat(db, user_id, query, chat_id, context=_build_rag_context(ctx, entities)):
            yield event
        rag_ms = int((time.perf_counter() - rag_t0) * 1000)
        asyncio.ensure_future(collect_event(**_make_event(
            response_source="rag", route_chosen=action,
            rag_used=True, rag_latency_ms=rag_ms,
            conversation_completed=True,
            query_original=ctx.query_original,
            query_corrected=ctx.query_corrected,
        )))
        return

    if action == "clarify":
        yield _build_clarification(ctx, plan_result.target)
        yield {"type": "done", "chat_id": chat_id, "cited_chunks": []}
        state.last_intent = "clarification"
        asyncio.ensure_future(collect_knowledge_gap(
            gap_type="repeated_clarification",
            query_text=message,
            suggestion=f"User needed clarification on: {plan_result.target}",
        ))
        asyncio.ensure_future(collect_event(**_make_event(
            response_source="clarification", route_chosen=action,
        )))
        return

    if action == "authority":
        async for event in _handle_authority_route(db, user_id, message, chat_id, state, plan_result):
            yield event
        asyncio.ensure_future(collect_event(**_make_event(
            response_source="authority", route_chosen=action,
            service_requested=plan_result.target,
        )))
        return

    if action == "grievance":
        extra = plan_result.extra or {}
        prefill = extra.get("query") or message
        category = extra.get("category") or "Other"
        yield {"type": "token", "text": "I hear you — let me set up a grievance report so the right office can look into it."}
        yield {"type": "grievance", "payload": {"prefill": prefill, "category": category}}
        yield {"type": "done", "chat_id": chat_id, "cited_chunks": []}
        state.last_intent = "grievance"
        asyncio.ensure_future(collect_event(**_make_event(
            response_source="grievance", route_chosen=action,
            conversation_completed=True,
        )))
        return

    if action == "llm":
        state.last_intent = "knowledge"
        llm_t0 = time.perf_counter()
        async for event in run_chat(db, user_id, message, chat_id):
            yield event
        llm_ms = int((time.perf_counter() - llm_t0) * 1000)
        asyncio.ensure_future(collect_event(**_make_event(
            response_source="llm", route_chosen=action,
            llm_used=True, llm_latency_ms=llm_ms,
            conversation_completed=True,
        )))
        return

    if action == "catalogue":
        # Structured academic catalogue route (schemes / programmes / subjects /
        # fee / eligibility / semesters / credits / outcomes / curriculum...).
        # The handler stores any picker continuation on state.catalogue_pending.
        req = (plan_result.extra or {}).get("req")
        if req:
            async for event in _handle_catalogue(db, user_id, message, chat_id, state, req):
                yield event
            return
        # No request payload — fall through to knowledge retrieval.
        query = plan_result.target or message
        state.last_intent = "knowledge"
        async for event in run_chat(db, user_id, query, chat_id, context=_build_rag_context(ctx, entities)):
            yield event
        return

    if action == "slot_fill":
        # Missing-entity question ("Which programme?") — remember the pending
        # topic so the user's next message continues the ORIGINAL request.
        extra = plan_result.extra or {}
        pending_topic = extra.get("pending_topic") or entities.topic
        slot_field = extra.get("slot") or plan_result.target or "programme"
        state.slot_topic = pending_topic
        state.slot_request = {"topic": pending_topic, "slot": slot_field}
        state.last_intent = "slot_fill"
        yield _build_slot_fill_question(pending_topic, slot_field)
        yield {"type": "done", "chat_id": chat_id, "cited_chunks": []}
        asyncio.ensure_future(collect_event(**_make_event(
            response_source="slot_fill", route_chosen=action,
            conversation_completed=True,
        )))
        return

    if action == "student_service":
        # Auth gate for personal student services. Without a resolved
        # server-side session the student is asked to sign in (auth_form);
        # with one, the authenticated hub is shown. Results (semester picker →
        # subject detail), Admit Card (semester picker → structured card) and
        # Exam Form (form picker → structured detail) are fully implemented;
        family = plan_result.target or "hub"
        state.last_intent = "student_service"
        state.slot_topic = None
        state.slot_request = None

        if not student_session:
            state.student_gate = {"stage": "auth", "family": family, "message": message}
            if student_auth_kind == "revoked":
                yield {"type": "token", "text": logged_out_gate_message(family)}
            elif student_auth_kind == "expired":
                yield {"type": "token", "text": expired_gate_message(family)}
            else:
                yield {"type": "token", "text": auth_gate_message(family)}
            yield auth_form_event(family)
            yield {"type": "done", "chat_id": chat_id, "cited_chunks": []}
            asyncio.ensure_future(collect_event(**_make_event(
                response_source="student_auth_form", route_chosen=action,
                conversation_completed=True,
            )))
            return

        previous_gate = state.student_gate
        state.student_gate = {"stage": "authed", "family": family}
        if family == "hub":
            # Continue the ORIGINAL request after re-login: the pre-login gate
            # stored which family the student asked for; "Student Services"
            # (sent by the frontend after a successful sign-in) resumes it
            # instead of forcing the student to start over.
            pending = previous_gate if isinstance(previous_gate, dict) else {}
            if pending.get("stage") == "auth" and pending.get("family") in (
                "results", "semester_result", "admit_card", "exam_form",
            ):
                family = pending["family"]
                if pending.get("message"):
                    message = pending["message"]
        if family == "hub":
            yield hub_options(student_session)
        elif family in ("results", "semester_result"):
            for event in _results_events(db, student_session, message, entities):
                yield event
        elif family == "admit_card":
            for event in _admit_card_events(db, student_session, message, entities):
                yield event
        elif family == "exam_form":
            for event in _exam_form_events(db, student_session, message, entities):
                yield event
        else:
            yield coming_soon_event(family)
            # Keep the authenticated hub reachable after the placeholder.
            yield hub_options(student_session)
        yield {"type": "done", "chat_id": chat_id, "cited_chunks": []}
        _student_src = {
            "hub": "student_hub",
            "results": "student_results",
            "semester_result": "student_results",
            "admit_card": "student_admit_card",
            "exam_form": "student_exam_form",
        }.get(family, "student_hub")
        asyncio.ensure_future(collect_event(**_make_event(
            response_source=_student_src,
            route_chosen=action,
            conversation_completed=True,
        )))
        return

    if action == "unavailable_service":
        # Unsupported personal student-service request ("results", "admit
        # card", "my attendance", ...): respond with a plain unavailability
        # message. Never yield options / programme pickers / forms, which
        # would imply a lookup capability that does not exist.
        family = plan_result.target or "results"
        yield _unavailable_service_event(family)
        yield {"type": "done", "chat_id": chat_id, "cited_chunks": []}
        state.last_intent = "unavailable_service"
        state.slot_topic = None
        state.slot_request = None
        asyncio.ensure_future(collect_event(**_make_event(
            response_source="unavailable_service", route_chosen=action,
            conversation_completed=True,
        )))
        return

    if action == "news":
        # Current notices / circulars / calendar from the synced website
        # knowledge base — never a dead-end menu.
        query = plan_result.target or message
        _update_context_from_rag(ctx, entities, query)
        state.last_intent = "knowledge"
        rag_t0 = time.perf_counter()
        async for event in run_chat(db, user_id, query, chat_id, context=_build_rag_context(ctx, entities)):
            yield event
        rag_ms = int((time.perf_counter() - rag_t0) * 1000)
        asyncio.ensure_future(collect_event(**_make_event(
            response_source="news", route_chosen=action,
            rag_used=True, rag_latency_ms=rag_ms,
            conversation_completed=True,
        )))
        return

    if action == "university_notices":
        # Public university notices / date sheets. Schedule facts are served
        # ONLY from VERIFIED DateSheetEntry rows (the structural gate lives in
        # notices.service.get_verified_schedule) — never generated by the LLM.
        extra = plan_result.extra or {}
        async for event in _handle_university_notices(db, chat_id, state, extra):
            yield event
        return

    if action == "comparison":
        # Side-by-side programme comparison — structured catalogue data when
        # both programmes exist, else scoped knowledge retrieval.
        async for event in _handle_comparison(db, user_id, message, chat_id, state, ctx, entities, plan_result):
            yield event
        return

    # Fallback (unknown action) — never silently dead-end
    from app.utils.logging import log as _log
    _log.warning("unhandled planner action=%s target=%s — falling back to knowledge", action, plan_result.target)
    state.last_intent = "knowledge"
    async for event in run_chat(db, user_id, message, chat_id):
        yield event


# ---------------------------------------------------------------------------
# Context update helpers
# ---------------------------------------------------------------------------


async def _try_resolve_slot_fill(
    db: Session,
    text: str,
    chat_id: str,
    state: ConversationState,
) -> AsyncGenerator[dict[str, Any], None] | None:
    """Resolve a pending slot-fill with the user's reply.

    When the planner asked "Which programme?" for a pending topic (fee,
    eligibility, ...) and the user now names a programme, this synthesizes
    "<programme> <topic>" and runs it through the same catalogue detector so
    the ORIGINAL request is answered directly.
    """
    ctx = state.context
    pending_topic = state.slot_topic
    try:
        syn_entities = extract_entities(f"{text} {pending_topic.replace('_', ' ')}")
        if not syn_entities.programme and not ctx.programme:
            return None
        from app.catalogue.detect import detect_catalogue_request
        req = detect_catalogue_request(f"{text} {pending_topic.replace('_', ' ')}", ctx, syn_entities)
        if not req:
            return None
        state.slot_topic = None
        state.slot_request = None
        return _handle_catalogue(db, "slot_resolve", text, chat_id, state, req, entities=syn_entities)
    except Exception:
        state.slot_topic = None
        state.slot_request = None
        return None


async def _handle_catalogue(
    db: Session,
    user_id: str,
    message: str,
    chat_id: str,
    state: ConversationState,
    request: dict[str, Any],
    entities: Any = None,
) -> AsyncGenerator[dict[str, Any], None]:
    """Execute a catalogue request and yield its SSE events.

    Exceptions are contained: the user gets a friendly message (with a trace
    identifier logged server-side) instead of a broken stream.
    """
    from app.catalogue.backend import handle_catalogue

    anon_session = _anon_session_id(chat_id)
    try:
        events = await handle_catalogue(db, user_id, message, chat_id, state, request)
    except Exception as exc:
        log.error("catalogue handler failed chat=%s req=%s: %s", chat_id, request.get("op"), exc)
        yield {
            "type": "error",
            "message": "I couldn't load that information right now. Please try again in a moment.",
            "ref": anon_session[:8],
        }
        yield {"type": "done", "chat_id": chat_id, "cited_chunks": []}
        return
    for event in events:
        yield event
    if entities is not None:
        asyncio.ensure_future(collect_event(
            anon_session_id=anon_session,
            conversation_id=chat_id,
            planner_action="catalogue",
            response_source="catalogue",
            route_chosen=request.get("op", "catalogue"),
            detected_programme=getattr(entities, "programme", None) or state.context.programme,
            detected_topic=getattr(entities, "topic", None) or state.context.topic,
            conversation_completed=True,
        ))


async def _handle_comparison(
    db: Session,
    user_id: str,
    message: str,
    chat_id: str,
    state: ConversationState,
    ctx: ConversationContext,
    entities: Any,
    plan_result: Any,
) -> AsyncGenerator[dict[str, Any], None]:
    """Answer a programme comparison.

    Structured catalogue data (fee, eligibility, duration, credits, subjects)
    is rendered side-by-side when both programmes exist in the catalogue;
    otherwise the request falls back to knowledge retrieval scoped to both
    programme names.
    """
    programmes = list(getattr(entities, "programmes", None) or [])
    if not programmes:
        programmes = list(getattr(ctx, "programmes", None) or [])
    if len(programmes) < 2:
        programmes = None

    try:
        from app.catalogue.service import get_programme, resolve_programme
        if programmes:
            resolved: list[tuple[str, dict]] = []
            for pid in programmes:
                row = resolve_programme(pid)
                if not row:
                    continue
                detail = get_programme(row["id"])
                if detail:
                    resolved.append((pid, detail))
            if len(resolved) >= 2:
                rows = _render_comparison_rows(resolved)
                yield {
                    "type": "detail",
                    "title": "Programme Comparison",
                    "message": f"Here's how the requested programmes compare:",
                    "fields": rows,
                    "context": {"breadcrumbs": ["Programmes", "Comparison"]},
                }
                yield {"type": "done", "chat_id": chat_id, "cited_chunks": []}
                state.last_intent = "knowledge"
                ctx.programmes = programmes
                asyncio.ensure_future(collect_event(
                    anon_session_id=_anon_session_id(chat_id),
                    conversation_id=chat_id,
                    planner_action="comparison",
                    response_source="catalogue",
                    route_chosen="comparison",
                    detected_programme=programmes[0],
                    conversation_completed=True,
                ))
                return
    except Exception:
        pass

    # Fallback: scoped knowledge retrieval mentioning both programmes
    _update_context_from_rag(ctx, entities, message)
    state.last_intent = "knowledge"
    async for event in run_chat(db, user_id, message, chat_id, context=_build_rag_context(ctx, entities)):
        yield event


async def _handle_university_notices(
    db: Session,
    chat_id: str,
    state: ConversationState,
    intent: dict[str, Any],
) -> AsyncGenerator[dict[str, Any], None]:
    """Serve a university-notices / date-sheet request (public pipeline).

    `intent` is the planner's Rule 3a extra dict. Mode `schedule` (programme +
    explicit semester) renders a `date_sheet_schedule` table from VERIFIED
    DateSheetEntry rows only (structural gate in
    notices.service.get_verified_schedule). Anything else renders a
    `notice_list` of published date-sheet notices. The LLM is never asked to
    produce an exam date, time, code or venue.
    """
    from app.config import settings as _settings
    from app.notices import service as _notices

    try:
        programme = (intent.get("programme") or "").strip().lower() or None
        semester = intent.get("semester")
        stream = (intent.get("stream") or "").strip().lower() or None
        batch = (intent.get("batch") or "").strip() or None
        mode = intent.get("mode") or "notice_list"

        seen = _notices.list_notices(
            db,
            published_only=True,
            notice_type="date_sheet",
            programme=programme,
            limit=_settings.NOTICES_SEARCH_TOP_N,
        )
        cards = [_notice_card(n) for n in seen]

        if mode == "schedule" and semester is not None:
            rows: list[dict[str, Any]] = []
            try:
                entries = _notices.get_verified_schedule(
                    db,
                    [n.id for n in seen],
                    programme=programme,
                    semester=int(semester),
                    stream=stream,
                    batch=batch,
                    limit=_settings.NOTICES_SCHEDULE_LIMIT,
                )
                rows = [_schedule_row(e) for e in entries]
            except Exception:
                rows = []
            if rows:
                # For a semester-only request the "documents" are the official
                # sheets that actually contain that semester — never unrelated
                # published notices.
                if not programme:
                    _row_ids = {r["notice_id"] for r in rows}
                    cards = [c for c in cards if c["id"] in _row_ids]
                yield _schedule_event(programme, semester, stream, rows, cards)
                yield {"type": "done", "chat_id": chat_id, "cited_chunks": []}
                state.last_intent = "notices"
                return
            # No verified structured row yet — show the official documents and
            # an honest not-available message (never fabricate a schedule).
            yield {
                "type": "notice_list",
                "notices": cards,
                "message": (
                    f"No verified structured schedule is available yet for "
                    f"{programme.upper() + ' ' if programme else ''}semester "
                    f"{semester}. The official notices above are the "
                    "authoritative source."
                ),
                "_query": {"via": "university_notices"},
            }
            yield {"type": "done", "chat_id": chat_id, "cited_chunks": []}
            state.last_intent = "notices"
            return

        if cards:
            message = (
                "Here are the published date-sheet notices"
                + (f" for {programme.upper()}" if programme else "")
                + ". Open a notice to view or download the official file."
            )
        else:
            message = (
                "No published date-sheet notices were found"
                + (f" for {programme.upper()}" if programme else "")
                + ". Try asking again later, or check the university website."
            )
        yield {
            "type": "notice_list",
            "notices": cards,
            "message": message,
            "_query": {"via": "university_notices"},
        }
        yield {"type": "done", "chat_id": chat_id, "cited_chunks": []}
        state.last_intent = "notices"
    except Exception:
        yield {
            "type": "notice_list",
            "notices": [],
            "message": "I couldn't fetch the university notices right now. Please try again in a moment.",
            "_query": {"via": "university_notices"},
        }
        yield {"type": "done", "chat_id": chat_id, "cited_chunks": []}


def _notice_card(n: Any) -> dict[str, Any]:
    """Lightweight published-notice card for the chatbot renderer."""
    from app.notices.service import notice_dto

    d = notice_dto(n, include_entries=False)
    return {
        "id": d["id"],
        "title": d["title"],
        "notice_type": d["notice_type"],
        "exam_session_label": d["exam_session_label"],
        "programme_ids": d["programme_ids"],
        "categories": d["categories"],
        "published_at": d["published_at"],
        "file_url": f"/api/notices/{d['id']}/file",
    }


def _schedule_row(e: Any) -> dict[str, Any]:
    """One verified schedule row — mirrors the DateSheetEntry columns verbatim."""
    return {
        "notice_id": str(e.notice_id),
        "exam_date": e.exam_date,
        "day": e.day,
        "start_time": e.start_time,
        "end_time": e.end_time,
        "subject": e.subject,
        "subject_code": e.subject_code,
        "paper_code": e.paper_code,
        "venue": e.venue,
        "programme_id": e.programme_id,
        "programme_name": e.programme_name,
        "semester": e.semester,
        "stream": e.stream,
        "batch": e.batch,
    }


def _schedule_event(
    programme: str,
    semester: int | None,
    stream: str | None,
    rows: list[dict[str, Any]],
    cards: list[dict[str, Any]],
) -> dict[str, Any]:
    """Build the `date_sheet_schedule` SSE payload from verified rows only."""
    label = (
        f"{programme.upper()} Semester {semester}"
        if programme else
        f"Semester {semester}"
    )
    return {
        "type": "date_sheet_schedule",
        "programme": programme,
        "semester": semester,
        "stream": stream,
        "message": f"Here is the verified {label} examination schedule.",
        "schedule": rows,
        "documents": [
            {"id": c["id"], "title": c["title"], "file_url": c["file_url"]}
            for c in cards
        ],
        "_query": {"via": "university_notices"},
    }


def _render_comparison_rows(resolved: list[tuple[str, dict]]) -> list[dict[str, str]]:
    """Render a side-by-side comparison table from catalogue programme data."""
    labels = {
        "code": "Code",
        "name": "Name",
        "level": "Level",
        "duration_years": "Duration (years)",
        "total_credits": "Total Credits",
        "minor_count": "Minors Offered",
        "subject_count": "Subjects",
        "scheme_name": "Academic Scheme",
    }
    rows: list[dict[str, str]] = []

    def _code(pid: str, detail: dict) -> str:
        return str(detail.get("code") or pid).upper()

    def _value(detail: dict, field: str) -> str:
        value = detail.get(field)
        if value is None:
            return "—"
        if isinstance(value, bool):
            return "Yes" if value else "No"
        if isinstance(value, (list, tuple)):
            return f"{len(value)}" if value else "—"
        return str(value)

    for field in ("code", "name", "level", "scheme_name", "duration_years", "total_credits", "minor_count", "subject_count"):
        if field in ("code", "name"):
            continue
        if all(detail.get(field) is None for _, detail in resolved):
            continue
        rows.append({"label": labels[field], **{_code(pid, detail): _value(detail, field) for pid, detail in resolved}})

    fee_rows = {}
    for pid, detail in resolved:
        fee = detail.get("fee_structure")
        if isinstance(fee, list) and fee:
            fee_rows[_code(pid, detail)] = "; ".join(
                f"{e.get('label')}: {e.get('value')}" for e in fee if isinstance(e, dict) and e.get("value")
            ) or "—"
        elif fee:
            fee_rows[_code(pid, detail)] = str(fee)
        else:
            fee_rows[_code(pid, detail)] = "—"
    if any(v != "—" for v in fee_rows.values()):
        rows.append({"label": "Fee Structure", **fee_rows})

    rows.append({"label": "Eligibility", **{_code(pid, detail): str(detail.get("eligibility") or "—") for pid, detail in resolved}})
    return rows


# Plain-text responses for unsupported personal student-service requests.
# These MUST be a bare text token — no options, no programme picker, no
# forms — so the frontend never renders a fake lookup workflow.
_UNSUPPORTED_SERVICE_MESSAGES: dict[str, str] = {
    "results": "Sorry, student result information is not available through this chatbot.",
    "attendance": "Sorry, student attendance information is not available through this chatbot.",
    "admit_card": "Sorry, this chatbot does not currently provide admit card information.",
    "hall_ticket": "Sorry, hall ticket information is not available through this chatbot.",
    "internal_marks": "Sorry, internal marks information is not available through this chatbot.",
    "semester_result": "Sorry, semester result information is not available through this chatbot.",
    "transcript": "Sorry, transcript information is not available through this chatbot.",
    "backlog": "Sorry, backlog status information is not available through this chatbot.",
    "exam_form": "Sorry, examination form information is not available through this chatbot.",
    "student_portal": "Sorry, student portal information is not available through this chatbot.",
    "fee_receipt": "Sorry, student fee receipt information is not available through this chatbot.",
}


def _unavailable_service_event(family: str) -> dict[str, Any]:
    return {
        "type": "token",
        "text": _UNSUPPORTED_SERVICE_MESSAGES.get(
            family,
            "Sorry, that information is not available through this chatbot.",
        ),
    }


# Results chips (legacy) carried the semester as `results-sem-N`; the pattern
# is kept so a previously-clicked chip still resolves, but it now only
# PRESELECTS the semester inside the semester + examination-roll form.
_SEM_OPTION_ID = re.compile(r"^results-sem-(\d+)$")

# An examination roll number typed in the same message ("my exam roll number
# is 2300101") prefills the roll input — single-turn, never persisted in
# session/chat state, so it cannot leak into a later unrelated render.
_ROLL_IN_TEXT = re.compile(
    r"\b(?:examination\s+)?roll\s*(?:number|no\.?)?\s*(?:is\s+|:)?\s*"
    r"([A-Za-z0-9][A-Za-z0-9-]{1,49})\b"
)


def _results_events(
    db: Session,
    student_session: dict[str, Any],
    message: str,
    entities: Any,
):
    """SSE events for an authenticated student's own results (Phase B).

    ALWAYS renders the semester + examination-roll form (`results_form`), no
    matter what the student typed:
      - a typed semester or a legacy `results-sem-N` chip PRESELECTS it
      - a typed roll PREFILLS the roll input (ephemeral, single-turn)

    An allowlisted semester with no published attempt yields a plain safe
    message — never an error, never a hint about other students' data. Marks
    are shown ONLY after the student submits the exact (semester, roll) pair
    through the POST /view lookup — never from a chat message or a URL.
    """
    from app.student.gate import results_form_event
    from app.student_results import service as results_svc

    student_id = (student_session or {}).get("student_id")
    if not student_id:
        yield {"type": "token", "text": "Your session could not be verified. Please sign in again."}
        return

    semesters = results_svc.student_semesters(
        db, student_id, (student_session or {}).get("semester")
    )
    if not semesters:
        yield {
            "type": "token",
            "text": "No results are available for your profile yet. They will appear here as soon as they are published.",
        }
        return

    semester: int | None = None
    if entities is not None:
        semester = getattr(entities, "semester", None)
    if semester is None:
        m = _SEM_OPTION_ID.match((message or "").strip().lower())
        if m:
            semester = int(m.group(1))

    roll: str | None = None
    m = _ROLL_IN_TEXT.search((message or "").strip().lower())
    if m:
        candidate = m.group(1)
        if results_svc._ROLL_FULL_RE.fullmatch(candidate):
            roll = candidate

    if semester is not None and semester not in results_svc._ALLOWLIST:
        semester = None

    if semester is not None and not any(s["semester"] == semester for s in semesters):
        # Allowlisted but nothing published FOR THIS STUDENT: safe message only.
        yield {"type": "token", "text": f"No result is published for Semester {semester} yet."}
        return

    yield results_form_event(semesters, preselect=semester, roll=roll)


# Admit-card picker chips carry the semester as `admit_card_sem-N` (the
# "admit_card" substring keeps the planner routing to student_service); this
# pattern parses the semester back out when the student clicks a chip.
_ADMIT_OPTION_ID = re.compile(r"^admit_card_sem[- ]?(\d+)$")


def _admit_card_events(
    db: Session,
    student_session: dict[str, Any],
    message: str,
    entities: Any,
):
    """SSE events for an authenticated student's own admit cards (Phase C).

    Semester resolution order:
      1. entities.semester  — typed requests ("show my 3rd semester admit card")
      2. `admit_card_sem-N` — a chip id clicked from the semester picker
      3. otherwise the semester picker (one chip per available semester)
    A semester with no issued card gets a plain safe message — never an error,
    never a hint about other students' data.
    """
    from app.student.gate import admit_card_document_event, admit_card_semesters_event
    from app.student_admit_card import service as admit_card_svc

    student_id = (student_session or {}).get("student_id")
    if not student_id:
        yield {"type": "token", "text": "Your session could not be verified. Please sign in again."}
        return

    semesters = admit_card_svc.student_card_semesters(db, student_id)
    if not semesters:
        yield {
            "type": "token",
            "text": "No admit card is available for your profile yet. It will appear here once it is issued.",
        }
        return

    semester: int | None = None
    if entities is not None:
        semester = getattr(entities, "semester", None)
    if semester is None:
        m = _ADMIT_OPTION_ID.match((message or "").strip().lower())
        if m:
            semester = int(m.group(1))

    if semester is not None and semester not in admit_card_svc._ALLOWLIST:
        semester = None

    if semester is not None:
        document = admit_card_svc.student_card_document(db, student_id, semester)
        if document is None:
            yield {"type": "token", "text": f"No admit card is issued for Semester {semester} yet."}
        else:
            yield admit_card_document_event(semester, document)
        return

    yield admit_card_semesters_event(semesters)


# Exam-form picker chips carry exam type + semester as `exam_form{type}{sem}`
# (the "exam_form" substring keeps the planner routing to student_service);
# this pattern parses type + semester back out when the student clicks a chip.
_EXAM_OPTION_ID = re.compile(r"^exam_form([a-z]+)(\d+)$")

# Phase D2 session-driven chip ids (all keep the "exam_form" routing substring):
#   exam_form_fill            -> OPEN session picker
#   exam_form_print           -> own numbered forms picker
#   exam_form_pick{code}      -> fill the chosen OPEN session
#   exam_form_pay{form_id}    -> mock checkout (exam_form_pay event)
#   exam_form_view{form_id}   -> document preview (exam_form_doc event)
#   exam_form_dl{form_id}     -> document preview (frontend triggers PDF download)
#   exam_form_submit{form_id} -> Pending -> Submitted (server-gated)
_EXAM_FILL_ID = re.compile(r"^exam_form_fill$")
_EXAM_PRINT_ID = re.compile(r"^exam_form_print$")
_EXAM_PICK_ID = re.compile(r"^exam_form_pick([a-zA-Z0-9_\-]+)$")
_EXAM_ACTION_ID = re.compile(r"^exam_form_(pay|view|dl|submit)([0-9a-f-]{36})$")


def _exam_form_events(
    db: Session,
    student_session: dict[str, Any],
    message: str,
    entities: Any,
):
    """SSE events for an authenticated student's own exam forms (Phase D).

    Phase D2 (Exam Session model) resolution order:
      1. `exam_form_fill` / `exam_form_print` — landing actions
      2. `exam_form_pick{code}` — session chips from the OPEN-session picker
      3. `exam_form_pay|view|dl|submit{form_id}` — action chips on an own form
      4. legacy `exam_form{type}{sem}` chips + typed semester requests
    A selected session/form with no eligible access gets a plain safe message —
    never an error, never a hint about other students' data.
    """
    from app.student.gate import (
        exam_form_actions_event,
        exam_form_detail_event,
        exam_form_document_event,
        exam_form_pay_event,
        exam_form_picker_event,
        exam_form_print_picker_event,
        exam_form_sessions_event,
    )
    from app.student_exam_form import exam_session as es
    from app.student_exam_form import service as efs
    from app.student_exam_form.eligibility import evaluate_eligibility

    student_id = (student_session or {}).get("student_id")
    if not student_id:
        yield {"type": "token", "text": "Your session could not be verified. Please sign in again."}
        return

    msg = (message or "").strip().lower()

    # -- Natural-language exam form intents (typed / voice + auth resume) ------
    # The query-understanding preprocessor rewrites "fill" -> "final" and
    # "print" -> "price", so these must be caught on the ORIGINAL message
    # before any chip regex matching.  Remap to the canonical chip id so the
    # existing handlers process them identically to a chip click.
    if msg in ("student services", "student service"):
        msg = "student_exam_form"
    elif re.search(r"\bfill\b.*\bexam(?:ination)?\s*form\b", msg):
        msg = "exam_form_fill"
    elif re.search(r"\bprint\b.*\bexam(?:ination)?\s*form\b", msg):
        msg = "exam_form_print"
    elif re.search(r"\bexam(?:ination)?\s*form\b", msg):
        msg = "student_exam_form"

    # -- Hub entry: "student_exam_form" ---------------------------------------
    # With provisioned (legacy) forms the classic identity picker is kept for
    # backward compatibility; otherwise the session-driven Fill / Print landing
    # is shown once the student has an open session or a numbered form.
    if msg == "student_exam_form":
        legacy_forms = efs.student_form_semesters(db, student_id)
        if legacy_forms:
            records = efs.student_form_records(db, student_id)
            session_bound = any(r.get("session_code") for r in records)
            if not session_bound:
                yield exam_form_picker_event(legacy_forms)
                return
        yield {
            "type": "options",
            "title": "Exam Form",
            "message": "What would you like to do?",
            "options": [
                {"id": "exam_form_fill", "label": "Fill Examination Form"},
                {"id": "exam_form_print", "label": "Print an Existing Exam Form"},
            ],
        }
        return

    # -- Landing: Fill (open session picker) -------------------------------
    if _EXAM_FILL_ID.match(msg):
        records = efs.student_form_records(db, student_id)
        sessions = es.student_available_sessions(db, student_id)
        open_codes = {(s.get("code") or "").lower() for s in sessions}
        existing = [r for r in records if r.get("session_code") and r.get("form_no") and (r.get("session_code") or "").lower() in open_codes]
        if existing:
            form_rec = existing[0]
            doc = efs.student_form_document(db, student_id, form_rec["id"])
            if doc:
                status_msg = "Exam form already filled and submitted." if doc.get("form_status") in ("Submitted", "Approved") else "Exam form already filled."
                yield {"type": "token", "text": status_msg}
                doc["id"] = doc["form_id"]
                doc["fee_amount"] = doc.get("fee_total")
                yield exam_form_detail_event(doc)
                session_match = next((s for s in sessions if (s.get("code") or "").lower() == (form_rec.get("session_code") or "").lower()), None)
                yield exam_form_actions_event(doc, session_match)
                return
        if not sessions:
            yield {
                "type": "token",
                "text": "No OPEN exam session is available for your profile yet. It will appear here once the university opens applications.",
            }
            return
        yield exam_form_sessions_event(sessions)
        return

    # -- Landing: Print (own numbered forms picker) ------------------------
    if _EXAM_PRINT_ID.match(msg):
        records = efs.student_form_records(db, student_id)
        numbered = [r for r in records if r.get("form_no")]
        submitted = [r for r in numbered if r.get("form_status") in ("Submitted", "Approved")]
        if not submitted:
            yield {
                "type": "token",
                "text": "No submitted exam form is available.",
            }
            return
        yield exam_form_print_picker_event(submitted)
        return

    # -- Session pick (fill) --------------------------------------------------
    pick = _EXAM_PICK_ID.match(msg)
    if pick:
        code = pick.group(1)
        sessions = es.student_available_sessions(db, student_id)
        chosen = next((s for s in sessions if (s.get("code") or "").lower() == code), None)
        if chosen is None:
            yield {
                "type": "token",
                "text": "That exam session is not open for your profile (or it has closed). Please pick again from the list.",
            }
            return
        session_id = chosen["id"]
        # Duplicate prevention: if the student already has a form for this
        # session, show the existing application instead of creating a new one.
        records = efs.student_form_records(db, student_id)
        existing = next((r for r in records if r.get("session_code") and (r.get("session_code") or "").lower() == code), None)
        if existing:
            doc = efs.student_form_document(db, student_id, existing["id"])
            if doc:
                status_msg = "Exam form already filled and submitted." if doc.get("form_status") in ("Submitted", "Approved") else "Exam form already filled."
                yield {"type": "token", "text": status_msg}
                doc["id"] = doc["form_id"]
                doc["fee_amount"] = doc.get("fee_total")
                yield exam_form_detail_event(doc)
                yield exam_form_actions_event(doc, chosen)
                return
        # Deterministic eligibility gate BEFORE any form is created.
        from app.models import Student as _St

        student = db.get(_St, uuid.UUID(str(student_id)))
        session = es.get_session(db, session_id)
        eligibility = evaluate_eligibility(db, student, session)
        if not eligibility.get("eligible"):
            failed = [r["message"] for r in eligibility["rules"] if not r["passed"]]
            yield {
                "type": "token",
                "text": "Your profile does not meet the eligibility criteria for this exam session yet. " + " ".join(failed),
            }
            return
        try:
            form = efs.student_fill(db, student_id, {"exam_session_id": session_id})
        except ValueError as exc:
            yield {"type": "token", "text": str(exc)}
            return
        yield exam_form_detail_event(form)
        yield exam_form_actions_event(form, chosen)
        return

    # -- Own-form action chips (pay / view / download / submit) ----------------
    action = _EXAM_ACTION_ID.match(msg)
    if action:
        kind, form_id = action.group(1), action.group(2)
        try:
            form = efs.form_or_404(db, form_id)
        except ValueError:
            yield {"type": "token", "text": "That exam form could not be found."}
            return
        if str(form.student_id) != student_id:
            yield {"type": "token", "text": "You are not authorized to access that exam form."}
            return

        document = efs.student_form_document(db, student_id, form_id)
        if document is None:
            yield {"type": "token", "text": "That exam form could not be found."}
            return

        if kind == "pay":
            if (form.form_status or "Pending") != "Pending":
                yield {"type": "token", "text": "This exam form has already been submitted."}
                return
            if int(document.get("fee_total") or 0) <= 0:
                yield {"type": "token", "text": "There is no payable fee on this form (zero-fee session). It can be submitted directly."}
                yield exam_form_actions_event(form)
                return
            if (form.fee_status or "Unpaid") == "Paid":
                yield {"type": "token", "text": "Your exam fee is already paid."}
                yield exam_form_actions_event(form)
                return
            yield exam_form_pay_event(document)
            return

        if kind == "dl":
            efs.mark_form_printed(db, student_id, form_id)
            yield {
                "type": "token",
                "text": "Opening your exam form for print (PDF Download). “Download” saves the PDF on your device.",
            }

        if kind in ("view", "dl"):
            yield exam_form_document_event(document)
            return

        if kind == "submit":
            try:
                form_sub, was = efs.student_submit(db, student_id, form_id, True)
            except ValueError as exc:
                yield {"type": "token", "text": str(exc)}
                return
            yield {"type": "token", "text": f"Your exam form {form_sub.get('id', '')[:8]}… was submitted successfully."}
            yield exam_form_detail_event(form_sub)
            yield exam_form_actions_event(form_sub)
            return

    # -- Legacy picker flow (backward compatible) -------------------------------
    forms = efs.student_form_semesters(db, student_id)
    if not forms:
        yield {
            "type": "token",
            "text": "No Exam Form is available for your profile yet. It will appear here once it is provisioned.",
        }
        return

    exam_type: str | None = None
    semester: int | None = None
    m = _EXAM_OPTION_ID.match(msg)
    if m:
        exam_type = m.group(1).capitalize()
        semester = int(m.group(2))
    elif entities is not None:
        semester = getattr(entities, "semester", None)

    if semester is not None:
        allowed_types = {exam_type} if exam_type is not None else {"Regular"}
        for candidate in allowed_types:
            form = efs.student_form_by_identity(db, student_id, semester, candidate)
            if form is not None:
                yield exam_form_detail_event(efs.student_dto(form))
                return
        label = f"{exam_type or 'Regular'} · Semester {semester}"
        yield {
            "type": "token",
            "text": f"No exam form is available for {label} on your profile yet.",
        }
        return

    yield exam_form_picker_event(forms)


def _build_slot_fill_question(topic: str | None, field: str) -> dict[str, Any]:
    """Build the targeted missing-entity question for slot-fill."""
    topic_label = (topic or "that").replace("_", " ")
    message = f"I can help with {topic_label} — which programme would you like to check?"
    options: list[dict[str, str]] = []
    try:
        from app.catalogue.service import list_catalogue_programmes
        rows = list_catalogue_programmes()[:8]
        for row in rows:
            code = str(row.get("code") or "").strip()
            if code:
                options.append({"id": code.lower(), "label": code})
    except Exception:
        pass
    if not options:
        options = [{"id": p, "label": p.upper()} for p in ("bca", "bba", "ba", "bsc", "bcom", "mca", "mba", "mcom")]
    return {
        "type": "options",
        "title": "Which programme?",
        "message": message,
        "options": options,
    }


def _update_context_from_plan(ctx: ConversationContext, plan_result: Any) -> None:
    """Update context fields based on the executed plan."""
    extra = plan_result.extra or {}

    # College context update from extra data
    college_id = extra.get("college_id")
    if college_id:
        college = CollegeService.get_college(college_id)
        college_name = college["name"] if college else college_id
        target = plan_result.target or ""
        # Extract topic from target (e.g., "college/{id}/fee" -> "fee")
        college_topic = None
        if "/" in target:
            parts = target.split("/")
            if len(parts) >= 3:
                college_topic = parts[-1]
        update_context_for_college(ctx, college_id, college_name, college_topic)
        ctx.last_selected_entity = "college"
    else:
        # If no college in the plan and no college reference, keep existing college context
        # Only clear if user explicitly starts fresh
        pass

    if plan_result.action == "structured":
        target = plan_result.target or ""
        if target.startswith("college/"):
            # College structured response — don't touch non-college context fields
            return
        if "/" in target:
            prog, topic = target.split("/", 1)
            ctx.programme = prog
            ctx.programme_id = prog
            ctx.topic = topic
            ctx.last_selected_entity = "programme"
        elif target in PROGRAMME_ALIASES:
            ctx.programme = target
            ctx.programme_id = target
            _derive_level(ctx, target)
            ctx.last_selected_entity = "programme"
        elif target in ("ug", "pg", "phd", "integrated", "dyd"):
            ctx.level = target
        elif target in ("admissions", "fee", "courses", "results", "datesheet",
                         "syllabus", "scholarships", "notices", "downloads",
                         "hostel", "examination", "departments", "colleges", "contact"):
            ctx.domain = target
            # If user navigates to "colleges", don't carry old college context
            if target == "colleges":
                clear_college_context(ctx)

    elif plan_result.action == "navigation":
        # Try to derive context from target first
        target = plan_result.target
        if target:
            if target.startswith("college/"):
                return
            if target in PROGRAMME_ALIASES:
                ctx.programme = target
                ctx.programme_id = target
                ctx.last_selected_entity = "programme"
            elif target in ("ug", "pg", "phd", "integrated", "dyd"):
                ctx.level = target
            elif target in ("admissions", "fee", "courses", "results", "datesheet",
                             "syllabus", "scholarships", "notices", "downloads",
                             "hostel", "examination", "departments", "colleges", "contact"):
                ctx.domain = target
                if target == "colleges":
                    clear_college_context(ctx)
        # Also try to derive from the response title
        if plan_result.response:
            title = (plan_result.response.get("title") or "").lower()
            if not ctx.level:
                for lvl in ("ug", "pg", "phd", "integrated", "dyd"):
                    if lvl in title:
                        ctx.level = lvl
                        break
            if not ctx.domain:
                for dom in ("admissions", "courses", "fee", "results", "datesheet",
                            "syllabus", "scholarships", "notices", "downloads",
                            "hostel", "examination", "departments", "colleges", "contact"):
                    if dom in title:
                        ctx.domain = dom
                        break

    ctx.pending_clarification = None
    ctx.clarification_field = None


def _build_rag_context(ctx: ConversationContext, entities: Any) -> dict[str, Any]:
    """Build the retrieval context dict passed into RAG.

    Scopes retrieval to the active college (when the conversation is
    college-anchored) and augments it with programme/topic/scheme/semester
    context so retrieved chunks match the conversation, not just the words.
    """
    rag_ctx: dict[str, Any] = {}
    if ctx.college:
        rag_ctx["college_id"] = ctx.college
    if ctx.college_name:
        rag_ctx["college_name"] = ctx.college_name
    if entities is not None and getattr(entities, "programme", None):
        rag_ctx["programme"] = entities.programme
    elif ctx.programme:
        rag_ctx["programme"] = ctx.programme
    if entities is not None and getattr(entities, "programmes", None):
        rag_ctx["programmes"] = entities.programmes
    elif ctx.programmes:
        rag_ctx["programmes"] = ctx.programmes
    if entities is not None and getattr(entities, "topic", None):
        rag_ctx["topic"] = entities.topic
    elif ctx.topic:
        rag_ctx["topic"] = ctx.topic
    if getattr(ctx, "academic_scheme", None):
        rag_ctx["academic_scheme"] = ctx.academic_scheme
    if getattr(ctx, "catalogue_scheme_code", None) and not rag_ctx.get("academic_scheme"):
        rag_ctx["academic_scheme"] = ctx.catalogue_scheme_code
    if getattr(ctx, "catalogue_semester", None) is not None:
        rag_ctx["semester"] = ctx.catalogue_semester
    if getattr(ctx, "semester", None) and "semester" not in rag_ctx:
        try:
            rag_ctx["semester"] = int(ctx.semester)
        except (TypeError, ValueError):
            pass
    if getattr(ctx, "catalogue_category", None):
        rag_ctx["category"] = ctx.catalogue_category
    rag_ctx["scope"] = "college" if ctx.college else "university"
    return rag_ctx


def _update_context_from_rag(ctx: ConversationContext, entities: Any, query: str) -> None:
    """Update context after a RAG response."""
    if entities.programme:
        ctx.programme = entities.programme
        ctx.programme_id = entities.programme
        _derive_level(ctx, entities.programme)
    if entities.topic:
        ctx.topic = entities.topic
    if entities.domain:
        ctx.domain = entities.domain
    ctx.last_document = None
    ctx.pending_clarification = None
    ctx.clarification_field = None


def _derive_level(ctx: ConversationContext, programme: str) -> None:
    known_ug = {"ba", "bsc", "bcom", "bba", "bca", "btech", "bed"}
    known_pg = {"ma", "msc", "mcom", "mba", "mca", "med"}
    if programme in known_ug:
        ctx.level = "ug"
    elif programme in known_pg:
        ctx.level = "pg"
    elif programme == "phd":
        ctx.level = "phd"


# ---------------------------------------------------------------------------
# Clarification builder
# ---------------------------------------------------------------------------


def _build_clarification(ctx: ConversationContext, field: str | None) -> dict[str, Any]:
    """Build a clarification question for the user."""
    if field == "programme":
        return {
            "type": "options",
            "title": "Which programme?",
            "message": f"You selected {ctx.level.upper() if ctx.level else 'a'} level. Which specific programme are you interested in?",
            "options": [
                {"id": "bca", "label": "BCA"},
                {"id": "bba", "label": "BBA"},
                {"id": "ba", "label": "BA"},
                {"id": "bsc", "label": "B.Sc"},
                {"id": "bcom", "label": "B.Com"},
            ],
        }
    if field == "domain":
        return {
            "type": "options",
            "title": "How can I help you?",
            "message": "What would you like to know about?",
            "options": [
                {"id": "admissions", "label": "Admissions"},
                {"id": "fee", "label": "Fee Structure"},
                {"id": "courses", "label": "Courses"},
                {"id": "examination", "label": "Examinations"},
            ],
        }
    return {
        "type": "options",
        "title": "Can you clarify?",
        "message": "I'm not sure what you're looking for.",
        "options": [
            {"id": "admissions", "label": "Admissions"},
            {"id": "courses", "label": "Courses"},
            {"id": "fee", "label": "Fee"},
        ],
    }


# ---------------------------------------------------------------------------
# Context-to-response helper
# ---------------------------------------------------------------------------


def _add_context_to_response(response: dict, ctx: ConversationContext) -> None:
    """Attach context breadcrumb trail to the response for frontend rendering."""
    crumbs = []
    if ctx.college_name:
        crumbs.append(ctx.college_name)
    if ctx.college_programme:
        crumbs.append(ctx.college_programme.upper())
    elif ctx.college_topic and ctx.college_topic not in ("about",):
        crumbs.append(ctx.college_topic.replace("_", " ").title())
    if ctx.domain:
        crumbs.append(ctx.domain.title() if ctx.domain != "admissions" else "Admissions")
    if ctx.level:
        crumbs.append(ctx.level.upper())
    if ctx.programme:
        label = _get_programme_label(ctx.programme) or ctx.programme.upper()
        crumbs.append(label)
    if ctx.topic and ctx.topic not in ("", None):
        crumbs.append(ctx.topic.replace("_", " ").title())
    result: dict = {"breadcrumbs": crumbs} if crumbs else {}
    if ctx.programme:
        result["programme"] = ctx.programme
    if ctx.college:
        result["college"] = ctx.college
        result["college_name"] = ctx.college_name
    if result:
        response["context"] = result

    # Attach query understanding metadata to response
    if ctx.query_original and ctx.query_corrected:
        response["_query"] = {
            "original": ctx.query_original,
            "clean": ctx.query_clean,
            "corrected": True,
        }


# ---------------------------------------------------------------------------
# Breadcrumb helper
# ---------------------------------------------------------------------------


async def _update_nav_breadcrumb(
    chat_id: str,
    state: ConversationState,
    response: dict,
    text: str,
) -> None:
    title = response.get("title", "") or response.get("message", "")
    crumb = Breadcrumb(label=title or text, type=response.get("type", "nav"))
    await push_breadcrumb(chat_id, crumb)


# ---------------------------------------------------------------------------
# Label helper
# ---------------------------------------------------------------------------


def _get_programme_label(programme_id: str) -> str | None:
    detail = _PROGRAMME_DETAILS.get(programme_id)
    if detail:
        return detail.get("title")
    return None




# ---------------------------------------------------------------------------
# Authority routing
# ---------------------------------------------------------------------------


async def _handle_authority_route(
    db: Session,
    user_id: str,
    message: str,
    chat_id: str,
    state: ConversationState,
    plan_result: Any,
) -> AsyncGenerator[dict[str, Any], None]:
    """Handle authority / escalation requests from the planner.

    Yields a contact card or multiple choices as SSE events.
    """
    authorities = (plan_result.extra or {}).get("authorities", [])
    if not authorities:
        from app.authority.matcher import find_authority
        authorities = find_authority(message, top_k=3)

    if not authorities:
        yield {
            "type": "options",
            "title": "Contact University Office",
            "message": "I couldn't find a specific office for your query. Please select a department below or describe your issue in more detail.",
            "options": [
                {"id": "admissions", "label": "Admissions Office"},
                {"id": "examinations", "label": "Controller of Examinations"},
                {"id": "academic", "label": "Academic Section"},
                {"id": "helpdesk", "label": "Student Help Desk"},
                {"id": "it", "label": "IT Cell"},
                {"id": "general", "label": "General Enquiry"},
            ],
        }
        yield {"type": "done", "chat_id": chat_id, "cited_chunks": []}
        state.last_intent = "authority_selection"
        return

    if len(authorities) == 1:
        auth = authorities[0]
        yield _build_authority_card(auth)
        yield {"type": "done", "chat_id": chat_id, "cited_chunks": []}
        state.last_intent = "authority_contact"
        return

    # Multiple matches — let the user choose
    yield {
        "type": "options",
        "title": "Which office are you looking for?",
        "message": f"I found {len(authorities)} relevant offices. Please select one:",
        "options": [
            {
                "id": a.get("id", ""),
                "label": f"{a.get('authority_name', '')} ({a.get('department_name', '')})",
                "description": a.get("designation") or a.get("description", "")[:80],
            }
            for a in authorities
        ],
    }
    yield {"type": "done", "chat_id": chat_id, "cited_chunks": []}
    state.last_intent = "authority_selection"


def _build_authority_card(authority: dict[str, Any]) -> dict[str, Any]:
    """Format an authority record as a structured contact card."""
    from app.authority.matcher import format_contact_card
    return {
        "type": "detail",
        "title": authority.get("authority_name", "University Office"),
        "message": authority.get("description", ""),
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
            {"id": f"call_{authority.get('id', '')}", "label": f"Call {authority.get('phone', '')}", "type": "phone"},
            {"id": f"email_{authority.get('id', '')}", "label": f"Email {authority.get('email', '')}", "type": "email"},
            {"id": f"map_{authority.get('id', '')}", "label": "View on Map", "type": "map", "url": authority.get("office_location")},
            {"id": f"website_{authority.get('id', '')}", "label": "Visit Website", "type": "url", "url": authority.get("website")},
        ],
        "extra": format_contact_card(authority),
    }

