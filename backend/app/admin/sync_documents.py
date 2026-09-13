"""
backend/app/admin/sync_documents.py — Super-Admin review of website documents.

Phase 1 "Intelligent Website Document Ingestion" admin surface.

Every binary document crawled by the Website Sync engine is labelled (doc_type
+ category + confidence + signals) and preserved as raw bytes — but NEVER
published anywhere automatically. Model papers in particular are classification-
only and held for review. This module gives admins the review queue:

    GET    /api/admin/sync-documents            list (filterable)
    GET    /api/admin/sync-documents/{id}       detail (incl. versions, raw presence)
    POST   /api/admin/sync-documents/{id}/verify      confirm classification (≠ publish)
    POST   /api/admin/sync-documents/{id}/ambiguous   re-label as ambiguous
    POST   /api/admin/sync-documents/{id}/hide        hold / remove from queue
    POST   /api/admin/sync-documents/{id}/reprocess   re-run classification/metadata

All endpoints are admin-only and write an audit trail. Nothing here publishes
to the University Notices / Date Sheet / public surface — that is a Phase 2
concern and is deliberately out of scope.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.auth.security import require_admin
from app.database import get_db, utcnow
from app.models import User, WebsitePage, WebsitePageVersion
from app.models.website_sync import CrawlRun
from app.utils.logging import audit, log

router = APIRouter(tags=["admin-sync-documents"])

_PREFIX = "/api/admin/sync-documents"

_ALLOWED_CATEGORIES = frozenset(
    {
        "date-sheet",
        "model-paper",
        "official-notification",
        "other-official-document",
        "ambiguous",
        "knowledge",
        "admissions", "examinations", "departments", "programmes", "news",
        "notices", "faculty", "scholarships", "hostels", "transport",
        "administration", "research", "academic-calendar", "events",
        "student-services", "policies", "downloads", "unknown",
    }
)


class VerifyBody(BaseModel):
    category: str | None = Field(default=None, max_length=50)
    review_note: str | None = Field(default=None, max_length=500)


class AmbiguousBody(BaseModel):
    review_note: str | None = Field(default=None, max_length=500)


class HideBody(BaseModel):
    review_note: str | None = Field(default=None, max_length=500)


def _get_page(db: Session, page_id: str) -> WebsitePage:
    page = db.get(WebsitePage, page_id)
    if not page:
        raise HTTPException(status_code=404, detail="Page not found")
    return page


def _actor_name(current: User) -> str:
    return current.username or str(current.id)


def _page_payload(db: Session, page: WebsitePage) -> dict[str, Any]:
    payload = page.to_dict()
    payload["versions"] = [
        v.to_dict()
        for v in (
            db.query(WebsitePageVersion)
            .filter(WebsitePageVersion.page_id == page.id)
            .order_by(WebsitePageVersion.version.desc())
            .all()
        )
    ]
    payload["has_raw"] = bool(page.raw_path and page.raw_sha256)
    # Resolve whether the preserved raw file still exists on disk.
    if payload["has_raw"]:
        from app.knowledge_sync.raw_store import resolve_contained

        payload["has_raw_on_disk"] = resolve_contained(page.raw_path) is not None
    else:
        payload["has_raw_on_disk"] = False
    return payload


@router.get(_PREFIX)
def sync_documents_list(
    db: Session = Depends(get_db),
    current: User = Depends(require_admin),
    doc_type: str | None = Query(default=None),
    classification_status: str | None = Query(default=None),
    category: str | None = Query(default=None),
    confidence: str | None = Query(default=None),
    q: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=500),
    offset: int = Query(default=0, ge=0),
):
    """List website documents with Phase 1 review filters."""
    query = db.query(WebsitePage)
    if doc_type:
        query = query.filter(WebsitePage.doc_type == doc_type)
    if classification_status:
        query = query.filter(WebsitePage.classification_status == classification_status)
    if category:
        query = query.filter(WebsitePage.category == category)
    if confidence:
        query = query.filter(
            WebsitePage.classification_confidence["band"].as_string() == confidence
        )
    if q:
        query = query.filter(WebsitePage.url.contains(q) | WebsitePage.title.contains(q))
    query = query.order_by(WebsitePage.last_synced.desc())
    total = query.count()
    rows = query.limit(limit).offset(offset).all()
    return {
        "items": [page.to_dict() for page in rows],
        "total": total,
        "filters": {
            "doc_type": doc_type,
            "classification_status": classification_status,
            "category": category,
            "confidence": confidence,
            "q": q,
        },
    }


@router.get(f"{_PREFIX}/stats")
def sync_documents_stats(
    db: Session = Depends(get_db),
    current: User = Depends(require_admin),
):
    """Review-queue summary for the admin tab header."""
    rows = db.query(
        WebsitePage.doc_type, WebsitePage.classification_status, WebsitePage.category
    ).all()
    by_type: dict[str, int] = {}
    by_status: dict[str, int] = {}
    # Per-category counts powering the "Sync Documents" category chips. The
    # chips are disjoint filters over the same list endpoint: knowledge and
    # ambiguous documents are keyed by doc_type (their category column carries
    # the legacy knowledge bucket / "ambiguous"), official documents are keyed
    # by their canonical category value.
    by_category: dict[str, int] = {
        "date-sheet": 0,
        "model-paper": 0,
        "official-notification": 0,
        "other-official-document": 0,
        "knowledge": 0,
        "ambiguous": 0,
    }
    for doc_type, status, category in rows:
        if doc_type:
            by_type[doc_type] = by_type.get(doc_type, 0) + 1
        if status:
            by_status[status] = by_status.get(status, 0) + 1
        if doc_type == "knowledge":
            by_category["knowledge"] += 1
        elif doc_type == "ambiguous":
            by_category["ambiguous"] += 1
        elif category in by_category:
            by_category[category] += 1
    last_run = (
        db.query(CrawlRun)
        .order_by(CrawlRun.started_at.desc())
        .first()
    )
    return {
        "by_doc_type": by_type,
        "by_status": by_status,
        "by_category": by_category,
        "pending_review": by_status.get("pending_review", 0),
        "verified": by_status.get("verified", 0),
        "hidden_hold": by_status.get("hidden_hold", 0),
        "total_pages": len(rows),
        "last_run": last_run.to_dict() if last_run else None,
    }


@router.get(f"{_PREFIX}/{{page_id}}")
def sync_documents_detail(
    page_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(require_admin),
):
    """Detail incl. version history and raw-file presence."""
    page = _get_page(db, page_id)
    return _page_payload(db, page)


@router.post(f"{_PREFIX}/{{page_id}}/verify")
def sync_documents_verify(
    page_id: str,
    body: VerifyBody,
    db: Session = Depends(get_db),
    current: User = Depends(require_admin),
):
    """Confirm a document's classification (metadata trust, NOT publication)."""
    page = _get_page(db, page_id)
    # Server-side category allowlist validation.
    if body.category and body.category not in _ALLOWED_CATEGORIES:
        raise HTTPException(status_code=422, detail=f"Unknown category '{body.category}'")

    page.classification_status = "verified"
    page.reviewed_by = _actor_name(current)
    page.reviewed_at = utcnow()
    if body.review_note is not None:
        page.review_note = body.review_note
    if body.category:
        page.category = body.category
    db.commit()
    audit(db, "sync_document_verify", actor_id=str(current.id), actor_role=current.role,
          target=page_id,
          detail=f"doc_type={page.doc_type} category={page.category}")
    db.refresh(page)
    return _page_payload(db, page)


