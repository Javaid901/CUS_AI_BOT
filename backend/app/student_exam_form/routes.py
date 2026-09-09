"""
backend/app/student_exam_form/routes.py

Student Exam Form endpoints (Phase D).

  Student (session cookie, server-validated):
    GET    /api/student/exam-forms                  form picker (own available forms)
    GET    /api/student/exam-forms/{id}/print       printable view of OWN form
    POST   /api/student/exam-forms                  Fill: create own Pending form
    POST   /api/student/exam-forms/{id}/submit      affirm → Pending → Submitted

  Super Admin (require_superadmin on every endpoint):
    GET    /api/admin/exam-forms                    list / search / filter / paginate
    POST   /api/admin/exam-forms                    create a form record
    PATCH  /api/admin/exam-forms/{id}               edit administrative data
    POST   /api/admin/exam-forms/{id}/status        deterministic status transition
    POST   /api/admin/exam-forms/preview            upload CSV/XLSX → validate (writes nothing)
    POST   /api/admin/exam-forms/confirm            apply validated rows in ONE transaction
    DELETE /api/admin/exam-forms/{id}               withdraw a form (audited)

Security contract:
  - The student identity is ALWAYS derived from the resolved StudentSession
    cookie. No `student_id` / `reg_no` / `roll_no` parameter is accepted, so
    cross-student access is structurally impossible (IDOR-safe).
  - Printing/filling/submitting are scoped to the student's own forms: the
    server re-checks `form.student_id == session student_id` on every owned-row
    operation, and {id}-based endpoints 404/403 for anyone else's form.
  - Fee/payment fields (fee_status, fee_amount, transaction_id,
    submission_date) are accepted ONLY on Super-Admin bodies; the student
    schema cannot express them. Students can never forge payment state.
  - Import preview writes nothing; confirm re-validates every row server-side
    and commits atomically (rollback on any failure).
  - Audit records only reg/sem/filename + count — never DOB, credentials,
    payment transaction ids, or unrelated student data.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from sqlalchemy.orm import Session

from app.auth.security import require_superadmin
from app.config import settings
from app.database import get_db
from app.models import User
from app.student.session import resolve_session
from app.student_exam_form import service as efs
from app.student_exam_form.schemas import (
    AdminFormCreate,
    AdminFormUpdate,
    FormStatusUpdate,
    ImportBundle,
    StudentFillCreate,
    StudentSubmitBody,
)
from app.utils.logging import audit

router = APIRouter(prefix=f"{settings.API_PREFIX}/student/exam-forms", tags=["student-exam-form"])
admin_router = APIRouter(prefix=f"{settings.API_PREFIX}/admin/exam-forms", tags=["student-exam-form-admin"])

_superadmin = Depends(require_superadmin)


def _ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _require_student_snapshot(request: Request, db: Session) -> dict:
    raw = request.cookies.get(settings.STUDENT_SESSION_COOKIE)
    resolved = resolve_session(db, raw)
    if not resolved:
        raise HTTPException(
            status_code=401,
            detail="Your student session is invalid or has expired. Please sign in again.",
        )
    return resolved


def _value_error(exc: ValueError) -> HTTPException:
    msg = str(exc)
    if "not found" in msg:
        return HTTPException(status_code=404, detail=msg)
    if "already" in msg or "duplicate" in msg.lower() or "submitted" in msg.lower():
        return HTTPException(status_code=409, detail=msg)
    if "not authorized" in msg:
        return HTTPException(status_code=403, detail=msg)
    return HTTPException(status_code=422, detail=msg)


# --------------------------------------------------------------------------- #
# Student-facing
# --------------------------------------------------------------------------- #
@router.get("")
def my_exam_forms(request: Request, db: Session = Depends(get_db)):
    """The student's own available forms (fillable + submitted/printing)."""
    identity = _require_student_snapshot(request, db)
    available = efs.student_form_semesters(db, identity["student_id"])
    return {"forms": available}


@router.post("", status_code=201)
def fill_exam_form(
    body: StudentFillCreate,
    request: Request,
    db: Session = Depends(get_db),
):
    """Fill: create a new Pending exam form for the student's own identity."""
    identity = _require_student_snapshot(request, db)
    try:
        form = efs.student_fill(db, identity["student_id"], body.model_dump())
    except ValueError as exc:
        raise _value_error(exc)
    audit(
        db, "student_exam_form.fill", actor_id=identity.get("actor_id"),
        actor_role="student", target=form["id"],
        detail=f"Student filled an exam form ({form['exam_type']}, semester {form['semester']})",
        ip=_ip(request),
    )
    return form


