"""backend/tests/test_ud3c7_hermetic.py -- Phase 3C-7 focused (hermetic, in-memory).

Canonical single-table verification for Phase 3C-7. Uses a throwaway
in-memory SQLite engine + StaticPool so it NEVER touches the real app DB.
Both the crawler rows ("crawler") and the manual-upload rows ("manual_upload")
must land in the SAME table (university_documents), with doc_type independent
of source. sha256 fingerprinting makes re-recording idempotent.
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

# Real on-disk bytes shared by the dedup test: sha256 dedup is keyed on FILE
# CONTENT, so an idempotency test must feed the same file both times.
_DEDUP_FILE = Path(tempfile.gettempdir()) / f"ud3c7_dedup_{uuid.uuid4().hex}.pdf"
_DEDUP_FILE.write_bytes(b"CUS dedup fixture bytes ud3c7")


@pytest.fixture(scope="module")
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    try:
        Base.metadata.drop_all(engine)
    except Exception:
        pass
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False, future=True)
    s = factory()
    yield s
    s.close()
    engine.dispose()


def _rec_crawled(s: Session, *, title: str, doc_type: str):
    return university_documents.record_crawled_document(
        s,
        title=title,
        doc_type=doc_type,
        file_path=str(Path(tempfile.gettempdir()) / f"{uuid.uuid4().hex}.pdf"),
        original_filename=f"{uuid.uuid4().hex}.pdf",
        file_type="application/pdf",
        file_size=1234,
        provider="system",
    )


def _rec_manual(s: Session, *, title: str, doc_type: str, file_path=None):
    return university_documents.record_manual_upload(
        s,
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
    c = _rec_crawled(db, title="Crawled Date Sheet", doc_type="date_sheet")
    m = _rec_manual(db, title="Manual Date Sheet", doc_type="date_sheet")
    rows = db.query(UniversityDocument).all()
    assert c.source == "crawler" and c.doc_type == "date_sheet"
    assert m.source == "manual_upload" and m.doc_type == "date_sheet"
    assert {r.source for r in rows} == {"crawler", "manual_upload"}
    assert {r.doc_type for r in rows} == {"date_sheet"}


def test_doc_type_independent_of_source(db):
    c = _rec_crawled(db, title="Crawled Model Paper", doc_type="model_paper")
    m = _rec_manual(db, title="Manual Model Paper", doc_type="model_paper")
    assert c.source == "crawler" and c.doc_type == "model_paper"
    assert m.source == "manual_upload" and m.doc_type == "model_paper"


def test_sha256_dedup_is_idempotent(db):
    first = _rec_manual(db, title="Dedup Target", doc_type="official_notification", file_path=str(_DEDUP_FILE))
    second = _rec_manual(db, title="Dedup Target", doc_type="official_notification", file_path=str(_DEDUP_FILE))
    same = db.query(UniversityDocument).filter(UniversityDocument.title == "Dedup Target").count()
    assert same == 1


def test_admin_lifecycle_transitions(db):
    d = _rec_manual(db, title="Lifecycle Doc", doc_type="official_notification")
    with pytest.raises(Exception):
        university_documents.publish_document(db, d, actor_id=str(uuid.uuid4()), actor_role="admin")
    university_documents.verify_document(db, d, actor_id=str(uuid.uuid4()), actor_role="admin")
    assert d.is_verified is True
    university_documents.publish_document(db, d, actor_id=str(uuid.uuid4()), actor_role="admin")
    assert d.is_published is True
    university_documents.unpublish_document(db, d, actor_id=str(uuid.uuid4()), actor_role="admin")
    assert d.is_published is False
    university_documents.hide_document(db, d, note="on hold", actor_id=str(uuid.uuid4()), actor_role="admin")
    assert d.status.startswith("hidden")
    university_documents.restore_document(db, d, note="back", actor_id=str(uuid.uuid4()), actor_role="admin")
    assert d.deleted_at is None or d.status != "hidden_hold"
    university_documents.soft_delete_document(db, d, actor_id=str(uuid.uuid4()), actor_role="admin")
    assert d.deleted_at is not None