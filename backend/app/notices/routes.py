"""backend/app/notices/routes.py — public university-notice endpoints.

Every read is structurally gated to VERIFIED + PUBLISHED, non-deleted
notices (and VERIFIED, non-deleted schedule rows). The file endpoint refuses
to serve unpublished documents and validates path containment server-side.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import UniversityNotice
from app.notices import service as notices

router = APIRouter(tags=["notices"])

_PREFIX = "/api/notices"


@router.get(_PREFIX)
def public_list_notices(
    q: str | None = Query(default=None),
    programme: str | None = Query(default=None),
    notice_type: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    items = notices.list_notices(
        db,
        published_only=True,
        q=q,
        programme=programme,
        notice_type=notice_type,
        limit=settings.NOTICES_SEARCH_TOP_N,
    )
    return {"notices": [notices.notice_dto(i, include_entries=False) for i in items]}


@router.get(f"{_PREFIX}/{{notice_id}}")
def public_get_notice(
    notice_id: str,
    db: Session = Depends(get_db),
):
    n: UniversityNotice | None = notices.get_notice(db, notice_id, published_only=True)
    if n is None:
        raise HTTPException(status_code=404, detail="Notice not found")
    return notices.notice_dto(n, include_entries=True)


@router.get(f"{_PREFIX}/{{notice_id}}/schedule")
def public_get_schedule(
    notice_id: str,
    programme: str | None = Query(default=None),
    semester: str | None = Query(default=None),
    stream: str | None = Query(default=None),
    batch: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    n: UniversityNotice | None = notices.get_notice(db, notice_id, published_only=True)
    if n is None:
        raise HTTPException(status_code=404, detail="Notice not found")
    entries = notices.get_verified_schedule(
        db,
        [n.id],
        programme=programme,
        semester=semester,
        stream=stream,
        batch=batch,
    )
    return {
        "notice": notices.notice_dto(n, include_entries=False),
        "schedule": [notices._entry_dict(e) for e in entries],
    }


@router.get(f"{_PREFIX}/{{notice_id}}/file")
def public_get_notice_file(
    notice_id: str,
    download: bool | None = Query(default=False),
    db: Session = Depends(get_db),
):
    n: UniversityNotice | None = notices.get_notice(db, notice_id, published_only=True)
    if n is None:
        raise HTTPException(status_code=404, detail="Notice not available")
    p: Path = notices.resolve_notice_file(n)
    media_type = ("application/pdf" if n.file_type == "pdf" else
                  "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    return FileResponse(
        str(p),
        media_type=media_type,
        filename=n.original_filename or n.filename,
        content_disposition_type="attachment" if download else "inline",
    )