"""
backend/app/student_admit_card/routes.py

Student Admit Card endpoints (Phase C).

  Student (session cookie, server-validated):
    GET /api/student/admit-cards                available cards (own)
    GET /api/student/admit-cards?semester=N     one semester's admit card

  Super Admin (require_superadmin on every endpoint):
    GET    /api/admin/admit-cards               list / search / filter / paginate
    POST   /api/admin/admit-cards               create a single card
    PATCH  /api/admin/admit-cards/{id}          edit a card
    DELETE /api/admin/admit-cards/{id}          withdraw a card (delete + audit)
    POST   /api/admin/admit-cards/preview       upload CSV/XLSX → validate (writes nothing)
    POST   /api/admin/admit-cards/confirm       apply validated rows in ONE transaction

Security contract:
  - The student identity is ALWAYS derived from the resolved StudentSession
    cookie. No `student_id` / `reg_no` / `roll_no` parameter is accepted, so
    cross-student access is structurally impossible (IDOR-safe).
  - Semester selection is restricted to an explicit allowlist.
  - Cards are STRUCTURED records (centre, session, subjects, instructions).
    There is no file/PDF upload: the assigned record is the canonical card.
  - Import preview writes nothing; confirm re-validates every row server-side
    and commits atomically (rollback on any failure). Audit records only the
    filename + count — never DOB, hashes, tokens or other student PII.
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
from app.student_admit_card import service as admit_card_svc
from app.student_admit_card.render import render_admit_card_pdf
from app.student_admit_card.schemas import AdmitCardCreate, AdmitCardPrintRequest, AdmitCardUpdate, ImportBundle
from app.utils.logging import audit

router = APIRouter(prefix=f"{settings.API_PREFIX}/student/admit-cards", tags=["student-admit-card"])
admin_router = APIRouter(prefix=f"{settings.API_PREFIX}/admin/admit-cards", tags=["student-admit-card-admin"])

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
def my_admit_cards(
    request: Request,
    db: Session = Depends(get_db),
    semester: int | None = Query(None, ge=1, description="Semester to fetch (allowlist)"),
):
    identity = _require_student_snapshot(request, db)
    student_id = identity["student_id"]
    available = admit_card_svc.student_card_semesters(db, student_id)

    if semester is None:
        return {"semesters": available}

    if semester not in settings.valid_student_semesters:
        raise HTTPException(status_code=422, detail="Invalid semester selection.")

    data = admit_card_svc.student_card_payload(db, student_id, semester)
    if data is None:
        return {
            "semester": semester,
            "available_semesters": [s["semester"] for s in available],
            "card": None,
            "message": f"No admit card is issued for Semester {semester} yet.",
        }
    return {
        "semester": semester,
        "available_semesters": [s["semester"] for s in available],
        "card": data,
    }


@router.post("/{semester}/print")
def print_admit_card(
    semester: int,
    request: Request,
    body: AdmitCardPrintRequest | None = None,
    db: Session = Depends(get_db),
):
    """Real one-page A4 PDF of the authenticated student's OWN admit card.

    Identity always comes from the authenticated StudentSession cookie —
    the semester is the only selector and never carries a student identity.
    `as_attachment=true` → Content-Disposition attachment (Download),
    otherwise inline (Print / preview).
    """
    identity = _require_student_snapshot(request, db)
    student_id = identity["student_id"]

    if semester not in settings.valid_student_semesters:
        raise HTTPException(status_code=422, detail="Invalid semester selection.")

    data = admit_card_svc.student_card_document(db, student_id, semester)
    if data is None:
        raise HTTPException(
            status_code=404,
            detail="No admit card is issued for the selected semester.",
        )

    pdf_bytes = render_admit_card_pdf(data)
    headers = {
        "X-Robots-Tag": "noindex, nofollow",
        "Cache-Control": "no-store",
        "Content-Security-Policy": "default-src 'none'",
    }
    filename = f"Admit_Card_Semester_{semester}.pdf"
    disposition = "attachment" if (body and body.as_attachment) else "inline"
    headers["Content-Disposition"] = f'{disposition}; filename="{filename}"'
    return Response(content=pdf_bytes, media_type="application/pdf", headers=headers)


# --------------------------------------------------------------------------- #
# Super-Admin: management list + create / update / delete + import
# --------------------------------------------------------------------------- #
@admin_router.get("")
def list_admit_cards(
    db: Session = Depends(get_db),
    current: User = _superadmin,
    q: str | None = Query(None, max_length=200, description="Search reg no or student name"),
    semester: int | None = Query(None, ge=1),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    try:
        return admit_card_svc.list_admit_cards(db, q=q, semester=semester, page=page, page_size=page_size)
    except ValueError as exc:
        raise _value_error(exc)


@admin_router.post("", status_code=201)
def create_admit_card(
    body: AdmitCardCreate,
    request: Request,
    db: Session = Depends(get_db),
    current: User = _superadmin,
):
    try:
        card = admit_card_svc.create_card(db, body)
    except ValueError as exc:
        raise _value_error(exc)
    audit(
        db, "student_admit_card.create", actor_id=str(current.id), actor_role=current.role,
        target=card["reg_no"],
        detail=f"Created admit card for {card['reg_no']} (semester {card['semester']})",
        ip=_ip(request),
    )
    return card


@admin_router.post("/preview")
def preview_import(
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
    current: User = _superadmin,
):
    content = file.file.read()
    return admit_card_svc.preview_import_file(db, file.filename or "", content)


@admin_router.post("/confirm")
def confirm_import(
    body: ImportBundle,
    request: Request,
    db: Session = Depends(get_db),
    current: User = _superadmin,
):
    try:
        count = admit_card_svc.apply_import(db, body.rows)
    except admit_card_svc.ImportDataError as exc:
        raise HTTPException(
            status_code=409 if exc.duplicate_only else 422,
            detail=exc.summary,
        )
    audit(
        db, "student_admit_card.import", actor_id=str(current.id), actor_role=current.role,
        target=body.filename or "admit card import",
        detail=f"Imported {count} admit card entries from '{body.filename or 'upload'}'. Rows: {count}.",
        ip=_ip(request),
    )
    return {"imported": count, "message": f"Imported {count} admit card entries."}


@admin_router.patch("/{card_id}")
def update_admit_card(
    card_id: str,
    body: AdmitCardUpdate,
    request: Request,
    db: Session = Depends(get_db),
    current: User = _superadmin,
):
    try:
        card = admit_card_svc.update_card(db, card_id, body)
    except ValueError as exc:
        raise _value_error(exc)
    audit(
        db, "student_admit_card.update", actor_id=str(current.id), actor_role=current.role,
        target=card["reg_no"],
        detail=f"Updated admit card for {card['reg_no']} (semester {card['semester']})",
        ip=_ip(request),
    )
    return card


@admin_router.delete("/{card_id}")
def delete_admit_card(
    card_id: str,
    request: Request,
    db: Session = Depends(get_db),
    current: User = _superadmin,
):
    try:
        card = admit_card_svc.delete_card(db, card_id)
    except ValueError as exc:
        raise _value_error(exc)
    audit(
        db, "student_admit_card.delete", actor_id=str(current.id), actor_role=current.role,
        target=card["reg_no"],
        detail=f"Withdrew admit card for {card['reg_no']} (semester {card['semester']})",
        ip=_ip(request),
    )
    return {"deleted": True, "reg_no": card["reg_no"], "semester": card["semester"]}