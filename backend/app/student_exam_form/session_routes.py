"""
backend/app/student_exam_form/session_routes.py

Exam Session endpoints (super-admin provisioning).

  Super Admin (require_superadmin on every endpoint):
    GET    /api/admin/exam-sessions               list / search / filter / paginate
    POST   /api/admin/exam-sessions               create a provisioned session
    GET    /api/admin/exam-sessions/{id}          session detail
    PATCH  /api/admin/exam-sessions/{id}          edit provisioning data
    POST   /api/admin/exam-sessions/{id}/status   deterministic status transition
    DELETE /api/admin/exam-sessions/{id}          delete (only when no forms exist)

Security contract:
  - Session mutations are SUPER-ADMIN ONLY via require_superadmin.
  - Fees are server-owned: a student can never express base/late fee, status,
    or the form-seq counter; they only read OPEN sessions for their own profile.
  - Audit records only session code/programme + count — never payments,
    credentials or unrelated student data.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.auth.security import require_superadmin
from app.config import settings
from app.database import get_db
from app.models import User
from app.student_exam_form import exam_session as es
from app.student_exam_form.schemas import (
    ExamSessionCreate,
    ExamSessionStatusUpdate,
    ExamSessionUpdate,
)
from app.utils.logging import audit

router = APIRouter(prefix=f"{settings.API_PREFIX}/admin/exam-sessions", tags=["exam-session-admin"])

_superadmin = Depends(require_superadmin)


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _value_error(exc: ValueError) -> HTTPException:
    msg = str(exc)
    if "not found" in msg:
        return HTTPException(status_code=404, detail=msg)
    if "already" in msg.lower() or "duplicate" in msg.lower():
        return HTTPException(status_code=409, detail=msg)
    if "archive" in msg.lower():
        return HTTPException(status_code=409, detail=msg)
    return HTTPException(status_code=422, detail=msg)


@router.get("")
def list_sessions(
    db: Session = Depends(get_db),
    current: User = _superadmin,
    q: str | None = Query(None, max_length=200, description="Search session name or code"),
    programme: str | None = Query(None, max_length=50),
    semester: int | None = Query(None, ge=1),
    status: str | None = Query(None, max_length=20),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    try:
        return es.list_sessions(db, q=q, programme=programme, semester=semester,
                                status=status, page=page, page_size=page_size)
    except ValueError as exc:
        raise _value_error(exc)


@router.post("", status_code=201)
def create_session(
    body: ExamSessionCreate,
    request: Request,
    db: Session = Depends(get_db),
    current: User = _superadmin,
):
    try:
        session = es.create_session(db, body.model_dump())
    except ValueError as exc:
        raise _value_error(exc)
    audit(
        db, "exam_session.create", actor_id=str(current.id), actor_role=current.role,
        target=session["code"],
        detail=f"Created exam session '{session['name']}' ({session['code']}, programme {session['programme']})",
        ip=_ip(request),
    )
    return session


@router.get("/{session_id}")
def get_session(
    session_id: str,
    db: Session = Depends(get_db),
    current: User = _superadmin,
):
    try:
        session = es.get_session(db, session_id)
    except ValueError as exc:
        raise _value_error(exc)
    return es.session_dto(session, counts=True)


@router.patch("/{session_id}")
def update_session(
    session_id: str,
    body: ExamSessionUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current: User = _superadmin,
):
    try:
        session = es.update_session(db, session_id, body.model_dump(exclude_unset=True))
    except ValueError as exc:
        raise _value_error(exc)
    audit(
        db, "exam_session.update", actor_id=str(current.id), actor_role=current.role,
        target=session["code"],
        detail=f"Updated exam session '{session['code']}' (programme {session['programme']}, status {session['status']})",
        ip=_ip(request),
    )
    return session


@router.post("/{session_id}/status")
def set_session_status(
    session_id: str,
    body: ExamSessionStatusUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current: User = _superadmin,
):
    try:
        session = es.set_session_status(db, session_id, body.status)
    except ValueError as exc:
        raise _value_error(exc)
    audit(
        db, "exam_session.status_change", actor_id=str(current.id), actor_role=current.role,
        target=session["code"],
        detail=f"Changed exam session '{session['code']}' status to '{body.status}'",
        ip=_ip(request),
    )
    return session


@router.delete("/{session_id}")
def delete_session(
    session_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current: User = _superadmin,
):
    try:
        session = es.delete_session(db, session_id)
    except ValueError as exc:
        raise _value_error(exc)
    audit(
        db, "exam_session.delete", actor_id=str(current.id), actor_role=current.role,
        target=session["code"],
        detail=f"Deleted exam session '{session['code']}' (programme {session['programme']})",
        ip=_ip(request),
    )
    return {"deleted": True, "code": session["code"], "name": session["name"]}