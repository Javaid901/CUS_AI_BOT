"""
backend/app/student_admin/routes.py

Super Admin → Student Services → Students (Phase A).

  GET    /api/admin/students            list / search / paginate
  GET    /api/admin/students/search     READ-ONLY Student Search (name / class
                                           roll no / registration no)
  POST   /api/admin/students            create (DOB becomes the password)
  GET    /api/admin/students/{id}       single student (profile view)
  PATCH  /api/admin/students/{id}       edit allowed profile/academic fields
  DELETE /api/admin/students/{id}       PERMANENT delete (student + sessions +
                                           results + admit cards + exam forms)
  POST   /api/admin/students/{id}/toggle        activate / deactivate (+ session revoke)
  POST   /api/admin/students/{id}/reset-dob     reset DOB credential (+ revoke all sessions)

Authorization: every endpoint requires require_superadmin (server-side).
The student_id path value is never a permission token — the backend validates
role first and existence second. A normal Admin / User / unauthenticated caller
gets 403 / 403 / 401 regardless of which id they supply (IDOR-safe).

DELETE is a permanent database delete (no soft delete, no archive). It removes
the student row plus every linked record (sessions, results, admit cards, exam
forms and the other Service children) atomically; grievance history survives
with its student reference nulled. See service.delete_student.

Audit: simple metadata (student_id + action + actor + outcome). Credential
values (DOB), hashes and tokens are never written to audit detail.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.orm import Session

from app.auth.security import require_superadmin
from app.config import settings
from app.database import get_db
from app.models import User
from app.student_admin import service as students_svc
from app.student_admin.schemas import StudentCreate, StudentResetDob, StudentUpdate
from app.utils.logging import audit

router = APIRouter(prefix=f"{settings.API_PREFIX}/admin/students", tags=["student-admin"])

_superadmin = Depends(require_superadmin)


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _value_error(exc: ValueError) -> HTTPException:
    msg = str(exc)
    if "not found" in msg:
        return HTTPException(status_code=404, detail=msg)
    if "already" in msg:
        return HTTPException(status_code=409, detail=msg)
    return HTTPException(status_code=422, detail=msg)


@router.get("")
def list_students(
    db: Session = Depends(get_db),
    current: User = _superadmin,
    q: str | None = Query(None, max_length=200, description="Search registration number or name"),
    status: str | None = Query(None, pattern="^(active|inactive)?$", description="Filter by auth state"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    return students_svc.list_students(db, q=q, status=status, page=page, page_size=page_size)


@router.post("", status_code=201)
def create_student(
    body: StudentCreate,
    request: Request,
    db: Session = Depends(get_db),
    current: User = _superadmin,
):
    try:
        student = students_svc.create_student(db, body)
    except ValueError as exc:
        raise _value_error(exc)
    audit(
        db, "student.create", actor_id=str(current.id), actor_role=current.role,
        target=student["reg_no"], detail=f"Created student {student['reg_no']}",
        ip=_ip(request),
    )
    return student


@router.get("/search")
def search_students(
    db: Session = Depends(get_db),
    current: User = _superadmin,
    q: str | None = Query(None, max_length=200, description="Name, registration number or class/college roll number"),
    status: str | None = Query(None, pattern="^(active|inactive)?$", description="Optionally restrict to auth state"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """READ-ONLY Student Search (Super Admin only, same boundary as the whole
    Students area). Returns a safe allowlist — never DOB, hashed_password or
    session material. A search with no match is a normal 200 + empty list."""
    return students_svc.search_students(db, q=q, status=status, page=page, page_size=page_size)


@router.get("/{student_id}")
def get_student(
    student_id: str,
    db: Session = Depends(get_db),
    current: User = _superadmin,
):
    try:
        return students_svc.get_student(db, student_id)
    except ValueError as exc:
        raise _value_error(exc)


@router.patch("/{student_id}")
def update_student(
    student_id: str,
    body: StudentUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current: User = _superadmin,
):
    try:
        student = students_svc.update_student(db, student_id, body)
    except ValueError as exc:
        raise _value_error(exc)
    audit(
        db, "student.update", actor_id=str(current.id), actor_role=current.role,
        target=student["reg_no"], detail=f"Updated student {student['reg_no']}",
        ip=_ip(request),
    )
    return student


@router.delete("/{student_id}")
def delete_student(
    student_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current: User = _superadmin,
):
    try:
        deleted = students_svc.delete_student(db, student_id)
    except ValueError as exc:
        raise _value_error(exc)
    audit(
        db, "student.delete", actor_id=str(current.id), actor_role=current.role,
        target=deleted["reg_no"],
        detail=(
            f"Permanently deleted student {deleted['reg_no']} "
            f"(student_id={deleted['id']}) outcome=success"
        ),
        ip=_ip(request),
    )
    return {"deleted": True, "reg_no": deleted["reg_no"]}


@router.post("/{student_id}/toggle")
def toggle_student(
    student_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current: User = _superadmin,
):
    try:
        student = students_svc.toggle_active(db, student_id)
    except ValueError as exc:
        raise _value_error(exc)
    audit(
        db, "student.toggle", actor_id=str(current.id), actor_role=current.role,
        target=student["reg_no"],
        detail=f"{'Activated' if student['is_active'] else 'Deactivated'} {student['reg_no']}",
        ip=_ip(request),
    )
    return student


@router.post("/{student_id}/reset-dob")
def reset_student_dob(
    student_id: str,
    body: StudentResetDob,
    request: Request,
    db: Session = Depends(get_db),
    current: User = _superadmin,
):
    try:
        student = students_svc.reset_dob_password(db, student_id, body.dob)
    except ValueError as exc:
        raise _value_error(exc)
    audit(
        db, "student.reset_dob", actor_id=str(current.id), actor_role=current.role,
        target=student["reg_no"], detail=f"Reset DOB credential for {student['reg_no']}",
        ip=_ip(request),
    )
    return student