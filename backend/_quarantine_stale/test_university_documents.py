"""
backend/tests/test_university_documents.py -- Phase 3C-7 focused suite.

Canonical single-table verification. Uses a HERMETIC in-memory SQLite engine
(StaticPool) so it never touches the real application DB and is idempotent:
the table is created exactly once inside the fixture.

Contract under test:
    crawler rows AND manual-upload rows land in ONE table (university_documents)
    with doc_type fully independent of origin (source carries crawler | manual_upload).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.university_document import UniversityDocument
from app.university_documents import service as university_documents


@pytest.fixture(scope="module")
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    s = factory()
    yield s
    s.close()
    engine.dispose()


def _rec_crawled(s: Session, *, title: str, doc_type: str) -> UniversityDocument:
    return university_documents.record_crawled_document(
        s,
        title=title,
        doc_type=doc_type,
        file_path=None,
        original_filename=f"{uuid.uuid4().hex}.pdf",
        file_type="application/pdf",
        file_size=1234,
        provider="system",
    )


def _rec_manual(s: Session, *, title: str, doc_type: str) -> UniversityDocument:
    return university_documents.record_manual_upload(
        s,
        title=title,
        doc_type=doc_type,
        file_path=None,
        original_filename=f"{uuid.uuid4().hex}.pdf",
        file_type="application/pdf",
        file_size=2048,
        actor_id=str(uuid.uuid4()),
        actor_role="admin",
    )


def test_crawler_and_manual_land_in_one_table(db_session):
    c = _rec_crawled(db_session, title="Crawled Date Sheet", doc_type="date_sheet")
    m = _rec_manual(db_session, title="Manual Date Sheet", doc_type="date_sheet")
    n = db_session.query(UniversityDocument).count()
    assert n >= 2
    assert {r.source for r in db_session.query(UniversityDocument).all()} == {
        "crawler",
        "manual_upload",
    }
    assert c.doc_type == "date_sheet" and m.doc_type == "date_sheet"


def test_doc_type_independent_of_source(db_session):
    c = _rec_crawled(db_session, title="Crawled Model Paper", doc_type="model_paper")
    m = _rec_manual(db_session, title="Manual Model Paper", doc_type="model_paper")
    assert c.source == "crawler" and c.doc_type == "model_paper"
    assert m.source == "manual_upload" and m.doc_type == "model_paper"


def test_sha256_dedup_is_idempotent(db_session):
    first = _rec_manual(
        db_session, title="Dedup Target", doc_type="official_notification"
    )
    second = _rec_manual(
        db_session, title="Dedup Target", doc_type="official_notification"
    )
    rows = (
        db_session.query(UniversityDocument)
        .filter(UniversityDocument.sha256 == first.sha256)
        .all()
    )
    assert len(rows) == 1

    assert university_documents.verify_document(
        db_session, first, actor_id=str(uuid.uuid4()), actor_role="admin"
    ).is_verified is True
    published = university_documents.publish_document(
        db_session, first, actor_id=str(uuid.uuid4()), actor_role="admin"
    )
    assert published.is_published is True and published.status == "published"

    with pytest.raises(HTTPException):
        # Publishing an unverified doc must be rejected by the service itself.
        unver = _rec_manual(
            db_session, title="Unverified", doc_type="official_notification"
        )
        university_documents.verify_document(
            db_session, unver, actor_id=str(uuid.uuid4()), actor_role="admin"
        )
        unver.is_verified = False
        …
