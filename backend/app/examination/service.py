"""
backend/app/examination/service.py

Data layer for the Examinations dedicated services.

Model Papers
  Only **verified** rows in ``website_pages`` whose ``category`` is
  ``"model-paper"`` may ever surface to students. The list endpoint
  (``list_model_papers``) and the detail/file helper (``get_model_paper`` /
  ``resolve_model_paper_file``) both enforce this rule internally, so the
  engine and the route never need to think about it.

Exam Fee Structure / Division Improvement
  ``exam_fee_source`` / ``division_improvement_source`` scan the verified
  official-source corpus for relevant content markers. Today none exist (all
  relevant pages are either pending_review or lack the required text), so both
  return ``None`` — the engine emits an honest not-available message rather
  than fabricating facts. A future crawl/admin-verification cycle that lands
  the official content in a verified page will automatically light up the
  structured response path.

Security invariants (mandatory):
  * All file paths are resolved through ``raw_store.resolve_contained``; any
    escape attempt (``..``, absolute/drive path, empty) returns ``None``.
  * The raw root (``settings.WEBSITE_SYNC_RAW_DIR``) is outside the public
    ``/api/uploads`` mount, so raw files can never be served by the static
    handler.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.knowledge_sync.raw_store import resolve_contained
from app.models import WebsitePage
from app.examination import metadata as paper_meta

_MODEL_PAPER_CATEGORY = "model-paper"

# Only these WebsitePage statuses represent live, accessible content.
_LIVE_PAGE_STATUSES = ("new", "updated", "unchanged")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _verified_model_paper_query(db: Session):
    """Base query: verified model-paper rows with an on-disk raw file."""
    return db.query(WebsitePage).filter(
        WebsitePage.category == _MODEL_PAPER_CATEGORY,
        WebsitePage.classification_status == "verified",
        WebsitePage.status.in_(_LIVE_PAGE_STATUSES),
        WebsitePage.raw_path.isnot(None),
    )


def _doc_meta_fields(row: WebsitePage) -> dict[str, Any]:
    """Pull official metadata fields that are *actually present* (never guess).

    Only ``subject``, ``programme``, ``semester``, ``batch`` and
    ``academic_year`` are surfaced when the crawler-sourced ``doc_meta``
    JSON carries a non-empty value. All other keys are silently ignored.
    """
    meta = row.doc_meta or {}
    out: dict[str, Any] = {}
    for key in ("subject", "programme", "semester", "batch", "academic_year"):
        val = meta.get(key)
        if val is not None and str(val).strip():
            out[key] = val
    return out


def _paper_dict(row: WebsitePage) -> dict[str, Any]:
    """Minimal card dict for the engine / frontend.

    Metadata gaps are filled deterministically from the row's title/URL/raw
    filename (see ``metadata.enrich_paper``) so filtering by programme,
    semester, subject, batch or academic year is always possible.
    """
    d: dict[str, Any] = {
        "id": str(row.id),
        "title": (row.title or "").strip() or "Model Paper",
        "document_id": row.document_id,
        "content_type": row.content_type,
        "published_at": (row.reviewed_at.isoformat()[:10]
                         if row.reviewed_at else None),
    }
    d.update(_doc_meta_fields(row))
    return paper_meta.enrich_paper(
        d,
        url=row.url or "",
        title=row.title or "",
        raw_path=row.raw_path or "",
    )


# ---------------------------------------------------------------------------
# Model Papers
# ---------------------------------------------------------------------------

def _paper_file_exists(row: WebsitePage) -> bool:
    """True when the raw copy resolves inside the raw root AND is on disk.

    A model paper whose raw file is missing or escapes the storage root is not
    eligible to be shown: its View/Download would 404, so advertising the card
    would be worse than not showing it.
    """
    if not row.raw_path:
        return False
    resolved = resolve_contained(row.raw_path)
    return resolved is not None and resolved.is_file()


def _matches_constraint(field_value: Any, wanted: str | int | None) -> bool:
    """Strict equality match for a single metadata constraint.

    A wanted constraint always counts unless the field value is missing.
    Comparison is case-insensitive and whitespace-stripped; semester values
    are compared numerically when possible.
    """
    if wanted is None:
        return True
    if field_value in (None, ""):
        return False
    if isinstance(wanted, int):
        try:
            return int(str(field_value).strip().split(".")[0]) == wanted
        except (TypeError, ValueError):
            return False
    return str(field_value).strip().lower() == str(wanted).strip().lower()


def _constrained(
    paper: dict[str, Any],
    programme: str | None = None,
    semester: int | None = None,
    subject: str | None = None,
    subject_code: str | None = None,
    batch: str | None = None,
    academic_year: str | None = None,
) -> bool:
    """Strict AND filter: a paper must satisfy every supplied constraint."""
    if subject_code is not None and not _matches_constraint(paper.get("subject_code"), subject_code):
        return False
    return all((
        _matches_constraint(paper.get("programme"), programme),
        _matches_constraint(paper.get("semester"), semester),
        _matches_constraint(paper.get("subject"), subject),
        _matches_constraint(paper.get("batch"), batch),
        _matches_constraint(paper.get("academic_year"), academic_year),
    ))


def list_model_papers(
    db: Session,
    document_ids: list[str] | None = None,
    programme: str | None = None,
    semester: int | None = None,
    subject: str | None = None,
    subject_code: str | None = None,
    batch: str | None = None,
    academic_year: str | None = None,
) -> list[dict[str, Any]]:
    """Verified model-paper rows, optionally scoped and filtered.

    ``document_ids`` narrows to a set of document IDs (SQL-level). All other
    constraints are applied as a strict AND over the card metadata, which
    includes values derived deterministically from the row text. A paper is
    returned only when it satisfies every supplied constraint.
    """
    q = _verified_model_paper_query(db)
    if document_ids:
        q = q.filter(WebsitePage.document_id.in_(document_ids))
    result = []
    for r in q.order_by(WebsitePage.title.asc()).all():
        if not _paper_file_exists(r):
            continue
        paper = _paper_dict(r)
        if _constrained(
            paper,
            programme=programme,
            semester=semester,
            subject=subject,
            subject_code=subject_code,
            batch=batch,
            academic_year=academic_year,
        ):
            result.append(paper)
    return result


def select_model_paper(db: Session, page_id: str) -> dict[str, Any] | None:
    """Return a single card dict (server-validated) for a verified paper.

    Returns ``None`` when the page is not a verified, live, on-disk model
    paper — the caller must never trust a client-supplied page ID.
    """
    row = _verified_model_paper_query(db).filter(WebsitePage.id == page_id).first()
    if row is None or not _paper_file_exists(row):
        return None
    return _paper_dict(row)


def get_model_paper(db: Session, page_id: str) -> dict[str, Any] | None:
    """Return a card dict for a single verified model-paper row, or ``None``."""
    row = _verified_model_paper_query(db).filter(WebsitePage.id == page_id).first()
    if row is None:
        return None
    d = _paper_dict(row)
    d["raw_path"] = row.raw_path
    return d


def resolve_model_paper_file(page: dict[str, Any]) -> Path | None:
    """Containment-safe resolved path, or ``None``."""
    rel = page.get("raw_path")
    if not rel:
        return None
    resolved = resolve_contained(rel)
    if resolved is None or not resolved.is_file():
        return None
    return resolved


def model_paper_file_url(page_id: str) -> str:
    return f"/api/examinations/model-papers/{page_id}/file"


def safe_filename(title: str) -> str:
    """Normalise a user-facing title into a filesystem-safe filename stem."""
    base = re.sub(r"[^A-Za-z0-9 _\-.]", "", title or "").strip()
    base = re.sub(r"\s+", "_", base) or "ModelPaper"
    return base[:120]


# ---------------------------------------------------------------------------
# Official-source lookups (Exam Fee / Division Improvement)
# ---------------------------------------------------------------------------

_FEE_CONTENT_MARKERS = (
    "examination fee",
    "exam fee",
    "fee for examination",
    "fee for the examination",
    "exam form fee",
    "examination fee structure",
    "fee structure of examination",
)

_DIVISION_CONTENT_MARKERS = (
    "division improvement",
    "division criteria",
    "improvement examination",
    "improvement exam",
)


def _official_content_source(
    db: Session,
    markers: tuple[str, ...],
    *,
    category_hint: str | None = None,
) -> dict[str, Any] | None:
    """Scan verified official pages for content markers.

    Returns a lightweight dict with ``title``, ``content`` and ``url`` when a
    verified page carries the relevant text, or ``None``. The engine decides
    how to format the card; no fabrication occurs.
    """
    q = db.query(WebsitePage).filter(
        WebsitePage.classification_status == "verified",
        WebsitePage.status.in_(_LIVE_PAGE_STATUSES),
        WebsitePage.content.isnot(None),
    )
    if category_hint:
        q = q.filter(WebsitePage.category == category_hint)
    for row in q.all():
        low = (row.content or "").lower()
        if any(m in low for m in markers):
            return {
                "title": (row.title or "Official Document").strip(),
                "content": row.content or "",
                "url": row.url or "",
            }
    return None


def exam_fee_source(db: Session) -> dict[str, Any] | None:
    return _official_content_source(db, _FEE_CONTENT_MARKERS)


def division_improvement_source(db: Session) -> dict[str, Any] | None:
    return _official_content_source(db, _DIVISION_CONTENT_MARKERS)