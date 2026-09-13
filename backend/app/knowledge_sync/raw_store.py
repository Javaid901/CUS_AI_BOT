"""
backend/app/knowledge_sync/raw_store.py

Phase 1 raw-document preservation for the website sync pipeline.

Preserves the original bytes of crawled binary documents on disk so the
metadata/provenance of an "official" document is never lost.

Security invariants (mandatory):
  * The storage root is WEBSITE_SYNC_RAW_DIR — never the public /api/uploads
    mount, so raw files can never be served by the static handler.
  * A stored file is named <uuid>.<safe-ext> — the original filename never
    appears, preventing extension/path tricks.
  * Only a whitelist of extensions is accepted.
  * Writes are atomic: a temp file is fsync'ed then os.replace()d into place.
  * Every path resolution is contained: resolve_contained() refuses paths that
    escape the storage root (no "..", no absolute, no drive traversal).
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
import uuid
from pathlib import Path
from typing import Any

from app.config import settings

RAW_EXT_WHITELIST = frozenset(
    {
        "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx",
        "csv", "txt", "md", "html", "htm", "rtf",
    }
)

_EXT_RE = re.compile(r"^[A-Za-z0-9]{1,10}$")


def raw_root() -> Path:
    """Absolute, normalized storage root (created on demand)."""
    root = Path(settings.WEBSITE_SYNC_RAW_DIR).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def sanitize_ext(ext: str) -> str | None:
    """Return a safe extension from the whitelist, or None if unsupported."""
    ext = (ext or "").lower().lstrip(".")
    if _EXT_RE.match(ext) and ext in RAW_EXT_WHITELIST:
        return ext
    return None


def resolve_contained(rel_path: str | None) -> Path | None:
    """Resolve a relative stored path, refusing any escape from the root.

    Dangerous inputs ("..", absolute/drive paths, empty) return None.
    """
    if not rel_path:
        return None
    root = raw_root()
    try:
        candidate = (root / rel_path).resolve()
    except (OSError, ValueError):
        return None
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    if candidate == root:
        return None
    return candidate


def store_raw(raw: bytes, ext: str) -> dict[str, Any] | None:
    """Persist raw document bytes atomically; return provenance dict or None.

    Return value: {"rel_path", "sha256", "size"}
    """
    safe = sanitize_ext(ext)
    if safe is None:
        return None
    if not raw:
        return None
    root = raw_root()
    rel_path = f"{uuid.uuid4().hex}.{safe}"
    target = root / rel_path

    fd, tmp_name = tempfile.mkstemp(prefix=".raw-", suffix=".tmp", dir=str(root))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(raw)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, str(target))
    except OSError:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        return None

    return {
        "rel_path": rel_path,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size": len(raw),
    }