@router.post("/{form_id}/submit")
def submit_exam_form(
    form_id: str,
    body: StudentSubmitBody,
    request: Request,
    db: Session = Depends(get_db),
):
    """Affirm the filled form — server performs Pending → Submitted + stamps date."""
    identity = _require_student_snapshot(request, db)
    try:
        form, was_submitted = efs.student_submit(db, identity["student_id"], form_id, body.confirm)
    except ValueError as exc:
        raise _value_error(exc)
    if was_submitted:
        audit(
            db, "student_exam_form.submit", actor_id=identity.get("actor_id"),
            actor_role="student", target=form["id"],
            detail=f"Student submitted an exam form ({form['exam_type']}, semester {form['semester']})",
            ip=_ip(request),
        )
    return form


@router.get("/{form_id}/print")
def print_exam_form(
    form_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Print/view payload for the student's OWN form (server-scoped)."""
    identity = _require_student_snapshot(request, db)
    try:
        form = efs.form_or_404(db, form_id)
    except ValueError as exc:
        raise _value_error(exc)
    if str(form.student_id) != identity["student_id"]:
        raise HTTPException(status_code=403, detail="You are not authorized to access this form.")
    return {"form": efs.student_dto(form)}


# --------------------------------------------------------------------------- #
# Super-Admin: management list + create / update / status / delete + import
# --------------------------------------------------------------------------- #
@admin_router.get("")
def list_exam_forms(
    db: Session = Depends(get_db),
    current: User = _superadmin,
    q: str | None = Query(None, max_length=200, description="Search reg no or student name"),
    semester: int | None = Query(None, ge=1),
    exam_type: str | None = Query(None, max_length=50),
    form_status: str | None = Query(None, max_length=50),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    try:
        return efs.list_exam_forms(db, q=q, semester=semester, exam_type=exam_type,
                                   form_status=form_status, page=page, page_size=page_size)
    except ValueError as exc:
        raise _value_error(exc)


@admin_router.post("", status_code=201)
def create_exam_form(
    body: AdminFormCreate,
    request: Request,
    db: Session = Depends(get_db),
    current: User = _superadmin,
):
    try:
        form = efs.create_exam_form(db, body)
    except ValueError as exc:
        raise _value_error(exc)
    audit(
        db, "student_exam_form.create", actor_id=str(current.id), actor_role=current.role,
        target=form["reg_no"],
        detail=f"Created exam form for {form['reg_no']} ({form['exam_type']}, semester {form['semester']})",
        ip=_ip(request),
    )
    return form


@admin_router.patch("/{form_id}")
def update_exam_form(
    form_id: str,
    body: AdminFormUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current: User = _superadmin,
):
    try:
        form = efs.update_exam_form(db, form_id, body)
    except ValueError as exc:
        raise _value_error(exc)
    audit(
        db, "student_exam_form.update", actor_id=str(current.id), actor_role=current.role,
        target=form["reg_no"],
        detail=f"Updated exam form for {form['reg_no']} (semester {form['semester']})",
        ip=_ip(request),
    )
    return form


@admin_router.post("/{form_id}/status")
def update_form_status(
    form_id: str,
    body: FormStatusUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current: User = _superadmin,
):
    try:
        form = efs.set_exam_form_status(db, form_id, body.form_status)
    except ValueError as exc:
        raise _value_error(exc)
    audit(
        db, "student_exam_form.status_change", actor_id=str(current.id), actor_role=current.role,
        target=form["reg_no"],
        detail=f"Changed exam form status for {form['reg_no']} to '{body.form_status}'",
        ip=_ip(request),
    )
    return form


@admin_router.post("/preview")
def preview_import(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current: User = _superadmin,
):
    content = file.file.read()
    return efs.preview_import_file(db, file.filename or "", content)


@admin_router.post("/confirm")
def confirm_import(
    body: ImportBundle,
    request: Request,
    db: Session = Depends(get_db),
    current: User = _superadmin,
):
    try:
        count = efs.apply_import(db, body.rows)
    except efs.ImportDataError as exc:
        raise HTTPException(
            status_code=409 if exc.duplicate_only else 422,
            detail=exc.summary,
        )
    audit(
        db, "student_exam_form.import", actor_id=str(current.id), actor_role=current.role,
        target=body.filename or "exam form import",
        detail=f"Imported {count} exam form entries from '{body.filename or 'upload'}'. Rows: {count}.",
        ip=_ip(request),
    )
    return {"imported": count, "message": f"Imported {count} exam form entries."}


@admin_router.delete("/{form_id}")
def delete_exam_form(
    form_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current: User = _superadmin,
):
    try:
        form = efs.delete_exam_form(db, form_id)
    except ValueError as exc:
        raise _value_error(exc)
    audit(
        db, "student_exam_form.delete", actor_id=str(current.id), actor_role=current.role,
        target=form["reg_no"],
        detail=f"Withdrew exam form for {form['reg_no']} (semester {form['semester']})",
        ip=_ip(request),
    )
    return {"deleted": True, "reg_no": form["reg_no"], "semester": form["semester"]}
