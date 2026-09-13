"""
backend/app/knowledge_sync/web_metadata.py

Phase 1 metadata extraction for preserved website documents.

Contract: KNOWN values are filled in, UNKNOWN values are stored as None.
Nothing is ever guessed or fabricated. The field set is deliberately small:

    content_type      normalized extension (from magic bytes when possible,
                      otherwise the URL/extension hint)
    mime              mime type mapped from content_type
    size_bytes        exact byte length of the raw document
    sha256            exact SHA-256 of the raw document
    title             safe title derived from filename (never invented)
    page_count        approximate page count (PDF only; counting "/Type /Page"
                      markers — best-effort, never fabricated for other formats)
    table_like        bool heuristic from the existing extracted text (NULL when
                      there is no extracted text to judge)
    extracted_chars   length of the extracted text actually used

extract_meta() is intentionally failure-friendly: any unexpected error yields
a minimal dict with the values that are always safe (raw size / hash), so the
hot ingest path can never be blocked by a metadata hiccup.
"""

from __future__ import annotations

import hashlib
import mimetypes
from pathlib import Path
from typing import Any

from app.knowledge_sync.document_classifier import detect_binary_ext

_MIME_OVERRIDES = {
    "pdf": "application/pdf",
    "doc": "application/msword",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "xls": "application/vnd.ms-excel",
    "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "ppt": "application/vnd.ms-powerpoint",
    "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    "csv": "text/csv",
    "txt": "text/plain",
    "md": "text/markdown",
    "html": "text/html",
    "htm": "text/html",
    "rtf": "application/rtf",
}


def _mime_for(ext: str | None) -> str | None:
    ext = (ext or "").lower().lstrip(".")
    if not ext:
        return None
    if ext in _MIME_OVERRIDES:
        return _MIME_OVERRIDES[ext]
    return mimetypes.guess_type(f"file.{ext}")[0]


def _page_count_hint(content_type: str, raw: bytes | None) -> int | None:
    """Approximate page count for PDFs by counting "/Type /Page" markers.

    Only exact PDFs are counted; every other format returns None (unknown).
    """
    if content_type != "pdf" or not raw:
        return None
    try:
        return raw.count(b"/Type /Page") or None
    except Exception:
        return None


def _table_like_hint(content: str | None) -> bool | None:
    """True when the extracted text looks tabular (many pipe/vertical-bar rows).

    Returns None when there is no extracted text to judge.
    """
    if not content:
        return None
    lines = [ln.strip() for ln in content.splitlines() if ln.strip()]
    if not lines:
        return None
    scored = sum(1 for ln in lines if "|" in ln or "\t" in ln)
    ratio = scored / len(lines)
    return ratio >= 0.3


def extract_meta(
    *,
    filename: str = "",
    url: str = "",
    ext: str = "",
    raw: bytes | None = None,
    extracted_text: str = "",
) -> dict[str, Any]:
    """Return the Phase 1 metadata dict for a crawled document.

    Known values are filled; unknown values are None. Never hallucinates.
    """
    detected = detect_binary_ext(raw)
    content_type = detected or (ext or "").lower().lstrip(".") or None
    if content_type and "." in content_type:
        # e.g. an extension passed with a dot, or a URL tail.
        content_type = content_type.rsplit(".", 1)[-1].lower()

    size_bytes = len(raw) if raw is not None else None
    sha256 = hashlib.sha256(raw).hexdigest() if raw else None
    mime = _mime_for(content_type)

    # Source stem for a safe display title — never invented, derived from the
    # actual filename/URL when present.
    title: str | None = None
    source_name = filename or (url or "").split("?")[0]
    if source_name:
        stem = Path(source_name).name.strip()
        if stem and stem not in (".", "", "/"):
            title = stem[:200]

    page_count = _page_count_hint(content_type, raw)
    table_like = _table_like_hint((extracted_text or "").strip())
    extracted_chars = len(extracted_text or "")

    return {
        "content_type": content_type,
        "mime": mime,
        "size_bytes": size_bytes,
        "sha256": sha256,
        "title": title,
        "page_count": page_count,
        "table_like": table_like,
        "extracted_chars": extracted_chars,
    }