@router.post(f"{_PREFIX}/{{page_id}}/ambiguous")
def sync_documents_ambiguous(
    page_id: str,
    body: AmbiguousBody,
    db: Session = Depends(get_db),
    current: User = Depends(require_admin),
):
    """Re-label a document as ambiguous (needs human judgment)."""
    page = _get_page(db, page_id)
    page.doc_type = "ambiguous"
    page.category = "ambiguous"
    page.classification_status = "pending_review"
    page.reviewed_by = _actor_name(current)
    page.reviewed_at = utcnow()
    if body.review_note is not None:
        page.review_note = body.review_note
    db.commit()
    audit(db, "sync_document_ambiguous", actor_id=str(current.id), actor_role=current.role,
          target=page_id, detail=page.review_note or "")
    db.refresh(page)
    return _page_payload(db, page)


@router.post(f"{_PREFIX}/{{page_id}}/hide")
def sync_documents_hide(
    page_id: str,
    body: HideBody,
    db: Session = Depends(get_db),
    current: User = Depends(require_admin),
):
    """Hold a document out of further processing (does not delete raw bytes)."""
    page = _get_page(db, page_id)
    page.classification_status = "hidden_hold"
    page.reviewed_by = _actor_name(current)
    page.reviewed_at = utcnow()
    if body.review_note is not None:
        page.review_note = body.review_note
    db.commit()
    audit(db, "sync_document_hide", actor_id=str(current.id), actor_role=current.role,
          target=page_id, detail=page.review_note or "")
    db.refresh(page)
    return _page_payload(db, page)


@router.post(f"{_PREFIX}/{{page_id}}/reprocess")
def sync_documents_reprocess(
    page_id: str,
    db: Session = Depends(get_db),
    current: User = Depends(require_admin),
):
    """Re-run Phase 1 classification + metadata using stored content/raw."""
    from app.knowledge_sync.document_classifier import classify_document, normalize_title_hash
    from app.knowledge_sync.raw_store import resolve_contained
    from app.knowledge_sync.web_metadata import extract_meta

    page = _get_page(db, page_id)

    raw: bytes | None = None
    if page.raw_path and page.raw_sha256:
        path = resolve_contained(page.raw_path)
        if path and path.is_file():
            try:
                raw = path.read_bytes()
            except OSError as exc:
                log.warning("reprocess: could not read raw %s: %s", page.raw_path, exc)
                raw = None

    is_doc = (page.content_type or "") == "document"
    ext = ""
    if is_doc:
        ext = page.url.split("?", 1)[0].rsplit(".", 1)[-1].lower() if "." in page.url else ""
    try:
        classification = classify_document(
            title=page.title or "",
            url=page.url,
            text=page.content or "",
            content_type=page.content_type or "html",
            raw=raw,
        )
        meta = extract_meta(
            filename=page.url.split("?", 1)[0].rsplit("/", 1)[-1],
            url=page.url,
            ext=page.content_type or ext,
            raw=raw,
            extracted_text=page.content or "",
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=422, detail=f"Reprocessing failed: {exc}")

    page.doc_type = classification.get("doc_type")
    page.category = classification.get("category") or page.category
    page.classification_confidence = classification.get("confidence")
    page.classification_signals = classification.get("signals")
    page.doc_meta = meta
    page.title_hash = normalize_title_hash(page.normalized_title or None)
    from app.knowledge_sync.document_classifier import classification_state_for

    page.classification_status = classification_state_for(classification, is_document=is_doc)
    if classification.get("category") == "model-paper":
        page.classification_status = "pending_review"
    db.commit()
    audit(db, "sync_document_reprocess", actor_id=str(current.id), actor_role=current.role,
          target=page_id,
          detail=f"doc_type={page.doc_type} category={page.category} "
                 f"conf={classification.get('confidence')}")
    db.refresh(page)
    return _page_payload(db, page)