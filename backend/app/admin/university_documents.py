"""
backend/app/admin/university_documents.py -- Super-Admin management of the
canonical university_documents repository (Phase 3C-7).

This is the single write-path admin panel for the unified canonical table.
Both crawler output (Website Sync) and manual uploads land here; doc_type is
INDEPENDENT of origin (carried by `source` = crawler | manual_upload).

All endpoints are admin-only and write an audit trail. Nothing here is
reachable by the public; the crawler control panel stays in
app/admin/sync_documents.py.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.security import require_admin, require_superadmin
from app.database import get_db, utcnow
from app.models import UniversityDocument, User
from app.university_documents import service as university_documents
from app.utils.logging import audit

router = APIRouter(tags=["admin-university-documents"])

_PREFIX = "/api/admin/university-documents"


# ---------------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------------
class ManualUploadIn(BaseModel):
    title: str = Field(..., min_length=1, max_length=500)
    doc_type: str = Field(..., description="date_sheet | model_paper | official_notification | other_official_document | knowledge | needs_review")
    file_path: str | None = Field(default=None, max_length=1000)
    original_filename: str | None = Field(default=None, max_length=500)
    file_type: str | None = Field(default=None, max_length=120)
    file_size: int | None = Field(default=None, ge=0)


class ReclassifyIn(BaseModel):
    doc_type: str = Field(..., description="Target doc_type for reclassification.")


class ReviewNoteIn(BaseModel):
    note: str | None = Field(default=None, max_length=500)


# ---------------------------------------------------------------------------
# Listing + detail (admin-only)
# ---------------------------------------------------------------------------
@router.get(_PREFIX)
def university_documents_list(
    db: Session = Depends(get_db),
    current: User = Depends(require_admin),
    doc_type: str | None = Query(default=None),
    source: str | None = Query(default=None),
    status: str | None = Query(default=None),
    q: str | None = Query(default=None),
    programme_id: str | None = Query(default=None),
    include_deleted: bool = Query(default=False),
    limit: int = Query(default=50, ge=1, le=200),
    offset: int = Query(default=0, ge=0),
):
    items = university_documents.list_documents(
        db,
        doc_type=doc_type,
        source=source,
        status=status,
        q=q,
        programme_id=programme_id,
        limit=limit,
        offset=offset,
        include_deleted=include_deleted,
    )
    total = university_documents.count_documents(
        db,
        doc_type=doc_type,
        source=source,
        status=status,
        q=q,
        programme_id=programme_id,
        include_deleted=include_deleted,
    )
    return {"items": [university_documents.document_dto(d) for d in items], "total": total}


@router.get(f"{_PREFIX}/{{document_id}}")
def university_documents_detail(
    document_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(require_admin),
):
    d = university_documents.get_document(db, document_id)
    if d is None:
        raise HTTPException(status_code=404, detail="University document not found.")
    return university_documents.document_dto(d)


# ---------------------------------------------------------------------------
# Manual upload -> canonical repo (super-admin only)
# ---------------------------------------------------------------------------
@router.post(_PREFIX, status_code=201)
def university_documents_manual_upload(
    payload: ManualUploadIn,
    db: Session = Depends(get_db),
    current: User = Depends(require_superadmin),
):
    try:
        d = university_documents.record_manual_upload(
            db,
            title=payload.title,
            doc_type=payload.doc_type,
            file_path=payload.file_path,
            original_filename=payload.original_filename,
            file_type=payload.file_type,
            file_size=payload.file_size,
            actor_id=str(current.id),
            actor_role="superadmin",
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return university_documents.document_dto(d)


# ---------------------------------------------------------------------------
# Lifecycle (super-admin only, audited)
# ---------------------------------------------------------------------------
@router.post(f"{_PREFIX}/{{document_id}}/verify")
def university_documents_verify(
    document_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(require_superadmin),
):
    d = university_documents._get(db, document_id)
    university_documents.verify_document(
        db, d, actor_id=str(current.id), actor_role="superadmin"
    )
    return university_documents.document_dto(d)


@router.post(f"{_PREFIX}/{{document_id}}/publish")
def university_documents_publish(
    document_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(require_superadmin),
):
    d = university_documents._get(db, document_id)
    try:
        university_documents.publish_document(
            db, d, actor_id=str(current.id), actor_role="superadmin"
        )
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    return university_documents.document_dto(d)


@router.post(f"{_PREFIX}/{{document_id}}/unpublish")
def university_documents_unpublish(
    document_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(require_superadmin),
):
    d = university_documents._get(db, document_id)
    university_documents.unpublish_document(
        db, d, actor_id=str(current.id), actor_role="superadmin"
    )
    return university_documents.document_dto(d)


@router.post(f"{_PREFIX}/{{document_id}}/hide")
def university_documents_hide(
    document_id: str,
    payload: ReviewNoteIn | None = None,
    db: Session = Depends(get_db),
    current: User = Depends(require_superadmin),
):
    d = university_documents._get(db, document_id)
    university_documents.hide_document(
        db, d,
        note=(payload.note if payload else None),
        actor_id=str(current.id),
        actor_role="superadmin",
    )
    return university_documents.document_dto(d)


@router.post(f"{_PREFIX}/{{document_id}}/restore")
def university_documents_restore(
    document_id: str,
    payload: ReviewNoteIn | None = None,
    db: Session = Depends(get_db),
    current: User = Depends(require_superadmin),
):
    d = university_documents._get(db, document_id)
    university_documents.restore_document(
        db, d,
        note=(payload.note if payload else None),
        actor_id=str(current.id),
        actor_role="superadmin",
    )
    return university_documents.document_dto(d)


@router.delete(f"{_PREFIX}/{{document_id}}")
def university_documents_soft_delete(
    document_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(require_superadmin),
):
    d = university_documents._get(db, document_id)
    university_documents.soft_delete_document(
        db, d, actor_id=str(current.id), actor_role="superadmin"
    )
    return {"ok": True, "id": str(d.id), "status": "deleted"}


@router.post(f"{_PREFIX}/{{document_id}}/reclassify")
def university_documents_reclassify(
    document_id: str,
    payload: ReclassifyIn,
    db: Session = Depends(get_db),
    current: User = Depends(require_superadmin),
):
    d = university_documents._get(db, document_id)
    try:
        university_documents.reclassify_document(
            db, d, payload.doc_type,
            actor_id=str(current.id),
            actor_role="superadmin",
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return university_documents.document_dto(d)


# ---------------------------------------------------------------------------
# Idempotent backfill from legacy UniversityNotice rows (super-admin only)
# ---------------------------------------------------------------------------
@router.post(f"{_PREFIX}/backfill")
def university_documents_backfill(
    db: Session = Depends(get_db),
    current: User = Depends(require_superadmin),
):
    result = university_documents.backfill_from_notices(
        db, actor_id=str(current.id)
    )
    return result
