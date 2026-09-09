"""
backend/app/student_results/routes.py

Student Results endpoints (Phase B).

  Student (session cookie, server-validated):
    GET  /api/student/results               available semesters (own results)
    POST /api/student/results/view          one published attempt (semester + examination roll)
    POST /api/student/results/view/print    server-rendered HTML for print / save-as-PDF

  Super Admin (require_superadmin on every endpoint):
    GET  /api/admin/results                 list / search / filter / paginate
    POST /api/admin/results/preview         upload CSV/XLSX → validate (writes nothing)
    POST /api/admin/results/confirm         apply validated rows in ONE transaction
    DELETE /api/admin/results/{id}          delete exactly ONE result row (never bulk)

Security contract:
  - The student identity is ALWAYS derived from the resolved StudentSession
    cookie. No `student_id` / `reg_no` / `roll_no` parameter is accepted, so
    cross-student access is structurally impossible (IDOR-safe).
  - The examination roll number is an INPUT that selects one of the student's
    OWN published attempts — never an authorization credential. It is sent in
    the POST body, never in a URL, and rejected unless it matches the safe
    charset `[A-Za-z0-9-]`.
  - Semester selection is restricted to an explicit allowlist.
  - Import preview writes nothing; confirm re-validates every row server-side
    and commits atomically (rollback on any failure). Audit records only the
    filename + outcome — never DOB, hashes, tokens or mark values.
"""

from __future__ import annotations

import re

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from app.auth.security import require_superadmin
from app.config import settings
from app.database import get_db
from app.models import User
from app.student.session import resolve_session
from app.student_results import service as results_svc
from app.student_results.schemas import ImportBundle, ResultsLookup
from app.utils.logging import audit

router = APIRouter(prefix=f"{settings.API_PREFIX}/student/results", tags=["student-results"])
admin_router = APIRouter(prefix=f"{settings.API_PREFIX}/admin/results", tags=["student-results-admin"])

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
    if "already" in msg or "duplicate" in msg.lower():
        return HTTPException(status_code=409, detail=msg)
    return HTTPException(status_code=422, detail=msg)


# --------------------------------------------------------------------------- #
# Student-facing
# --------------------------------------------------------------------------- #
@router.get("")
def my_semesters(
    request: Request,
    db: Session = Depends(get_db),
    semester: int | None = Query(None, ge=1, description="(rejected) semester content lookup"),
):
    identity = _require_student_snapshot(request, db)
    student_id = identity["student_id"]

    if semester is not None:
        # Since examination rolls were introduced there is no "whole semester
        # without a roll number" view — the per-attempt lookup is POST only, so
        # an examination roll number never appears in a URL or access log.
        raise HTTPException(
            status_code=422,
            detail="Semester results are now per-attempt. Use POST /api/student/results/view.",
        )

    return {"semesters": results_svc.student_semesters(db, student_id, identity.get("semester"))}


@router.post("/view")
def view_result(
    body: ResultsLookup,
    request: Request,
    db: Session = Depends(get_db),
):
    identity = _require_student_snapshot(request, db)
    student_id = identity["student_id"]

    semester, exam_roll_no = body.semester, (body.exam_roll_no or "").strip()
    if body.semester not in settings.valid_student_semesters:
        raise HTTPException(status_code=422, detail="Invalid semester selection.")
    if (
        not exam_roll_no
        or len(exam_roll_no) > 50
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{1,49}", exam_roll_no)
    ):
        raise HTTPException(status_code=422, detail="Invalid examination roll number.")

    result = results_svc.student_result_view(
        db, student_id, semester, exam_roll_no, current_semester=identity.get("semester")
    )
    if result is None:
        return {
            "found": False,
            "message": "No result was found for the selected semester and examination roll number.",
            "semester": semester,
        }
    return {"found": True, "result": result}


@router.post("/view/print", response_class=HTMLResponse)
def print_result(
    body: ResultsLookup,
    request: Request,
    db: Session = Depends(get_db),
):
    identity = _require_student_snapshot(request, db)
    student_id = identity["student_id"]

    semester, exam_roll_no = body.semester, (body.exam_roll_no or "").strip()
    if body.semester not in settings.valid_student_semesters:
        raise HTTPException(status_code=422, detail="Invalid semester selection.")
    if (
        not exam_roll_no
        or len(exam_roll_no) > 50
        or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]{1,49}", exam_roll_no)
    ):
        raise HTTPException(status_code=422, detail="Invalid examination roll number.")

    result = results_svc.student_result_view(
        db, student_id, semester, exam_roll_no, current_semester=identity.get("semester")
    )
    if result is None:
        raise HTTPException(
            status_code=404,
            detail="No result was found for the selected semester and examination roll number.",
        )
    document = results_svc.render_result_print_html(result)
    headers = {
        "X-Robots-Tag": "noindex, nofollow",
        "Cache-Control": "no-store",
        "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'",
    }
    if body.as_attachment:
        filename = f"Result_Semester_{semester}.html"
        headers["Content-Disposition"] = f"attachment; filename=\"{filename}\""
    return HTMLResponse(content=document, headers=headers)


# --------------------------------------------------------------------------- #
# Super-Admin: management list + import
# --------------------------------------------------------------------------- #
@admin_router.get("")
def list_results(
    db: Session = Depends(get_db),
    current: User = _superadmin,
    q: str | None = Query(None, max_length=200, description="Search reg no or student name"),
    semester: int | None = Query(None, ge=1),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    try:
        return results_svc.list_results(db, q=q, semester=semester, page=page, page_size=page_size)
    except ValueError as exc:
        raise _value_error(exc)


@admin_router.post("/preview")
def preview_import(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current: User = _superadmin,
):
    content = file.file.read()
    return results_svc.preview_import_file(db, file.filename or "", content)


@admin_router.post("/confirm")
def confirm_import(
    body: ImportBundle,
    request: Request,
    db: Session = Depends(get_db),
    current: User = _superadmin,
):
    try:
        count = results_svc.apply_import(db, body.rows)
    except results_svc.ImportDataError as exc:
        raise HTTPException(
            status_code=409 if exc.duplicate_only else 422,
            detail=exc.summary,
        )
    audit(
        db, "student_result.import", actor_id=str(current.id), actor_role=current.role,
        target=body.filename or "results import",
        detail=f"Imported {count} result entries from '{body.filename or 'upload'}'. Rows: {count}.",
        ip=_ip(request),
    )
    return {"imported": count, "message": f"Imported {count} result entries."}


@admin_router.delete("/{result_id}")
def delete_result(
    result_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current: User = _superadmin,
):
    try:
        row = results_svc.delete_result(db, result_id)
    except ValueError as exc:
        raise _value_error(exc)
    audit(
        db, "student_result.delete", actor_id=str(current.id), actor_role=current.role,
        target=row["id"],
        detail=(
            f"Deleted result {row['id']} "
            f"(student_id={row.get('student_id') or 'N/A'}, reg_no={row.get('reg_no') or 'N/A'}) "
            "outcome=success"
        ),
        ip=_ip(request),
    )
    return {"deleted": True, "result_id": row["id"]}