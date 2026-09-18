"""
backend/app/university_documents/routes.py — public student endpoints.

Student-facing Read/View/Download access to the canonical university-document
repository, limited to official notifications and other official documents.
Every read is structurally gated to VERIFIED + PUBLISHED, non-deleted rows, and
the file endpoint validates path containment server-side (mirroring
``app/notices/routes.py`` and ``app/examination/routes.py``). The stored
filesystem path is never exposed to the client.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from sqlalchemy.orm import Session

from app.database import get_db
from app.examination.service import safe_filename
from app.university_documents import service as university_documents

router = APIRouter(tags=["university-documents"])

_PREFIX = "/api/university-documents"

_MEDIA_TYPES = {
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "csv": "text/csv",
    "txt": "text/plain",
    "html": "text/html",
    "htm": "text/html",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


def _extension(doc) -> str:
    """Resolve the real file extension.

    ``file_type`` on repository rows is a coarse label ("document"), so the
    stored path / original filename / title are consulted first and only known
    extensions are accepted.
    """
    for candidate in (doc.file_path, doc.original_filename, doc.title, doc.file_type):
        value = (candidate or "").strip().lower()
        if not value:
            continue
        ext = value.rsplit(".", 1)[-1] if "." in value else value
        if ext in _MEDIA_TYPES:
            return ext
    return ""


def _media_type(doc) -> str:
    return _MEDIA_TYPES.get(_extension(doc), "application/octet-stream")


def _download_filename(doc) -> str:
    ext = _extension(doc)
    stem = safe_filename(doc.title or doc.original_filename or "document")
    if ext:
        if stem.lower().endswith("." + ext):
            stem = stem[: -(len(ext) + 1)]
        stem = f"{stem}.{ext}"
    return stem or "document"


@router.get(f"{_PREFIX}/{{document_id}}/file")
def public_get_document_file(
    document_id: str,
    download: bool | None = Query(default=False),
    db: Session = Depends(get_db),
):
    doc = university_documents.get_published_document(db, document_id)
    if doc is None:
        raise HTTPException(status_code=404, detail="Document not available")
    path: Path = university_documents.resolve_document_file(doc)
    return FileResponse(
        str(path),
        media_type=_media_type(doc),
        filename=_download_filename(doc),
        content_disposition_type="attachment" if download else "inline",
    )
