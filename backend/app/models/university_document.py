"""
backend/app/models/university_document.py

Canonical University Document repository (Phase 3C-7 — unified document
management).

Every official/university document — whether it came from the website crawler
(Website Sync) or from an authorised manual upload — lands in ONE table:

    university_documents

Classification is carried by ``doc_type`` and is INDEPENDENT of the origin of
the document, which is carried by ``source``. This is intentional:

    source = crawler        doc_type = date_sheet          MCA date sheet crawled
    source = manual_upload  doc_type = date_sheet          MCA date sheet uploaded
    source = crawler        doc_type = official_notification   crawled notice
    source = manual_upload  doc_type = model_paper             uploaded paper

The rows here are the single source of truth an administrator manages. The
crawler writes here after classification; manual uploads write here on upload.
Website Sync itself stays a SEPARATE administrative control panel — it only
manages the crawler, its output is unified into this repository.

Student-facing consumers (date sheets, model papers, official notifications,
exam flows, chatbot/RAG) keep reading through their existing services; those
services now point at this canonical repository so behaviour does not change
even though the underlying storage is unified.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime

from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import relationship

from app.database import Base, _UUID, utcnow

# doc_type values (TYPE — independent from source).
DOC_TYPES = frozenset(
    {
        "date_sheet",            # date sheets / schedule notifications
        "model_paper",           # model question papers
        "official_notification", # official notifications/notices
        "other_official_document",
        "knowledge",             # anything knowledge-flavoured, not officially gated
        "needs_review",          # classification pending / ambiguous
    }
)

# source values (SOURCE/ORIGIN — not a type).
SOURCES = frozenset({"crawler", "manual_upload"})


class UniversityDocument(Base):
    """One canonical row per university document, regardless of origin."""

    __tablename__ = "university_documents"
    __table_args__ = (
        Index("ix_university_documents_doc_type", "doc_type"),
        Index("ix_university_documents_source", "source"),
        Index("ix_university_documents_status", "status"),
        Index("ix_university_documents_sha256", "sha256"),
    )

    id = Column(_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(500), nullable=False)
    # TYPE — independent of source.
    doc_type = Column(String(40), nullable=False)
    # SOURCE — where the document came from.
    source = Column(String(30), nullable=False)  # crawler | manual_upload

    # ----- File / storage reference -----
    file_path = Column(String(700), nullable=True)      # stored file location (outside public mount)
    original_filename = Column(String(400), nullable=True)
    file_type = Column(String(20), nullable=True)       # pdf | docx | xlsx | csv | txt | html
    file_size = Column(Integer, nullable=True)
    sha256 = Column(String(64), nullable=True)

    # ----- Provenance -----
    source_url = Column(String(1024), nullable=True)            # crawler: originating URL
    site_page_id = Column(String(36), nullable=True)            # crawler: website_pages.id
    upload_notice_id = Column(String(36), nullable=True)        # manual: university_notices.id
    legacy_source = Column(String(40), nullable=True)           # e.g. "notices" | "crawler"
    provenance = Column(JSON, nullable=True)                    # free-form {"crawled_at", "etag", ...}

    # ----- Programme / academic metadata -----
    programme_id = Column(String(60), nullable=True)
    programme_name = Column(String(200), nullable=True)
    stream = Column(String(60), nullable=True)
    semester = Column(String(10), nullable=True)
    batch = Column(String(20), nullable=True)
    academic_year = Column(String(20), nullable=True)

    # ----- Lifecycle -----
    # status: draft | pending_review | needs_review | verified | hidden_hold | published
    status = Column(String(30), default="draft", nullable=False)
    is_verified = Column(Boolean, default=False, nullable=False)
    is_published = Column(Boolean, default=False, nullable=False)
    published_at = Column(DateTime(timezone=True), nullable=True)
    verified_by = Column(String(200), nullable=True)
    verified_at = Column(DateTime(timezone=True), nullable=True)
    confidence = Column(JSON, nullable=True)                     # {"band", "score"}
    review_note = Column(Text, nullable=True)

    created_by = Column(String(200), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    def to_dict(self) -> dict:
        return {
            "id": str(self.id),
            "title": self.title,
            "doc_type": self.doc_type,
            "source": self.source,
            "file_path": self.file_path,
            "original_filename": self.original_filename,
            "file_type": self.file_type,
            "file_size": self.file_size,
            "sha256": self.sha256,
            "source_url": self.source_url,
            "site_page_id": self.site_page_id,
            "upload_notice_id": self.upload_notice_id,
            "legacy_source": self.legacy_source,
            "programme_id": self.programme_id,
            "programme_name": self.programme_name,
            "stream": self.stream,
            "semester": self.semester,
            "batch": self.batch,
            "academic_year": self.academic_year,
            "status": self.status,
            "is_verified": self.is_verified,
            "is_published": self.is_published,
            "published_at": self.published_at.isoformat() if self.published_at else None,
            "verified_by": self.verified_by,
            "verified_at": self.verified_at.isoformat() if self.verified_at else None,
            "confidence": self.confidence,
            "review_note": self.review_note,
            "created_by": self.created_by,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "deleted_at": self.deleted_at.isoformat() if self.deleted_at else None,
        }


def _json(value) -> list | None:
    if not value:
        return None
    try:
        data = json.loads(value)
        return data if isinstance(data, list) else None
    except (ValueError, TypeError):
        return None
