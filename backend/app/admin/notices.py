"""backend/app/admin/notices.py — Super-Admin management of university notices.

Lifecycle endpoints mirror the service contract:
    upload+extract -> schedule review/correct -> verify -> publish
`notice.verify` / `notice.publish` / all mutations are super-admin only.
Listing is available to admins; nothing here is reachable by the public.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.security import require_admin, require_superadmin
from app.database import get_db, utcnow
from app.models import DateSheetEntry, UniversityNotice, User
from app.notices import service as notices
from app.notices.validator import iso_date_ok, time_ok
from app.utils.logging import audit

router = APIRouter(tags=["admin-notices"])

_PREFIX = "/api/admin/notices"

_ENTRY_EDITABLE = frozenset(
    {
        "programme_id", "programme_name", "stream", "semester", "batch",
        "exam_type", "exam_date", "day", "start_time", "end_time",
        "subject_code", "subject", "paper_code", "venue",
    }
)


class NoticeUpdate(BaseModel):
    title: str | None = Field(default=None, max_length=400)
    notice_type: str | None = None
    categories: list[str] | None = None
    programme_ids: list[str] | None = None


class EntryUpdate(BaseModel):
    programme_id: str | None = None
    programme_name: str | None = None
    stream: str | None = None
    semester: str | None = None
    batch: str | None = None
    exam_type: str | None = None
    exam_date: str | None = None
    day: str | None = None
    start_time: str | None = None
    end_time: str | None = None
    subject_code: str | None = None
    subject: str | None = None
    paper_code: str | None = None
    venue: str | None = None


def _get_notice(db: Session, notice_id: str) -> UniversityNotice:
    n = notices.get_notice(db, notice_id)
    if n is None:
        raise HTTPException(status_code=404, detail="Notice not found")
    return n


def _get_entry(db: Session, notice_id: str, entry_id: str) -> DateSheetEntry:
    n = _get_notice(db, notice_id)
    entry = (
        db.query(DateSheetEntry)
        .filter(
            DateSheetEntry.id == notices_entry_uuid(entry_id),
            DateSheetEntry.notice_id == n.id,
            DateSheetEntry.deleted_at.is_(None),
        )
        .first()
    )
    if entry is None:
        raise HTTPException(status_code=404, detail="Schedule entry not found")
    return entry


def notices_entry_uuid(value: str):
    try:
        import uuid

        return uuid.UUID(value)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid schedule entry id")


@router.get(_PREFIX)
def admin_list_notices(
    q: str | None = Query(default=None),
    notice_type: str | None = Query(default=None),
    status: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
    current: User = Depends(require_admin),
):
    items = notices.list_notices(
        db,
        published_only=False,
        q=q,
        notice_type=notice_type,
        limit=200,
        include_deleted=False,
    )
    if status:
        items = [i for i in items if i.extraction_status == status]
    items.sort(key=lambda i: (i.published_at or i.created_at), reverse=True)
    total = len(items)
    start = (page - 1) * page_size
    page_items = items[start : start + page_size]
    return {
        "items": [notices.notice_dto(i, include_entries=False) for i in page_items],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.post(_PREFIX, status_code=201)
async def admin_upload_notice(
    file: UploadFile = File(..., description="PDF/DOCX notice file"),
    title: str | None = Form(default=None),
    notice_type: str | None = Form(default=None),
    categories: str | None = Form(default=None, description="JSON array of tags"),
    programme_ids: str | None = Form(default=None, description="JSON array of programme ids"),
    db: Session = Depends(get_db),
    current: User = Depends(require_superadmin),
):
    data = await file.read()

    def _parse_json_array(raw: str | None, name: str) -> list[str] | None:
        if raw is None or raw.strip() == "":
            return None
        try:
            val = json.loads(raw)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"{name} must be a JSON array") from exc
        if not isinstance(val, list) or not all(isinstance(x, str) for x in val):
            raise HTTPException(status_code=400, detail=f"{name} must be a JSON array of strings")
        return [str(x).strip()[:50] for x in val]

    n = notices.upload_and_extract(
        db,
        created_by=current.id,
        title=title,
        notice_type=notice_type,
        categories=_parse_json_array(categories, "categories"),
        programme_ids=_parse_json_array(programme_ids, "programme_ids"),
        filename=file.filename or "notice.pdf",
        data=data,
    )
    return notices.notice_dto(n)


@router.get(f"{_PREFIX}/{{notice_id}}")
def admin_get_notice(
    notice_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(require_superadmin),
):
    n = _get_notice(db, notice_id)
    return notices.notice_dto(n)


@router.patch(f"{_PREFIX}/{{notice_id}}")
def admin_update_notice(
    notice_id: str,
    body: NoticeUpdate,
    db: Session = Depends(get_db),
    current: User = Depends(require_superadmin),
):
    n = _get_notice(db, notice_id)
    changed = False
    if body.title is not None:
        n.title = body.title.strip()[:400]
        changed = True
    if body.notice_type is not None and body.notice_type.lower() != n.notice_type:
        nt = body.notice_type.lower()
        if nt not in notices.NOTICE_TYPES:
            raise HTTPException(status_code=400, detail="notice_type must be 'notice' or 'date_sheet'")
        n.notice_type = nt
        changed = True
    if body.categories is not None:
        n.categories = notices._to_json(body.categories[:50])
        changed = True
    if body.programme_ids is not None:
        n.programme_ids = notices._to_json(body.programme_ids[:50])
        changed = True
    if changed:
        db.commit()
        audit(db, "notice.update_metadata", actor_id=str(current.id),
              actor_role=current.role, target=str(n.id))
    return notices.notice_dto(n)


@router.delete(f"{_PREFIX}/{{notice_id}}")
def admin_delete_notice(
    notice_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(require_superadmin),
):
    n = _get_notice(db, notice_id)
    notices.soft_delete_notice(db, n, actor_id=str(current.id))
    return {"status": "deleted", "id": notice_id}


@router.post(f"{_PREFIX}/{{notice_id}}/extract")
def admin_re_extract_notice(
    notice_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(require_superadmin),
):
    n = _get_notice(db, notice_id)
    n = notices.re_extract(db, n)
    return notices.notice_dto(n)


# ---------------------------------------------------------------------------
# Schedule rows
# ---------------------------------------------------------------------------

@router.get(f"{_PREFIX}/{{notice_id}}/schedule")
def admin_get_schedule(
    notice_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(require_superadmin),
):
    n = _get_notice(db, notice_id)
    entries = notices.entries_for_notice(db, n)
    return {"entries": [notices._entry_dict(e) for e in entries]}


@router.post(f"{_PREFIX}/{{notice_id}}/schedule", status_code=201)
def admin_add_schedule_entry(
    notice_id: str,
    body: EntryUpdate,
    db: Session = Depends(get_db),
    current: User = Depends(require_superadmin),
):
    n = _get_notice(db, notice_id)
    _validate_entry_fields(body)
    entries = notices.entries_for_notice(db, n)
    row_no = max([e.row_no for e in entries], default=0) + 1
    entry = DateSheetEntry(
        notice_id=n.id,
        row_no=row_no,
        extraction_status="pending_verification",
        is_manual=True,
        raw=None,
        **{f: getattr(body, f) for f in _ENTRY_EDITABLE},
    )
    db.add(entry)
    db.flush()
    if n.notice_type != "date_sheet":
        n.notice_type = "date_sheet"
    db.commit()
    notices.revalidate_after_edit(db, n)
    entry = _get_entry(db, notice_id, str(entry.id))
    audit(db, "notice.schedule.add", actor_id=str(current.id), actor_role=current.role,
          target=f"{notice_id}:{entry.id}")
    return notices._entry_dict(entry)


@router.patch(f"{_PREFIX}/{{notice_id}}/schedule/{{entry_id}}")
def admin_update_schedule_entry(
    notice_id: str,
    entry_id: str,
    body: EntryUpdate,
    db: Session = Depends(get_db),
    current: User = Depends(require_superadmin),
):
    entry = _get_entry(db, notice_id, entry_id)
    changes = body.model_dump(exclude_unset=True)
    if not changes:
        return notices._entry_dict(entry)
    _validate_entry_fields(body)
    n = entry.notice
    for field, value in changes.items():
        if field in _ENTRY_EDITABLE:
            setattr(entry, field, value)
    entry.is_corrected = True
    db.commit()
    notices.revalidate_after_edit(db, n)
    entry = _get_entry(db, notice_id, entry_id)
    audit(db, "notice.schedule.update", actor_id=str(current.id), actor_role=current.role,
          target=f"{notice_id}:{entry.id}")
    return notices._entry_dict(entry)


@router.delete(f"{_PREFIX}/{{notice_id}}/schedule/{{entry_id}}")
def admin_delete_schedule_entry(
    notice_id: str,
    entry_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(require_superadmin),
):
    entry = _get_entry(db, notice_id, entry_id)
    n = entry.notice
    entry.deleted_at = utcnow()
    db.commit()
    notices.revalidate_after_edit(db, n)
    audit(db, "notice.schedule.delete", actor_id=str(current.id), actor_role=current.role,
          target=f"{notice_id}:{entry.id}")
    return {"status": "deleted", "id": entry_id}


def _validate_entry_fields(body: EntryUpdate) -> None:
    if body.exam_date is not None and body.exam_date != "" and not iso_date_ok(body.exam_date):
        raise HTTPException(status_code=422, detail="exam_date must be an ISO date (YYYY-MM-DD)")
    for name in ("start_time", "end_time"):
        val = getattr(body, name)
        if val not in (None, "") and not time_ok(val):
            raise HTTPException(status_code=422, detail=f"{name} must be 24h HH:MM")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

@router.post(f"{_PREFIX}/{{notice_id}}/verify")
def admin_verify_notice(
    notice_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(require_superadmin),
):
    n = _get_notice(db, notice_id)
    n = notices.verify_notice(db, n, actor_id=str(current.id))
    return notices.notice_dto(n)


@router.post(f"{_PREFIX}/{{notice_id}}/publish")
def admin_publish_notice(
    notice_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(require_superadmin),
):
    n = _get_notice(db, notice_id)
    n = notices.publish_notice(db, n, actor_id=str(current.id))
    return notices.notice_dto(n)


@router.post(f"{_PREFIX}/{{notice_id}}/unpublish")
def admin_unpublish_notice(
    notice_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(require_superadmin),
):
    n = _get_notice(db, notice_id)
    n = notices.unpublish_notice(db, n, actor_id=str(current.id))
    return notices.notice_dto(n)


@router.get(f"{_PREFIX}/{{notice_id}}/file")
def admin_get_notice_file(
    notice_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(require_superadmin),
):
    n = _get_notice(db, notice_id)
    p = notices.resolve_stored_file(n)
    media_type = ("application/pdf" if n.file_type == "pdf" else
                  "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    return FileResponse(
        str(p),
        media_type=media_type,
        filename=n.original_filename or n.filename,
    )