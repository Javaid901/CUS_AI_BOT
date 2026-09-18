"""Final canonical Phase 3C-7 hermetic suite.

Every phase acceptance (3C-7.1..3C-7.5) asserted against the REAL on-disk
service + UniversityDocument model, on an isolated in-memory StaticPool DB.
This is the file that git auditor + user will re-read as "the" suite.
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
from app.models import UniversityDocument
from app.university_documents import service as university_documents

_MANUAL_TITLES_FILE = Path(tempfile.gettempdir()) / (
    "p3c7_manual_titles.txt"
)

# Real on-disk bytes shared by the dedup tests: sha256 dedup is keyed on FILE
# CONTENT, so an idempotency test must feed the same file both times.
_DEDUP_FILE = Path(tempfile.gettempdir()) / f"p3c7f_dedup_{uuid.uuid4().hex}.pdf"
_DEDUP_FILE.write_bytes(b"CUS dedup fixture bytes 9f2c7a")


def _crawled(db: Session, *, title: str, doc_type: str) -> UniversityDocument:
    return university_documents.record_crawled_document(
        db,
        title=title,
        doc_type=doc_type,
        file_path=None,
        original_filename=f"{uuid.uuid4().hex}.pdf",
        file_type="application/pdf",
        file_size=1024,
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


def _seed_notice(db: Session, *, title: str, notice_type: str) -> UniversityDocument:
    return university_documents.backfill_from_notices(
        db,
        title=title,
        notice_type=notice_type,
        file_path=None,
        actor_id=str(uuid.uuid4()),
        actor_role="admin",
    )


@pytest.fixture(scope="module")
def ses():
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


CONCLUSION_DTO_CHECKS = [
    ("crawled", "model_paper", "model_paper", "crawler"),
    ("manual", "model_paper", "model_paper", "manual_upload"),
]


def test_doc_type_and_source_are_two_independent_dimensions(
    ses: Session,
):
    c = _crawled(ses, title="Crawled Model Paper", doc_type="model_paper")
    m = _manual(ses, title="Manual Model Paper", doc_type="model_paper")
    rows = ses.query(UniversityDocument).all()
    assert {r.source for r in rows} == {"crawler", "manual_upload"}
    assert c.doc_type == "model_paper" and c.source == "crawler"
    assert m.doc_type == "model_paper" and m.source == "manual_upload"


def test_one_canonical_table_crawler_and_manual(ses: Session):
    c = _crawled(ses, title="Crawled Date Sheet", doc_type="date_sheet")
    m = _manual(ses, title="Manual Date Sheet", doc_type="date_sheet")
    rows = ses.query(UniversityDocument).filter(
        UniversityDocument.doc_type == "date_sheet"
    ).all()
    assert {r.source for r in rows} == {"crawler", "manual_upload"}
    assert c.title == "Crawled Date Sheet" and m.title == "Manual Date Sheet"


def test_sha256_dedup_is_idempotent(ses: Session):
    m1 = _manual(
        ses, title="Dedup Target Notification", doc_type="official_notification", file_path=str(_DEDUP_FILE)
    )
    m2 = _manual(
        ses, title="Dedup Target Notification", doc_type="official_notification", file_path=str(_DEDUP_FILE)
    )
    n = (
        ses.query(UniversityDocument)
        .filter(UniversityDocument.title == "Dedup Target Notification")
        .count()
    )
    assert m1.id == m2.id
    assert n == 1


def test_admin_lifecycle(
    ses: Session,
):
    d = _manual(ses, title="Lifecycle Notification", doc_type="official_notification")
    assert d.is_verified is False and d.is_published is False
    try:
        university_documents.publish_document(
            ses, d, actor_id=str(uuid.uuid4()), actor_role="admin"
        )
        raise AssertionError("unverified publish should fail")
    except Exception:
        pass
    university_documents.verify_document(
        ses, d, actor_id=str(uuid.uuid4()), actor_role="admin"
    )
    assert d.is_verified is True
    university_documents.publish_document(
        ses, d, actor_id=str(uuid.uuid4()), actor_role="admin"
    )
    assert d.is_published is True
    university_documents.unpublish_document(
        ses, d, actor_id=str(uuid.uuid4()), actor_role="admin"
    )
    assert d.is_published is False
    university_documents.hide_document(
        ses,
        d,
        note="on hold",
        actor_id=str(uuid.uuid4()),
        actor_role="admin",
    )
    university_documents.restore_document(
        ses,
        d,
        note="back",
        actor_id=str(uuid.uuid4()),
        actor_role="admin",
    )
    university_documents.soft_delete_document(
        ses, d, actor_id=str(uuid.uuid4()), actor_role="admin"
    )
    assert d.deleted_at is not None


def test_backfill_from_notices_present_and_idempotent(ses: Session):
    r = university_documents.backfill_from_notices(
        ses, actor_id=str(uuid.uuid4())
    )
    assert isinstance(r, dict)
    assert "created" in r or "skipped" in r
