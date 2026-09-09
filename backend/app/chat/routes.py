"""
backend/app/chat/routes.py

Chat endpoint — thin SSE layer over the Orchestration Engine,
now fronted by the Admission Controller for intelligent request management.

POST /api/chat/ask
  Body: {"message": str, "chat_id": str|null, "stream": bool}
  Auth: Bearer JWT

Response: Server-Sent Events stream:
  event: queued\ndata: {...}               (request queued)
  event: processing\ndata: {...}           (processing started)
  data: <token>\n\n                        (LLM streaming token)
  event: options\ndata: {...}\n\n           (structured navigation options)
  event: detail\ndata: {...}\n\n            (structured detail card)
  event: grievance\ndata: {...}\n\n         (grievance intake prefill)
  event: logout\ndata: {"done":true,...}\n\n (Student Services logged out)
  event: done\ndata: {"chat_id":"...",...}\n\n (end of response)
  event: error\ndata: {"message":"..."}\n\n  (error)
"""

from __future__ import annotations

import json
import uuid

from app.auth.security import get_current_user
from app.chat.intent_router import get_nav_path, set_nav_path
from app.config import settings
from app.database import get_db
from app.models import User
from app.orchestrator.engine import process
from app.request_manager import admission_controller
from app.student.logout import detect_logout_command
from app.student.session import classify_stale_session, resolve_session, revoke_session
from app.utils.logging import audit
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

router = APIRouter(prefix=f"{settings.API_PREFIX}/chat", tags=["chat"])


class AskRequest(BaseModel):
    message: str
    chat_id: str | None = None
    stream: bool = True


def _sse(event: str | None, data: str) -> str:
    """Serialize an SSE frame.

    Multi-line token text is framed correctly: every line of `data` gets its
    own "data:" prefix so a "\n" inside a generated answer can never break
    the stream (lines without a prefix would be silently dropped by the
    client's EventSource parser, truncating the answer).
    """
    if event:
        return f"event: {event}\ndata: {data}\n\n"
    lines = data.splitlines() or [""]
    return "".join(f"data: {line}\n" for line in lines) + "\n"


def _structured_event(event_type: str, payload: dict) -> str:
    return f"event: {event_type}\ndata: {json.dumps(payload)}\n\n"


def _audit_message(message: str) -> str:
    return message


