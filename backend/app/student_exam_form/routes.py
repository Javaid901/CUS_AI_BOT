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
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.auth.security import require_superadmin
from app.config import settings
from app.database import get_db
from app.models import User
from app.student.session import resolve_session
from app.student_exam_form import exam_session as es
from app.student_exam_form import payment as efpay
from app.student_exam_form import render as efrender
from app.student_exam_form import service as efs
from app.student_exam_form.schemas import (
    AdminFormCreate,
    AdminFormUpdate,
    FormStatusUpdate,
    ImportBundle,
    StudentFillCreate,
    StudentPrintBody,
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
# Phase D2 student endpoints: exam sessions, document, PDF print, payments
# --------------------------------------------------------------------------- #
@router.get("/sessions")
def my_exam_sessions(request: Request, db: Session = Depends(get_db)):
    """OPEN exam sessions matching the student's OWN profile (fill picker)."""
    identity = _require_student_snapshot(request, db)
    sessions = es.student_available_sessions(db, identity["student_id"])
    return {"sessions": sessions}


@router.get("/{form_id}/document")
def exam_form_document(
    form_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Printable HTML document of the student's OWN exam form (chat preview).

    The same document feeds both view and print → a single source of truth for
    the printed record (form_no, identity, subjects, fee, reference).
    """
    identity = _require_student_snapshot(request, db)
    data = efs.student_form_document(db, identity["student_id"], form_id)
    if data is None:
        raise HTTPException(status_code=403, detail="You are not authorized to access this form.")
    html = efrender.render_exam_form_document_html(data)
    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={
            "X-Robots-Tag": "noindex, nofollow",
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'",
        },
    )


@router.post("/{form_id}/print")
def download_exam_form_pdf(
    form_id: str,
    request: Request,
    body: StudentPrintBody | None = None,
    db: Session = Depends(get_db),
):
    """Real one-page A4 PDF of the student's OWN exam form (Download / Print).

    Identity comes from the authenticated student session — the form id is
    re-scoped to that student server-side. `as_attachment=true` → attachment
    (Download), otherwise inline (preview/Print). Stamps printed_at (server).
    """
    identity = _require_student_snapshot(request, db)
    student_id = identity["student_id"]
    data = efs.student_form_document(db, student_id, form_id)
    if data is None:
        raise HTTPException(status_code=403, detail="You are not authorized to access this form.")
    if not data.get("form_no"):
        raise HTTPException(status_code=404, detail="This exam form has no printed number yet.")
    efs.mark_form_printed(db, student_id, form_id)

    pdf_bytes = efrender.render_exam_form_pdf(data)
    filename = f"Exam_Form_{data.get('form_no') or form_id}.pdf"
    headers = {
        "X-Robots-Tag": "noindex, nofollow",
        "Cache-Control": "no-store",
        "Content-Security-Policy": "default-src 'none'",
        "Content-Disposition": f"{'attachment' if (body and body.as_attachment) else 'inline'}; filename=\"{filename}\"",
    }
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)


@router.get("/{form_id}/receipt")
def view_exam_form_receipt(
    form_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """HTML fee receipt of the student's OWN form (after successful payment).

    Only a server-confirmed successful payment produces a receipt; an unpaid
    form is rejected with 409. Identity comes from the authenticated student
    session and the form id is re-scoped to that student (IDOR-safe, same
    source of truth as the exam-form document).
    """
    identity = _require_student_snapshot(request, db)
    data = efs.student_payment_receipt(db, identity["student_id"], form_id)
    if data is None:
        raise HTTPException(status_code=403, detail="You are not authorized to access this form.")
    if (data.get("fee_status") or "Unpaid") != "Paid":
        raise HTTPException(status_code=409, detail="No successful payment has been recorded for this exam form yet.")
    html = efrender.render_fee_receipt_html(data)
    return Response(
        content=html,
        media_type="text/html; charset=utf-8",
        headers={
            "X-Robots-Tag": "noindex, nofollow",
            "Cache-Control": "no-store",
            "Content-Security-Policy": "default-src 'none'",
        },
    )


@router.post("/{form_id}/receipt")
def download_exam_form_receipt(
    form_id: str,
    request: Request,
    body: StudentPrintBody | None = None,
    db: Session = Depends(get_db),
):
    """One-page A4 PDF fee receipt (Download / Print) for the student's OWN
    successful payment. Same ownership + payment gating as the HTML receipt."""
    identity = _require_student_snapshot(request, db)
    data = efs.student_payment_receipt(db, identity["student_id"], form_id)
    if data is None:
        raise HTTPException(status_code=403, detail="You are not authorized to access this form.")
    if (data.get("fee_status") or "Unpaid") != "Paid":
        raise HTTPException(status_code=409, detail="No successful payment has been recorded for this exam form yet.")

    pdf_bytes = efrender.render_fee_receipt_pdf(data)
    filename = f"Fee_Receipt_{data.get('form_no') or form_id}.pdf"
    headers = {
        "X-Robots-Tag": "noindex, nofollow",
        "Cache-Control": "no-store",
        "Content-Security-Policy": "default-src 'none'",
        "Content-Disposition": f"{'attachment' if (body and body.as_attachment) else 'inline'}; filename=\"{filename}\"",
    }
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)


@router.post("/{form_id}/payments/initiate")
def initiate_exam_payment(
    form_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Open an `initiated` payment for the student's OWN form (amount server-side)."""
    identity = _require_student_snapshot(request, db)
    try:
        form = efs.form_or_404(db, form_id)
        payment = efpay.initiate_payment(db, form, identity["student_id"])
    except ValueError as exc:
        raise _value_error(exc)
    audit(
        db, "student_exam_form.pay_initiate", actor_id=identity.get("actor_id"),
        actor_role="student", target=str(form.id),
        detail=f"Student initiated a payment of ₹{payment['amount']} for exam form {form.form_no or form_id}",
        ip=_ip(request),
    )
    return payment


@router.post("/{form_id}/payments/{payment_id}/confirm")
def confirm_exam_payment(
    form_id: str,
    payment_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Run the payment backend on an initiated payment → success (server-side)."""
    identity = _require_student_snapshot(request, db)
    try:
        payment = efpay.confirm_payment(db, payment_id, identity["student_id"])
    except ValueError as exc:
        raise _value_error(exc)
    audit(
        db, "student_exam_form.pay_confirm", actor_id=identity.get("actor_id"),
        actor_role="student", target=payment["form_id"],
        detail=f"Student payment confirmed: {payment.get('gateway_ref') or payment['id']}",
        ip=_ip(request),
    )
    return payment


@router.get("/{form_id}/payments")
def list_exam_payments(
    form_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Payment history for the student's OWN form (initiated/success/refunded)."""
    identity = _require_student_snapshot(request, db)
    try:
        form = efs.form_or_404(db, form_id)
    except ValueError as exc:
        raise _value_error(exc)
    if str(form.student_id) != identity["student_id"]:
        raise HTTPException(status_code=403, detail="You are not authorized to access this form.")
    return {"payments": efpay.list_form_payments(db, form_id)}


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
