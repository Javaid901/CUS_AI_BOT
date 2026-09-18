"""
backend/app/university_documents/service.py

Canonical repository service for Phase 3C-7 (unified university document
management).

Every university document — whether crawled or manually uploaded — is stored
in ONE table (``university_documents``). ``doc_type`` carries the TYPE of the
document and is INDEPENDENT of its origin, which is carried by ``source``:

    source = crawler        doc_type = date_sheet          crawled date sheet
    source = manual_upload  doc_type = date_sheet          uploaded date sheet
    source = crawler        doc_type = official_notification  crawled notice

This service is the ONLY place rows in that table are written / transitioned.
Write paths (Website Sync output + manual uploads) adapt INTO this service;
student-facing consumers keep reading through their existing services.
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.database import utcnow
from app.models.university_document import SOURCES, DOC_TYPES, UniversityDocument
from app.utils.logging import audit

# status values (lifecycle on the canonical row).
_VALID_STATUSES = frozenset(
    {
        "draft",
        "pending_review",
        "needs_review",
        "verified",
        "hidden_hold",
        "published",
    }
)

# Student-facing official-document families. Only these doc_types may be
# exposed on the public read path; the lifecycle gate (verified + published +
# not deleted) is enforced by the queries below, never by the caller.
PUBLIC_OFFICIAL_DOC_TYPES = ("official_notification", "other_official_document")


# ---------------------------------------------------------------------------
# DTO
# ---------------------------------------------------------------------------

def document_dto(d: UniversityDocument, *, include_file: bool = True) -> dict:
    dto = d.to_dict()
    if include_file:
        dto["file"] = {
            "file_path": d.file_path,
            "original_filename": d.original_filename,
            "file_type": d.file_type,
            "file_size": d.file_size,
            "sha256": d.sha256,
        }
    return dto


# ---------------------------------------------------------------------------
# Persistence helpers
# ---------------------------------------------------------------------------

def _validate_doc_type(doc_type: str) -> str:
    doc_type = (doc_type or "").strip().lower()
    if doc_type not in DOC_TYPES:
        raise HTTPException(status_code=422, detail=f"doc_type must be one of: {', '.join(sorted(DOC_TYPES))}")
    return doc_type


def _validate_source(source: str) -> str:
    source = (source or "").strip().lower()
    if source not in SOURCES:
        raise HTTPException(status_code=422, detail=f"source must be one of: {', '.join(sorted(SOURCES))}")
    return source


def _validate_status(status: str) -> str:
    status = (status or "").strip().lower()
    if status not in _VALID_STATUSES:
        raise HTTPException(status_code=422, detail="status must be one of: draft, pending_review, needs_review, verified, hidden_hold, published")
    return status


def _sha256_of_file(path: str | Path) -> str | None:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except (OSError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Query / list
# ---------------------------------------------------------------------------

def _documents_query(
    db: Session,
    *,
    doc_type: str | None = None,
    source: str | None = None,
    status: str | None = None,
    q: str | None = None,
    programme_id: str | None = None,
    include_deleted: bool = False,
):
    qry = db.query(UniversityDocument)
    if not include_deleted:
        qry = qry.filter(UniversityDocument.deleted_at.is_(None))
    if doc_type:
        qry = qry.filter(UniversityDocument.doc_type == _validate_doc_type(doc_type))
    if source:
        qry = qry.filter(UniversityDocument.source == _validate_source(source))
    if status:
        qry = qry.filter(UniversityDocument.status == _validate_status(status))
    if programme_id:
        qry = qry.filter(UniversityDocument.programme_id == programme_id)
    if q:
        like = f"%{q.strip()}%"
        qry = qry.filter(
            or_(
                UniversityDocument.title.like(like),
                UniversityDocument.original_filename.like(like),
            )
        )
    return qry


def list_documents(
    db: Session,
    *,
    doc_type: str | None = None,
    source: str | None = None,
    status: str | None = None,
    q: str | None = None,
    programme_id: str | None = None,
    limit: int = 50,
    offset: int = 0,
    include_deleted: bool = False,
) -> list[UniversityDocument]:
    qry = _documents_query(
        db,
        doc_type=doc_type,
        source=source,
        status=status,
        q=q,
        programme_id=programme_id,
        include_deleted=include_deleted,
    )
    return (
        qry.order_by(UniversityDocument.updated_at.desc())
        .offset(max(0, offset))
        .limit(min(limit, 200))
        .all()
    )


def count_documents(
    db: Session,
    *,
    doc_type: str | None = None,
    source: str | None = None,
    status: str | None = None,
    q: str | None = None,
    programme_id: str | None = None,
    include_deleted: bool = False,
) -> int:
    return _documents_query(
        db,
        doc_type=doc_type,
        source=source,
        status=status,
        q=q,
        programme_id=programme_id,
        include_deleted=include_deleted,
    ).count()


def get_document(db: Session, document_id: str, *, include_deleted: bool = False) -> UniversityDocument | None:
    try:
        uid = uuid.UUID(document_id)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid document id")
    qry = db.query(UniversityDocument).filter(UniversityDocument.id == uid)
    if not include_deleted:
        qry = qry.filter(UniversityDocument.deleted_at.is_(None))
    return qry.first()


def _get(db: Session, document_id: str) -> UniversityDocument:
    d = get_document(db, document_id)
    if d is None:
        raise HTTPException(status_code=404, detail="University document not found")
    return d


# ---------------------------------------------------------------------------
# Public / student read path (official notifications + other official docs)
# ---------------------------------------------------------------------------

def document_public_dto(d: UniversityDocument) -> dict:
    """Student-safe DTO — the stored filesystem path is NEVER exposed."""
    return {
        "id": str(d.id),
        "title": d.title,
        "doc_type": d.doc_type,
        "programme_id": d.programme_id,
        "programme_name": d.programme_name,
        "published_at": d.published_at.isoformat() if d.published_at else None,
        "file_url": public_document_file_url(d.id),
    }


def public_document_file_url(document_id: str) -> str:
    return f"/api/university-documents/{document_id}/file"


def _matches_programme(d: UniversityDocument, programme: str) -> bool:
    """Soft programme match.

    A row that carries NO programme metadata is an institutional (programme-
    agnostic) document and is never excluded by a programme constraint;
    a row that carries metadata must actually match it.
    """
    pid = (d.programme_id or "").strip().lower()
    pname = (d.programme_name or "").strip().lower()
    title = (d.title or "").strip().lower()
    if not pid and not pname:
        return True
    return pid == programme or programme in pname or programme in title


def list_published_documents(
    db: Session,
    *,
    doc_types: tuple[str, ...] | list[str] | None = None,
    q: str | None = None,
    programme: str | None = None,
    limit: int = 50,
) -> list[UniversityDocument]:
    """Verified + published + non-deleted official documents, newest first.

    `doc_types` defaults to the two student-facing official families. `q` is an
    optional case-insensitive title/filename contains-filter. `programme` is a
    soft filter (see ``_matches_programme``).
    """
    types = [_validate_doc_type(t) for t in (doc_types or PUBLIC_OFFICIAL_DOC_TYPES)]
    qry = db.query(UniversityDocument).filter(
        UniversityDocument.deleted_at.is_(None),
        UniversityDocument.is_verified.is_(True),
        UniversityDocument.is_published.is_(True),
        UniversityDocument.doc_type.in_(types),
    )
    if q and q.strip():
        like = f"%{q.strip()}%"
        qry = qry.filter(
            or_(
                UniversityDocument.title.ilike(like),
                UniversityDocument.original_filename.ilike(like),
            )
        )
    rows = (
        qry.order_by(
            UniversityDocument.published_at.is_(None),
            UniversityDocument.published_at.desc(),
            UniversityDocument.updated_at.desc(),
        )
        .limit(200)
        .all()
    )
    if programme:
        prog = str(programme).strip().lower()
        rows = [d for d in rows if _matches_programme(d, prog)]
    return rows[: max(1, min(int(limit or 50), 200))]


def get_published_document(
    db: Session,
    document_id: str,
    *,
    doc_types: tuple[str, ...] | list[str] | None = None,
) -> UniversityDocument | None:
    """A single verified + published + non-deleted official document, or None."""
    try:
        uid = uuid.UUID(document_id)
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(status_code=400, detail="Invalid document id")
    types = [_validate_doc_type(t) for t in (doc_types or PUBLIC_OFFICIAL_DOC_TYPES)]
    return (
        db.query(UniversityDocument)
        .filter(
            UniversityDocument.id == uid,
            UniversityDocument.deleted_at.is_(None),
            UniversityDocument.is_verified.is_(True),
            UniversityDocument.is_published.is_(True),
            UniversityDocument.doc_type.in_(types),
        )
        .first()
    )


def _document_storage_roots() -> tuple[Path, ...]:
    """Containment roots for stored document bytes (never the public mount)."""
    from app.knowledge_sync.raw_store import raw_root
    from app.notices.service import notices_root

    roots: list[Path] = []
    for root in (raw_root(), notices_root()):
        try:
            roots.append(root.resolve())
        except (OSError, ValueError):
            continue
    return tuple(roots)


def _resolve_contained(value: str | None) -> Path | None:
    """Resolve a stored path, refusing any escape from the known roots."""
    value = (value or "").strip()
    if not value:
        return None
    candidate = Path(value)
    if candidate.is_absolute():
        resolved = candidate.resolve()
        for root in _document_storage_roots():
            try:
                resolved.relative_to(root)
                return resolved
            except ValueError:
                continue
        return None
    try:
        from app.knowledge_sync.raw_store import resolve_contained

        p = resolve_contained(value)
        if p is not None:
            return p
    except Exception:
        pass
    for root in _document_storage_roots():
        try:
            p = (root / value).resolve()
            p.relative_to(root)
            return p
        except (OSError, ValueError):
            continue
    return None


def resolve_document_file(d: UniversityDocument) -> Path:
    """Return the on-disk file for a published official document.

    The stored path is never trusted without a containment check. Unpublished,
    unverified, deleted, pathless or escaping rows all raise 404.
    """
    if d.deleted_at is not None or not d.is_published or not d.is_verified:
        raise HTTPException(status_code=404, detail="Document not available.")
    resolved = _resolve_contained(d.file_path)
    if resolved is None or not resolved.is_file():
        raise HTTPException(status_code=404, detail="Document file not available.")
    return resolved


# ---------------------------------------------------------------------------
# Create (single canonical write path)
# ---------------------------------------------------------------------------

def record_crawled_document(
    db: Session,
    *,
    title: str,
    doc_type: str,
    file_path: str | None = None,
    original_filename: str | None = None,
    file_type: str | None = None,
    file_size: int | None = None,
    provider: str = "system",
    sha256: str | None = None,
    source_url: str | None = None,
    site_page_id: str | None = None,
    confidence: dict | None = None,
    provenance: dict | None = None,
) -> UniversityDocument:
    """Register a crawler-produced document in the canonical repository.

    Idempotent at the storage layer: if a non-deleted row already exists with
    the same sha256 fingerprint, it is returned instead of duplicated.

    ``sha256`` may be supplied directly (the crawler already fingerprints raw
    bytes); otherwise it is computed from ``file_path`` on disk.
    """
    doc_type = _validate_doc_type(doc_type)
    if not sha256 and file_path:
        sha256 = _sha256_of_file(file_path)
    if sha256:
        dup = (
            db.query(UniversityDocument)
            .filter(
                UniversityDocument.sha256 == sha256,
                UniversityDocument.deleted_at.is_(None),
            )
            .first()
        )
        if dup:
            return dup
    d = UniversityDocument(
        id=uuid.uuid4(),
        title=(title or "Untitled university document").strip()[:500],
        doc_type=doc_type,
        source="crawler",
        file_path=file_path,
        original_filename=original_filename,
        file_type=file_type,
        file_size=file_size,
        sha256=sha256,
        status="pending_review",
        is_verified=False,
        is_published=False,
        source_url=source_url,
        site_page_id=site_page_id,
        confidence=confidence,
        provenance=provenance,
    )
    db.add(d)
    db.commit()
    audit(
        db, "document.record_crawled",
        actor_id=provider if provider and provider != "system" else None,
        target=str(d.id), detail=f"{doc_type}: {d.title}",
    )
    return d


def record_manual_upload(
    db: Session,
    *,
    title: str,
    doc_type: str,
    file_path: str | None,
    original_filename: str | None,
    file_type: str | None,
    file_size: int | None,
    actor_id: str | None = None,
    actor_role: str | None = "admin",
) -> UniversityDocument:
    """Register a manually uploaded university document in the canonical repo."""
    doc_type = _validate_doc_type(doc_type)
    sha256 = _sha256_of_file(file_path) if file_path else None
    if sha256:
        dup = (
            db.query(UniversityDocument)
            .filter(
                UniversityDocument.sha256 == sha256,
                UniversityDocument.deleted_at.is_(None),
            )
            .first()
        )
        if dup:
            return dup
    d = UniversityDocument(
        id=uuid.uuid4(),
        title=(title or "Untitled university document").strip()[:500],
        doc_type=doc_type,
        source="manual_upload",
        file_path=file_path,
        original_filename=original_filename,
        file_type=file_type,
        file_size=file_size,
        sha256=sha256,
        status="pending_review",
        is_verified=False,
        is_published=False,
    )
    db.add(d)
    db.commit()
    audit(db, "document.record_manual_upload", actor_id=actor_id, actor_role=actor_role,
          target=str(d.id), detail=f"{doc_type}: {d.title}")
    return d


# ---------------------------------------------------------------------------
# Lifecycle transitions (admin-only, audited)
# ---------------------------------------------------------------------------

def verify_document(db: Session, d: UniversityDocument, *, actor_id: str | None = None, actor_role: str | None = "admin") -> UniversityDocument:
    d.is_verified = True
    d.status = "verified"
    db.commit()
    audit(db, "document.verify", actor_id=actor_id, actor_role=actor_role,
          target=str(d.id), detail=d.title)
    db.refresh(d)
    return d


def publish_document(db: Session, d: UniversityDocument, *, actor_id: str | None = None, actor_role: str | None = "admin") -> UniversityDocument:
    if not d.is_verified:
        raise HTTPException(status_code=409, detail="Document must be verified before it can be published.")
    d.is_published = True
    d.status = "published"
    d.published_at = d.published_at or utcnow()
    db.commit()
    audit(db, "document.publish", actor_id=actor_id, actor_role=actor_role,
          target=str(d.id), detail=d.title)
    db.refresh(d)
    return d


def unpublish_document(db: Session, d: UniversityDocument, *, actor_id: str | None = None, actor_role: str | None = "admin") -> UniversityDocument:
    d.is_published = False
    d.status = "verified"
    d.published_at = None
    db.commit()
    audit(db, "document.unpublish", actor_id=actor_id, actor_role=actor_role,
          target=str(d.id), detail=d.title)
    db.refresh(d)
    return d


def hide_document(db: Session, d: UniversityDocument, *, note: str | None = None, actor_id: str | None = None, actor_role: str | None = "admin") -> UniversityDocument:
    d.status = "hidden_hold"
    d.is_published = False
    d.published_at = None
    if note is not None:
        d.review_note = note[:500]
    db.commit()
    audit(db, "document.hide", actor_id=actor_id, actor_role=actor_role,
          target=str(d.id), detail=note or "")
    db.refresh(d)
    return d


def restore_document(db: Session, d: UniversityDocument, *, note: str | None = None, actor_id: str | None = None, actor_role: str | None = "admin") -> UniversityDocument:
    d.status = "pending_review"
    d.deleted_at = None
    d.review_note = (note or d.review_note or "")[:500]
    db.commit()
    audit(db, "document.restore", actor_id=actor_id, actor_role=actor_role,
          target=str(d.id), detail=d.title)
    db.refresh(d)
    return d


def soft_delete_document(db: Session, d: UniversityDocument, *, actor_id: str | None = None, actor_role: str | None = "admin") -> UniversityDocument:
    d.deleted_at = utcnow()
    d.is_published = False
    d.status = "hidden_hold"
    db.commit()
    audit(db, "document.soft_delete", actor_id=actor_id, actor_role=actor_role,
          target=str(d.id), detail=d.title)
    db.refresh(d)
    return d


def reclassify_document(db: Session, d: UniversityDocument, doc_type: str, *, actor_id: str | None = None, actor_role: str | None = "admin") -> UniversityDocument:
    """Change classification (TYPE) — independent of source. Never mutates source."""
    new_type = _validate_doc_type(doc_type)
    old_type = d.doc_type
    d.doc_type = new_type
    d.status = "pending_review"
    d.is_verified = False
    d.is_published = False
    d.published_at = None
    db.commit()
    audit(db, "document.reclassify", actor_id=actor_id, actor_role=actor_role,
          target=str(d.id), detail=f"{old_type} -> {new_type}")
    db.refresh(d)
    return d


# ---------------------------------------------------------------------------
# Backfill (idempotent) from legacy UniversityNotice rows
# ---------------------------------------------------------------------------

def backfill_from_notices(db: Session, *, actor_id: str | None = None) -> dict:
    """One-time, idempotent backfill (Phase 3C-7 directive §7/§15):
    existing LEGACY documents become canonical rows keyed on their sha256 so
    re-running is a no-op.

    Two legacy sources are adapted, both through the SAME canonical write path
    (record_crawled_document / UniversityDocument with sha256-dedup):

      * crawler WebsitePage rows  -> source="crawler"
      * UniversityNotice rows     -> source="notices"

    Nothing runs on page load; this executes only when an admin invokes the
    existing backfill endpoint.
    """
    from app.models import WebsitePage

    created = skipped = errors = 0

    def _sha_of(obj) -> str | None:
        return getattr(obj, "sha256", None) or getattr(obj, "raw_sha256", None) or getattr(obj, "content_hash", None)

    def _dedup(sha: str) -> bool:
        return (
            db.query(UniversityDocument)
            .filter(
                UniversityDocument.sha256 == sha,
                UniversityDocument.deleted_at.is_(None),
            )
            .first()
        ) is not None

    for p in (
        db.query(WebsitePage)
        .filter(WebsitePage.status != "archived")
        .all()
    ):
        try:
            sha = _sha_of(p)
            if not sha or _dedup(sha):
                skipped += 1
                continue
            d = UniversityDocument(
                id=uuid.uuid4(),
                title=(
                    getattr(p, "title", None)
                    or getattr(p, "normalized_title", None)
                    or "Untitled university document"
                )[:500],
                doc_type=_page_to_canonical_doc_type(p),
                source="crawler",
                file_path=getattr(p, "raw_path", None),
                original_filename=getattr(p, "raw_path", None),
                file_type=getattr(p, "content_type", None),
                file_size=getattr(p, "raw_size", None),
                sha256=sha,
                status="pending_review",
                is_verified=False,
                is_published=False,
                source_url=getattr(p, "url", None),
                site_page_id=str(getattr(p, "id", "")) or None,
                confidence=getattr(p, "classification_confidence", None) or None,
                provenance={
                    "signals": (getattr(p, "classification_signals", None) or []),
                    "category": getattr(p, "category", None),
                    "doc_type": getattr(p, "doc_type", None),
                    "legacy_backfill": True,
                },
            )
            db.add(d)
            created += 1
        except Exception:
            errors += 1
            continue

    # UniversityNotice legacy -> canonical (sha256-dedup), same write path.
    from app.models import UniversityNotice

    for n in db.query(UniversityNotice).filter(UniversityNotice.deleted_at.is_(None)).all():
        try:
            sha = _sha_of(n)
            if not sha or _dedup(sha):
                skipped += 1
                continue
            d = UniversityDocument(
                id=uuid.uuid4(),
                title=(getattr(n, "title", None) or "Untitled university notice")[:500],
                doc_type=_notion_to_doc_type(getattr(n, "notice_type", None)),
                source="notices",
                file_path=getattr(n, "file_path", None),
                original_filename=getattr(n, "original_filename", None),
                file_type=getattr(n, "file_type", None),
                file_size=getattr(n, "file_size", None),
                sha256=sha,
                status="verified" if getattr(n, "is_verified", False) else "pending_review",
                is_verified=bool(getattr(n, "is_verified", False)),
                is_published=bool(getattr(n, "is_published", False)),
                source_url=getattr(n, "source_url", None),
            )
            db.add(d)
            created += 1
        except Exception:
            errors += 1
            continue

    db.commit()
    if created:
        audit(db, "document.backfill_legacy", actor_id=actor_id,
              detail=f"created={created} skipped={skipped} errors={errors}")
    return {"created": created, "skipped": skipped, "errors": errors}


def backfill_from_notices_deprecated(db: Session, *, actor_id: str | None = None) -> dict:
    """One-time, idempotent backfill: existing university notices become
    canonical rows keyed on their sha256 so re-running is a no-op."""
    from app.models import UniversityNotice

    created = skipped = errors = 0
    for n in db.query(UniversityNotice).filter(UniversityNotice.deleted_at.is_(None)).all():
        try:
            if not (getattr(n, "sha256", None) or getattr(n, "sha256", None)):
                skipped += 1
                continue
            sha = n.sha256 or n.sha256
            dup = (
                db.query(UniversityDocument)
                .filter(UniversityDocument.sha256 == sha, UniversityDocument.deleted_at.is_(None))
                .first()
            )
            if dup:
                skipped += 1
                continue
            leg = "university_notices"
            d = UniversityDocument(
                id=uuid.uuid4(),
                title=(n.title or "Untitled")[:500],
                doc_type=_notion_to_doc_type(getattr(n, "notice_type", None)),
                source="manual_upload",
                file_path=getattr(n, "file_path", None),
                original_filename=getattr(n, "original_filename", None),
                file_type=getattr(n, "file_type", None),
                file_size=getattr(n, "file_size", None),
                sha256=sha,
                status="verified" if getattr(n, "is_verified", False) else "draft",
                is_verified=bool(getattr(n, "is_verified", False)),
                is_published=bool(getattr(n, "is_published", False)),
            )
            db.add(d)
            created += 1
        except Exception:
            errors += 1
            continue
    db.commit()
    if created:
        audit(db, "document.backfill_notices", actor_id=actor_id,
              detail=f"created={created} skipped={skipped} errors={errors}")
    return {"created": created, "skipped": skipped, "errors": errors}


def _page_to_canonical_doc_type(p) -> str:
    """Canonical doc_type from a WebsitePage row.

    The crawler stores the fine official category (date-sheet / model-paper /
    official-notification / other-official-document) in ``page.category`` and
    the coarse Phase 1 doc_type (knowledge / official / ambiguous) in
    ``page.doc_type``. The fine category is what lands in the canonical
    repository; unknown/ambiguous never collapses to official_notification.
    """
    from app.knowledge_sync.document_classifier import canonical_doc_type

    cat = (getattr(p, "category", None) or "").strip()
    if cat:
        return canonical_doc_type(cat)
    dt = (getattr(p, "doc_type", None) or "").strip().lower()
    if dt == "knowledge":
        return "knowledge"
    return "needs_review"


def classify_website_page(p) -> dict:
    """Re-classify a WebsitePage from its stored content (body-first).

    Returns the Phase 1 classification contract. Never raises: on failure the
    page keeps a needs_review classification so the canonical row is corrected
    rather than guessed.
    """
    from app.knowledge_sync.document_classifier import classify_document

    kwargs = {
        "title": getattr(p, "title", None) or "",
        "url": getattr(p, "url", None) or "",
        "text": getattr(p, "content", None) or "",
        "content_type": getattr(p, "content_type", None) or "document",
    }
    try:
        return classify_document(**kwargs)
    except Exception:
        return {
            "doc_type": "ambiguous",
            "category": "ambiguous",
            "confidence": {"band": "low", "score": 0},
            "signals": ["reclassification error: needs human review"],
        }


def reclassify_documents_from_website(
    db: Session,
    *,
    actor_id: str | None = None,
    dry_run: bool = False,
) -> dict:
    """Re-run content classification over crawler-sourced canonical rows and
    correct demonstrably wrong doc_type values.

    Safety rules:
      * Only rows still awaiting review are touched
        (pending_review / needs_review / draft, is_verified=False).
      * Rows an administrator already finalised (verified / published /
        hidden_hold) are never modified — manual classification wins.
      * The update is a plain UPDATE of doc_type/confidence/provenance; no
        rows are deleted or re-created, and sha256-dedup is untouched.
      * ``dry_run=True`` reports the would-be changes without writing.
    """
    import uuid as _uuid

    from app.models import WebsitePage

    SKIP_STATUSES = frozenset({"verified", "published", "hidden_hold"})
    rows = (
        db.query(UniversityDocument)
        .filter(
            UniversityDocument.source == "crawler",
            UniversityDocument.deleted_at.is_(None),
            UniversityDocument.site_page_id.isnot(None),
            UniversityDocument.is_verified.is_(False),
        )
        .all()
    )
    changed = skipped = errors = 0
    corrected: list[dict] = []
    page_ids = {d.site_page_id for d in rows}
    pages = {
        p.id: p
        for p in db.query(WebsitePage)
        .filter(WebsitePage.id.in_(list(page_ids)))
        .all()
        if p.status != "archived"
    }
    for d in rows:
        if d.status in SKIP_STATUSES:
            skipped += 1
            continue
        p = pages.get(d.site_page_id)
        if p is None:
            skipped += 1
            continue
        classification = classify_website_page(p)
        from app.knowledge_sync.document_classifier import canonical_doc_type_for

        new_type = canonical_doc_type_for(classification)
        if new_type == d.doc_type:
            skipped += 1
            continue
        corrected.append(
            {"id": str(d.id), "title": d.title, "from": d.doc_type, "to": new_type}
        )
        if dry_run:
            changed += 1
            continue
        d.doc_type = new_type
        d.confidence = classification.get("confidence") or d.confidence
        prov = dict(d.provenance or {})
        prov["signals"] = classification.get("signals") or []
        prov["category"] = classification.get("category")
        prov["reclassified_at"] = utcnow().isoformat()
        d.provenance = prov
        db.commit()
        audit(
            db, "document.reclassify_auto", actor_id=actor_id,
            target=str(d.id), detail=f"{corrected[-1]['from']} -> {new_type}",
        )
        changed += 1
    return {"changed": changed, "skipped": skipped, "errors": errors,
            "corrected": corrected}


def _notion_to_doc_type(value: str | None) -> str:
    """Canonical doc_type from a legacy doc_type/category value. Independent of
    the source; unknown/missing values fall back to needs_review so a backfill
    never drops a valid row and never guesses official_notification."""
    v = (value or "").strip().lower()
    if v in ("date_sheet", "date-sheet", "date sheet"):
        return "date_sheet"
    if v in ("model_paper", "model_paper", "model-paper", "model paper"):
        return "model_paper"
    if v in ("official_notification", "official-notification", "official notification", "notice"):
        return "official_notification"
    if v in ("other_official_document", "other-official-document", "other official document"):
        return "other_official_document"
    if v in ("university_notice", "notices"):
        return "official_notification"
    if v in ("knowledge", "amb"):
        return "knowledge"
    return "needs_review"
    return "official_notification"
