"""
backend/app/ingest/restore_missing.py

P0-A knowledge-retrieval recovery helper.

Restores vector coverage for `Document` rows that are marked `ready` but have no
chunks in the live Chroma collection. The live Postgres DB does not persist the
website page text (website_pages.content is empty), but the sqlite snapshot at
`backend/cus_ai.db` retains it; this helper re-embeds that stored text using the
existing chunk/embed/store pipeline and writes ONLY to Chroma.

Properties:
  * idempotent - each target document is delete-then-re-added by its existing
    document_id, so re-running converges to the same vector set.
  * non-destructive to the relational DB - `documents` / page rows are read,
    never written or committed.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from app.catalogue.models import CurriculumUpload
from app.config import settings
from app.ingest.chunker import chunk_pages
from app.ingest.embed import embed_documents
from app.ingest.store import add_chunks_with_embeddings, delete_document, get_all_chunks
from app.knowledge_sync.document_classifier import canonical_doc_type_for
from app.models import Document


def _norm_url(value: str | None) -> str:
    return (value or "").strip().lower().rstrip("/")


def _json_or(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def load_sqlite_website_text(sqlite_path: str | Path) -> dict[str, dict[str, Any]]:
    """Return {normalized_url: website_pages row dict} for rows with content."""
    path = Path(sqlite_path)
    if not path.exists():
        return {}
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute("SELECT * FROM website_pages").fetchall()
    finally:
        conn.close()
    out: dict[str, dict[str, Any]] = {}
    for row in rows:
        data = dict(row)
        if (data.get("content") or "").strip():
            out[_norm_url(data.get("url"))] = data
    return out


def _chroma_document_ids() -> set[str]:
    return {
        str(c.get("document_id"))
        for c in get_all_chunks()
        if c.get("document_id")
    }


def _missing_ready_documents(db: Session) -> list[Document]:
    chroma_ids = _chroma_document_ids()
    docs = db.query(Document).filter(Document.status == "ready").all()
    return [d for d in docs if str(d.id) not in chroma_ids]


def _website_extra(page: dict[str, Any], doc: Document) -> dict[str, str]:
    confidence = _json_or(page.get("classification_confidence"), {})
    classification = {
        "doc_type": page.get("doc_type"),
        "category": page.get("category"),
        "confidence": confidence or {"band": "low", "score": 0},
        "signals": _json_or(page.get("classification_signals"), []),
    }
    return {
        "document_type": "website",
        "category": page.get("category") or doc.category or "",
        "doc_type": canonical_doc_type_for(classification),
        "classification_confidence": (confidence or {}).get("band", ""),
        "source_url": doc.original_filename or page.get("url") or "",
        "source": "website",
        "scope": doc.scope or "university",
    }


def _curriculum_extra(upload: CurriculumUpload, payload: dict[str, Any], doc: Document) -> dict[str, str]:
    scheme = getattr(upload, "scheme_code", None)
    if not scheme and isinstance(payload.get("scheme"), dict):
        scheme = (payload["scheme"] or {}).get("code")
    extra = {
        "document_type": "curriculum",
        "programme": (upload.programme_code or doc.programme or "").lower()[:50],
    }
    if scheme:
        extra["academic_scheme"] = scheme
    return extra


def restore_missing_vectors(  # noqa: C901
    db: Session,
    *,
    sqlite_path: str | Path | None = None,
    apply: bool = False,
    limit: int | None = None,
) -> dict[str, Any]:
    """Re-embed ready documents missing from Chroma. Returns a report dict."""
    sqlite_path = sqlite_path or (Path(settings.CHROMA_PERSIST_DIR).parent / "cus_ai.db")
    if not Path(sqlite_path).exists():
        candidate = Path(__file__).resolve().parents[2] / "cus_ai.db"
        if candidate.exists():
            sqlite_path = candidate
    sqlite_text = load_sqlite_website_text(sqlite_path)

    missing = _missing_ready_documents(db)
    report: dict[str, Any] = {
        "apply": apply,
        "sqlite_path": str(sqlite_path),
        "sqlite_pages_with_content": len(sqlite_text),
        "missing_before": len(missing),
        "website": {"targets": 0, "indexed": 0, "chunks_added": 0, "no_text": [], "errors": []},
        "curriculum": {"targets": 0, "indexed": 0, "chunks_added": 0, "skipped": [], "errors": []},
    }

    processed = 0
    for doc in missing:
        if limit is not None and processed >= limit:
            break

        if doc.document_type == "website":
            bucket = report["website"]
            bucket["targets"] += 1
            page = sqlite_text.get(_norm_url(doc.original_filename))
            content = (page or {}).get("content") or ""
            if not page or not content.strip():
                bucket["no_text"].append(str(doc.id))
                processed += 1
                continue
            if not apply:
                processed += 1
                continue
            try:
                chunks = chunk_pages([{"page": 1, "text": content}])
                if not chunks:
                    bucket["no_text"].append(str(doc.id))
                    processed += 1
                    continue
                embeddings = embed_documents([c["content"] for c in chunks])
                delete_document(str(doc.id))
                added = add_chunks_with_embeddings(
                    str(doc.id), doc.title, chunks, embeddings, _website_extra(page, doc)
                )
                bucket["indexed"] += 1
                bucket["chunks_added"] += added
            except Exception as exc:  # noqa: BLE001
                bucket["errors"].append({"document_id": str(doc.id), "error": str(exc)[:300]})
            processed += 1
            continue

        if doc.document_type == "curriculum":
            bucket = report["curriculum"]
            bucket["targets"] += 1
            upload = (
                db.query(CurriculumUpload)
                .filter(CurriculumUpload.document_id == doc.id)
                .order_by(CurriculumUpload.status != "active")
                .first()
            )
            if upload is None or upload.status != "active" or not upload.payload:
                reason = "no upload" if upload is None else f"status={upload.status}"
                bucket["skipped"].append({"document_id": str(doc.id), "reason": reason})
                processed += 1
                continue
            if not apply:
                processed += 1
                continue
            try:
                from app.catalogue.service import _payload_pages  # noqa: PLC0415

                payload = upload.payload or {}
                chunks = chunk_pages(_payload_pages(payload))
                if not chunks:
                    bucket["skipped"].append({"document_id": str(doc.id), "reason": "empty payload"})
                    processed += 1
                    continue
                embeddings = embed_documents([c["content"] for c in chunks])
                delete_document(str(doc.id))
                added = add_chunks_with_embeddings(
                    str(doc.id), doc.title, chunks, embeddings, _curriculum_extra(upload, payload, doc)
                )
                bucket["indexed"] += 1
                bucket["chunks_added"] += added
            except Exception as exc:  # noqa: BLE001
                bucket["errors"].append({"document_id": str(doc.id), "error": str(exc)[:300]})
            processed += 1

    report["missing_after"] = len(_missing_ready_documents(db)) if apply else report["missing_before"]
    report["chroma_chunks_after"] = len(get_all_chunks()) if apply else None
    return report