@router.post("/ask")
async def ask(
    body: AskRequest,
    request: Request,
    db=Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    if not body.message or not body.message.strip():
        raise HTTPException(status_code=400, detail="Empty message")

    chat_id = body.chat_id or ""
    if not chat_id:
        chat_id = "anon_" + uuid.uuid4().hex[:12]
    message = body.message.strip()
    client_ip = request.client.host if request.client else None
    user_id = str(current_user.id)
    user_role = current_user.role

    # Resolve the Student Services cookie server-side. The engine's
    # student_service handler uses this as the SOLE source of truth for auth;
    # the raw cookie token is never read by the frontend or logged.
    raw_student_sid = request.cookies.get(settings.STUDENT_SESSION_COOKIE)
    if raw_student_sid:
        student_session = resolve_session(db, raw_student_sid)
        if student_session:
            student_auth_kind = "valid"
        else:
            # Cookie sent but did NOT resolve. Classify (wording only) so a
            # revoked token says "session has ended" and a lapsed token says
            # "session has expired" — never exposing which token or why.
            student_auth_kind = classify_stale_session(db, raw_student_sid)
    elif request.cookies.get(settings.STUDENT_LOGOUT_MARKER_COOKIE):
        # No session cookie but this browser logged out recently: the student
        # explicitly ended their sign-in, so the gate must not claim a fresh
        # expiry ("Your session has ended. Please log in again.").
        student_session = None
        student_auth_kind = "revoked"
    else:
        student_session = None
        student_auth_kind = "none"

    # Deterministic Student Services logout — resolved BEFORE the Admission
    # Controller and the LLM so it is immediate and can never be mis-routed.
    # Only the StudentServices session is revoked; the admin JWT and the
    # regular chat guest identity are untouched.
    if detect_logout_command(message):
        was_authenticated = student_session is not None
        revoked = revoke_session(db, raw_student_sid)
        audit(
            db, "student_logout", actor_id=user_id, actor_role=user_role,
            detail=(
                "student logout via chat command executed"
                if was_authenticated else
                "student logout via chat command (no active sign-in)"
            ),
            ip=client_ip,
        )

        if was_authenticated:
            logout_text = "Logged out successfully."
        else:
            logout_text = "You are not signed in to Student Services."

        async def logout_stream():
            yield _sse(None, logout_text)
            yield _structured_event("logout", {"done": True, "revoked": bool(revoked)})
            yield _sse("done", json.dumps({"chat_id": chat_id, "cited_chunks": []}))

        logout_response = StreamingResponse(
            logout_stream(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "X-Accel-Buffering": "no",
                "Connection": "keep-alive",
            },
        )
        logout_response.delete_cookie(settings.STUDENT_SESSION_COOKIE, path=f"{settings.API_PREFIX}/")
        # Non-credential UX marker: next time this browser asks for a protected
        # service the gate says the session "ended" (not "expired"). It grants
        # nothing — access still requires a valid session cookie.
        if was_authenticated:
            logout_response.set_cookie(
                settings.STUDENT_LOGOUT_MARKER_COOKIE, "1",
                max_age=3600,
                httponly=True,
                secure=settings.cookie_secure,
                samesite="lax",
                path=f"{settings.API_PREFIX}/",
            )
        return logout_response

    async def _run_orchestrator(uid: str, msg: str, cid: str):
        """Wrapper that binds the DB session into the orchestrator."""
        async for event in process(
            db, uid, msg, cid,
            student_session=student_session,
            student_auth_kind=student_auth_kind,
        ):
            yield event

    async def event_stream():
        request_id = uuid.uuid4().hex[:12]
        try:
            # Wrap the orchestrator with admission control
            async for event in admission_controller.admit(
                user_id=user_id,
                message=message,
                chat_id=chat_id,
                executor=_run_orchestrator,
            ):
                etype = event["type"]

                # Admission controller events
                if etype == "queued":
                    yield _sse("queued", json.dumps({
                        "position": event.get("position"),
                        "estimated_wait_sec": event.get("estimated_wait_sec"),
                        "action": event.get("action"),
                    }))
                    continue

                if etype == "processing":
                    yield _sse("processing", json.dumps({
                        "action": event.get("action"),
                    }))
                    continue

                # Orchestrator events (delegated to process())
                if etype == "token":
                    yield _sse(None, event["text"])

                elif etype in ("options", "detail", "auth_form", "results_form", "grievance"):
                    yield _structured_event(etype, event)
                    audit(
                        db, "chat",
                        actor_id=user_id,
                        actor_role=user_role,
                        detail=f"[{etype}] {_audit_message(message)}",
                        ip=client_ip,
                    )

                elif etype == "done":
                    # Migrate nav state from anonymous session to real conversation.
                    real_id = event.get("chat_id", chat_id)
                    if chat_id.startswith("anon_") and real_id != chat_id:
                        nav_path = get_nav_path(chat_id)
                        if nav_path:
                            set_nav_path(real_id, nav_path)
                    yield _sse("done", json.dumps(event))

                elif etype == "error":
                    yield _sse("error", json.dumps({
                        "message": event["message"],
                        "ref": event.get("ref") or request_id,
                    }))

        except Exception as exc:
            # Never leak internal exception text to the user; log the full
            # trace with a correlation id so support can find it.
            from app.utils.logging import log
            log.error("chat stream failed request_id=%s chat=%s user=%s: %s",
                      request_id, chat_id, user_id, exc, exc_info=True)
            yield _sse("error", json.dumps({
                "message": "Something went wrong while preparing your answer. Please try again in a moment.",
                "ref": request_id,
            }))

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )
