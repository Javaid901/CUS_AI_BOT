"""backend/app/notices/service.py — notice persistence, lifecycle, search.

Lifecycle (admin driven, two-step publication):
    draft -> extracting -> pending_verification -> verified -> published
                                    ^
    extraction_failed / manual_entry┴-> (admin entry) -> pending_verification

Public read paths are structurally gated in SQL to VERIFIED + PUBLISHED,
non-deleted notices and VERIFIED, non-deleted schedule entries. The chatbot
orchestrator may only ever read rows this service returns.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.database import utcnow
from app.models import DateSheetEntry, UniversityNotice
from app.notices.validator import verify_ready_problems
from app.utils.files import ensure_dir, extract_pages_with_tables, sanitize_filename
from app.utils.logging import audit, log

from . import parser as _parser

NOTICE_TYPES = ("notice", "date_sheet")
ENTRY_VERIFIED = "verified"
_AUDIT_SOURCE = "notices"


def notices_root() -> Path:
    """Storage root for notice files — deliberately OUTSIDE the public
    `/api/uploads` static mount so unpublished documents are never served
    by the static file handler."""
    base = Path(__file__).resolve().parent.parent  # backend/
    p = Path(settings.NOTICES_DIR)
    if p.is_absolute():
        return p
    return (base / p).resolve()


def _json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        data = json.loads(value)
        return data if isinstance(data, list) else []
    except (ValueError, TypeError):
        return []


def _to_json(value: list[str] | None) -> str | None:
    if value is None:
        return None
    return json.dumps(list(value), ensure_ascii=False)


def _entry_dict(e: DateSheetEntry) -> dict[str, Any]:
    return {
        "id": str(e.id),
        "notice_id": str(e.notice_id),
        "row_no": e.row_no,
        "programme_id": e.programme_id,
        "programme_name": e.programme_name,
        "stream": e.stream,
        "semester": e.semester,
        "batch": e.batch,
        "exam_type": e.exam_type,
        "exam_date": e.exam_date,
        "day": e.day,
        "start_time": e.start_time,
        "end_time": e.end_time,
        "subject_code": e.subject_code,
        "subject": e.subject,
        "paper_code": e.paper_code,
        "venue": e.venue,
        "source_page": e.source_page,
        "source_section": e.source_section,
        "raw": e.raw,
        "extraction_status": e.extraction_status,
        "is_manual": e.is_manual,
        "is_corrected": e.is_corrected,
        "validation_flags": _json_list(e.validation_flags),
        "created_at": e.created_at.isoformat() if e.created_at else None,
        "updated_at": e.updated_at.isoformat() if e.updated_at else None,
    }


def notice_dto(n: UniversityNotice, *, include_entries: bool = True) -> dict[str, Any]:
    dto = {
        "id": str(n.id),
        "title": n.title,
        "notice_type": n.notice_type,
        "filename": n.filename,
        "original_filename": n.original_filename,
        "file_type": n.file_type,
        "file_size": n.file_size,
        "sha256": n.sha256,
        "categories": _json_list(n.categories),
        "programme_ids": _json_list(n.programme_ids),
        "exam_type": n.exam_type,
        "exam_session_label": n.exam_session_label,
        "notification_date": n.notification_date.isoformat() if n.notification_date else None,
        "extraction_status": n.extraction_status,
        "extraction_error": n.extraction_error,
        "validation_flags": _json_list(n.validation_flags),
        "is_verified": n.is_verified,
        "is_published": n.is_published,
        "published_at": n.published_at.isoformat() if n.published_at else None,
        "source_kind": n.source_kind,
        "created_at": n.created_at.isoformat() if n.created_at else None,
        "updated_at": n.updated_at.isoformat() if n.updated_at else None,
        "deleted_at": n.deleted_at.isoformat() if n.deleted_at else None,
    }
    if include_entries:
        dto["entries"] = [_entry_dict(e) for e in n.entries if e.deleted_at is None]
    return dto


def _active_entries(n: UniversityNotice) -> list[DateSheetEntry]:
    return [e for e in n.entries if e.deleted_at is None]


def _get_uuid(value: str | uuid.UUID) -> uuid.UUID | None:
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        return None


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def store_uploaded_file(filename: str, data: bytes) -> dict[str, Any]:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in ("pdf", "docx"):
        raise HTTPException(
            status_code=400,
            detail="University notices accept PDF and DOCX files only (legacy .doc and scanned PDFs are not supported).",
        )
    max_bytes = settings.MAX_UPLOAD_MB * 1024 * 1024
    if len(data) > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"File too large ({len(data)} bytes). Max {settings.MAX_UPLOAD_MB} MB.",
        )
    clean = sanitize_filename(filename)
    notices_dir = notices_root()
    ensure_dir(str(notices_dir))
    dest = notices_dir / clean
    dest.write_bytes(data)
    return {
        "filename": clean,
        "original_filename": filename,
        "file_type": ext,
        "file_size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "file_path": str(dest),
    }


# ---------------------------------------------------------------------------
# Create + extract
# ---------------------------------------------------------------------------

def upload_and_extract(
    db: Session,
    *,
    created_by: uuid.UUID | None,
    title: str | None,
    notice_type: str | None,
    categories: list[str] | None,
    programme_ids: list[str] | None,
    filename: str,
    data: bytes,
) -> UniversityNotice:
    dup = (
        db.query(UniversityNotice)
        .filter(UniversityNotice.deleted_at.is_(None))
        .all()
    )
    sha = hashlib.sha256(data).hexdigest()
    for existing in dup:
        if existing.sha256 == sha:
            raise HTTPException(
                status_code=409,
                detail="A notice with identical file content already exists (merged by file fingerprint).",
            )

    file_info = store_uploaded_file(filename, data)
    file_info["sha256"] = sha

    pages = []
    try:
        pages = extract_pages_with_tables(file_info["file_path"], file_info["file_type"])
    except HTTPException as exc:
        pages = []
        file_info["parse_error"] = str(exc.detail)

    result = _parser.parse_date_sheet(pages) if pages else None

    effective_type = (notice_type or "").strip().lower()
    if not effective_type and result is not None:
        effective_type = result.notice_type
    effective_type = effective_type if effective_type in NOTICE_TYPES else "notice"

    n = UniversityNotice(
        id=uuid.uuid4(),
        title=(title or filename).strip()[:400],
        notice_type=effective_type,
        filename=file_info["filename"],
        original_filename=file_info["original_filename"],
        file_type=file_info["file_type"],
        file_size=file_info["file_size"],
        sha256=file_info["sha256"],
        file_path=file_info["file_path"],
        categories=_to_json(categories or []),
        programme_ids=_to_json(programme_ids or (result.programme_ids if result else None)),
        exam_type=result.exam_type if result else None,
        exam_session_label=result.exam_session_label if result else None,
        extraction_status="draft",
        created_by=created_by,
    )
    db.add(n)
    db.flush()

    if result is None or result.extraction_status == "extraction_failed":
        n.extraction_status = result.extraction_status if result else "extraction_failed"
        n.extraction_error = result.extraction_error if result else (file_info.get("parse_error") or "Document could not be parsed.")
    elif result.notice_type == "notice" and not result.rows:
        n.extraction_status = "pending_verification"
    else:
        _persist_rows(db, n, result.rows)

    db.commit()
    audit(db, action="notice.upload", actor_id=str(created_by) if created_by else None,
          actor_role="superadmin", target=str(n.id), detail=f"{n.notice_type}: {n.title} (sha256={n.sha256})")

    n = db.query(UniversityNotice).filter(UniversityNotice.id == n.id).one()
    return n


def _persist_rows(db: Session, n: UniversityNotice, rows: list[dict]) -> None:
    db.query(DateSheetEntry).filter(DateSheetEntry.notice_id == n.id).delete()
    for r in rows:
        db.add(
            DateSheetEntry(
                notice_id=n.id,
                row_no=int(r.get("row_no") or 0),
                programme_id=r.get("programme_id"),
                programme_name=r.get("programme_name"),
                stream=r.get("stream"),
                semester=r.get("semester"),
                batch=r.get("batch"),
                exam_type=r.get("exam_type"),
                exam_date=r.get("exam_date"),
                day=r.get("day"),
                start_time=r.get("start_time"),
                end_time=r.get("end_time"),
                subject_code=r.get("subject_code"),
                subject=r.get("subject"),
                paper_code=r.get("paper_code"),
                venue=r.get("venue"),
                source_page=r.get("source_page"),
                source_section=r.get("source_section"),
                raw=r.get("raw"),
                extraction_status="pending_verification",
                validation_flags=_to_json(r.get("validation_flags") or []),
            )
        )
    n.extraction_status = "pending_verification"
    n.extraction_error = None


def re_extract(db: Session, n: UniversityNotice) -> UniversityNotice:
    """Re-run the deterministic parser over the stored file (idempotent)."""
    n.extraction_status = "extracting"
    db.commit()
    try:
        pages = extract_pages_with_tables(n.file_path, n.file_type)
        result = _parser.parse_date_sheet(pages)
    except HTTPException as exc:
        n.extraction_status = "extraction_failed"
        n.extraction_error = str(exc.detail)
        db.commit()
        return n
    if result.extraction_status == "extraction_failed":
        n.extraction_status = "extraction_failed"
        n.extraction_error = result.extraction_error
        _persist_rows(db, n, [])
        db.commit()
        return n
    n.programme_ids = _to_json(result.programme_ids or _json_list(n.programme_ids))
    _persist_rows(db, n, result.rows)
    db.commit()
    return n


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------

def verify_notice(db: Session, n: UniversityNotice, actor_id: str | None) -> UniversityNotice:
    if n.is_verified:
        return n
    entries = _active_entries(n)
    if n.notice_type == "date_sheet" and entries:
        problems = verify_ready_problems([_entry_dict(e) for e in entries])
        if problems:
            raise HTTPException(
                status_code=422,
                detail={"error": "Schedule rows are not ready to verify — correct or remove the flagged rows first.", "problems": problems},
            )
    for e in entries:
        e.extraction_status = ENTRY_VERIFIED
    n.is_verified = True
    n.extraction_status = "verified"
    db.commit()
    audit(db, action="notice.verify", actor_id=actor_id, actor_role="superadmin",
          target=str(n.id), detail=f"verified {len(entries)} schedule rows")
    return n


def publish_notice(db: Session, n: UniversityNotice, actor_id: str | None) -> UniversityNotice:
    if not n.is_verified:
        raise HTTPException(status_code=409, detail="Notice must be verified before it can be published.")
    if not n.is_published:
        n.is_published = True
        n.published_at = utcnow()
        db.commit()
        audit(db, action="notice.publish", actor_id=actor_id, actor_role="superadmin",
              target=str(n.id), detail=n.title)
    return n


def unpublish_notice(db: Session, n: UniversityNotice, actor_id: str | None) -> UniversityNotice:
    if n.is_published:
        n.is_published = False
        n.published_at = None
        db.commit()
        audit(db, action="notice.unpublish", actor_id=actor_id, actor_role="superadmin",
              target=str(n.id), detail=n.title)
    return n


def soft_delete_notice(db: Session, n: UniversityNotice, actor_id: str | None) -> None:
    n.deleted_at = utcnow()
    db.commit()
    audit(db, action="notice.delete", actor_id=actor_id, actor_role="superadmin",
          target=str(n.id), detail=n.title)


def hard_delete_notice(db: Session, n: UniversityNotice, actor_id: str | None) -> None:
    try:
        path = Path(n.file_path)
        if path.is_file():
            path.unlink(missing_ok=True)
    except Exception as exc:  # pragma: no cover
        log.warning("notice file cleanup failed for %s: %s", n.id, exc)
    audit(db, action="notice.delete_hard", actor_id=actor_id, actor_role="superadmin",
          target=str(n.id), detail=n.title)
    db.delete(n)
    db.commit()


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------

def get_notice(db: Session, notice_id: str, *, published_only: bool = False,
               include_deleted: bool = False) -> UniversityNotice | None:
    uid = _get_uuid(notice_id)
    if uid is None:
        return None
    q = db.query(UniversityNotice).filter(UniversityNotice.id == uid)
    if not include_deleted:
        q = q.filter(UniversityNotice.deleted_at.is_(None))
    if published_only:
        q = q.filter(
            UniversityNotice.is_verified.is_(True),
            UniversityNotice.is_published.is_(True),
        )
    return q.first()


def list_notices(
    db: Session,
    *,
    published_only: bool = False,
    q: str | None = None,
    programme: str | None = None,
    notice_type: str | None = None,
    limit: int | None = None,
    include_deleted: bool = False,
) -> list[UniversityNotice]:
    limit = min(limit if limit and limit > 0 else settings.NOTICES_SEARCH_TOP_N, 200)
    qry = db.query(UniversityNotice)
    if not include_deleted:
        qry = qry.filter(UniversityNotice.deleted_at.is_(None))
    if published_only:
        qry = qry.filter(
            UniversityNotice.is_verified.is_(True),
            UniversityNotice.is_published.is_(True),
        )
    if notice_type:
        qry = qry.filter(UniversityNotice.notice_type == notice_type)
    if programme:
        qry = qry.filter(UniversityNotice.programme_ids.like(f'%"{programme.lower()}"%'))
    if q:
        like = f"%{q.strip()}%"
        qry = qry.filter(
            or_(
                UniversityNotice.title.like(like),
                UniversityNotice.categories.like(f'%{q.strip()}%'),
                UniversityNotice.programme_ids.like(f'%{q.strip()}%'),
            )
        )
    qry = qry.order_by(
        # Unpublished/in-verified rows sink below published ones on both
        # dialects: SQLite already puts NULLs last in DESC order, and the
        # explicit nulls_last() reproduces exactly that behavior on PostgreSQL
        # (whose default is NULLs FIRST in DESC).
        UniversityNotice.published_at.desc().nulls_last(),
        UniversityNotice.created_at.desc(),
    )
    return qry.limit(limit).all()


def get_verified_schedule(
    db: Session,
    notice_ids: list[uuid.UUID] | list[str],
    *,
    programme: str | None = None,
    semester: int | str | None = None,
    stream: str | None = None,
    batch: str | None = None,
    limit: int | None = None,
) -> list[DateSheetEntry]:
    """VERIFIED + PUBLISHED rows only — the structural zero-hallucination gate."""
    qry = (
        db.query(DateSheetEntry)
        .join(UniversityNotice)
        .filter(
            UniversityNotice.id.in_(notice_ids),
            UniversityNotice.is_verified.is_(True),
            UniversityNotice.is_published.is_(True),
            UniversityNotice.deleted_at.is_(None),
            DateSheetEntry.deleted_at.is_(None),
            DateSheetEntry.extraction_status == ENTRY_VERIFIED,
        )
    )
    if programme:
        qry = qry.filter(DateSheetEntry.programme_id == programme.lower())
    if semester is not None and str(semester) not in ("", "0"):
        qry = qry.filter(DateSheetEntry.semester == str(semester))
    if stream:
        qry = qry.filter(DateSheetEntry.stream == stream.lower())
    if batch:
        qry = qry.filter(DateSheetEntry.batch == batch)
    qry = qry.order_by(DateSheetEntry.exam_date, DateSheetEntry.row_no)
    return qry.limit(limit or settings.NOTICES_SCHEDULE_LIMIT).all()


def entries_for_notice(db: Session, n: UniversityNotice) -> list[DateSheetEntry]:
    return (
        db.query(DateSheetEntry)
        .filter(DateSheetEntry.notice_id == n.id, DateSheetEntry.deleted_at.is_(None))
        .order_by(DateSheetEntry.row_no)
        .all()
    )


def revalidate_after_edit(db: Session, n: UniversityNotice) -> None:
    """Keep the structural verified-invariant after any schedule mutation.

    If a VERIFIED date-sheet notice is edited so that any row is no longer
    verify-ready, the whole notice drops back to PENDING_VERIFICATION and is
    auto-unpublished — stale or unverifiable rows are never served. If the
    rows all remain verify-ready, the VERIFIED marker is refreshed onto them.
    """
    if n.notice_type != "date_sheet" or not n.is_verified:
        return
    entries = _active_entries(n)
    problems = verify_ready_problems([_entry_dict(e) for e in entries])
    if problems:
        for e in entries:
            e.extraction_status = "pending_verification"
        n.is_verified = False
        n.extraction_status = "pending_verification"
        if n.is_published:
            n.is_published = False
            n.published_at = None
        db.commit()
    else:
        for e in entries:
            e.extraction_status = ENTRY_VERIFIED
        db.commit()


def resolve_stored_file(n: UniversityNotice) -> Path:
    """Path of the stored notice file with containment + existence checks.

    The client-supplied path is never trusted; only the DB path under the
    notices root is accepted. No publish gate here — admin preview uses this.
    """
    root = notices_root().resolve()
    f = Path(n.file_path).resolve()
    try:
        f.relative_to(root)
    except ValueError:
        log.warning("notice file containment failure: %s is outside %s", f, root)
        raise HTTPException(status_code=404, detail="Notice file not available.")
    if not f.is_file():
        raise HTTPException(status_code=404, detail="Notice file not available.")
    return f


def resolve_notice_file(n: UniversityNotice) -> Path:
    """Return the on-disk file path for a published notice, with containment
    checks. The client-supplied path is never trusted — only the DB path."""
    if n.deleted_at is not None or not n.is_published or not n.is_verified:
        raise HTTPException(status_code=404, detail="Notice not available.")
    root = notices_root().resolve()
    f = Path(n.file_path).resolve()
    try:
        f.relative_to(root)
    except ValueError:
        log.warning("notice file containment failure: %s is outside %s", f, root)
        raise HTTPException(status_code=404, detail="Notice file not available.")
    if not f.is_file():
        raise HTTPException(status_code=404, detail="Notice file not available.")
    return f