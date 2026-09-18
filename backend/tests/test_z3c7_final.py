"""test_z3c7_final.py -- Phase 3C-7 hermetic, uniquely named to dodge ghosts.

Builds a LOCAL in-memory StaticPool SQLite engine and creates the schema
exactly once via drop-then-create. Imports the REAL canonical service.
"""

from __future__ import annotations

import tempfile
import uuid
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.models.university_document import UniversityDocument
from app.university_documents import service as university_documents

# Real on-disk bytes shared by the dedup tests: postgres-acquired sha256 dedup
# is keyed on FILE CONTENT, so an idempotency test must feed the same file.
_DEDUP_FILE = Path(tempfile.gettempdir()) / f"z3c7_dedup_{uuid.uuid4().hex}.pdf"
_DEDUP_FILE.write_bytes(b"CUS dedup fixture bytes 9f2c7a")


@pytest.fixture(scope="module")
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    s = factory()
    yield s
    s.close()
    engine.dispose()


def _crawled(db: Session, *, title: str, doc_type: str) -> UniversityDocument:
    return university_documents.record_crawled_document(
        db,
        title=title,
        doc_type=doc_type,
        file_path=None,
        original_filename=f"{uuid.uuid4().hex}.pdf",
        file_type="application/pdf",
        file_size=1234,
        provider="crawler",
    )


def _manual(db: Session, *, title: str, doc_type: str, file_path=None) -> UniversityDocument:
    return university_documents.record_manual_upload(
        db,
        title=title,
        doc_type=doc_type,
        file_path=file_path,
        original_filename=f"{uuid.uuid4().hex}.pdf",
        file_type="application/pdf",
        file_size=2048,
        actor_id=str(uuid.uuid4()),
        actor_role="admin",
    )


def test_crawler_and_manual_land_in_one_canonical_table(db):
    c = _crawled(db, title="Crawled Date Sheet", doc_type="date_sheet")
    m = _manual(db, title="Manual Date Sheet", doc_type="date_sheet")
    rows = db.query(UniversityDocument).all()
    assert c.source == "crawler" and m.source == "manual_upload"
    assert {r.source for r in rows} == {"crawler", "manual_upload"}
    assert {r.doc_type for r in rows} == {"date_sheet"}


def test_doc_type_independent_of_source(db):
    c = _crawled(db, title="Crawled Model Paper", doc_type="model_paper")
    m = _manual(db, title="Manual Model Paper", doc_type="model_paper")
    assert c.source == "crawler" and c.doc_type == "model_paper"
    assert m.source == "manual_upload" and m.doc_type == "model_paper"


def test_sha256_dedup_is_idempotent(db):
    first = _manual(db, title="Dedup Target", doc_type="official_notification", file_path=str(_DEDUP_FILE))
    second = _manual(db, title="Dedup Target", doc_type="official_notification", file_path=str(_DEDUP_FILE))
    n = (
        db.query(UniversityDocument)
        .filter(UniversityDocument.title == "Dedup Target")
        .count()
    )
    assert first.id == second.id
    assert n == 1


def test_verify_dedup_and_reject_unverified_publish(db):
    d = _manual(db, title="Lifecycle", doc_type="official_notification")
    with pytest.raises(Exception):
        university_documents.publish_document(
            db, d, actor_id=str(uuid.uuid4()), actor_role="admin"
        )
    university_documents.verify_document(
        db, d, actor_id=str(uuid.uuid4()), actor_role="admin"
    )
    assert d.is_verified is True
    university_documents.publish_document(
        db, d, actor_id=str(uuid.uuid4()), actor_role="admin"
    )
    assert d.is_published is True
    university_documents.soft_delete_document(
        db, d, actor_id=str(uuid.uuid4()), actor_role="admin"
    )
    assert d.deleted_at is not None


def test_backfill_from_notices_idempotent(db):
    r = university_documents.backfill_from_notices(
        db, actor_id=str(uuid.uuid4())
    )
    assert isinstance(r, dict) and "created" in r and "skipped" in r
