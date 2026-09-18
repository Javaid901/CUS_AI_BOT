"""
P6+ — Student access to Official Notifications & Other Official Documents.

Contract under test:
  1. Planner Rule 3ab routes official-notification / official-document lookups
     to the ``official_documents`` action, while bare notices / circulars keep
     their existing news / navigation flows.
  2. Generic requests search BOTH ``official_notification`` and
     ``other_official_document``; "other official documents" narrows.
  3. The engine handler serves ONLY verified + published official rows: a date
     sheet, an unverified row, an unpublished row and a soft-deleted row never
     leak, and a card never exposes the stored filesystem path.
  4. The public file endpoint resolves a published document with containment and
     refuses unpublished / escaping / foreign paths with 404.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base
from app.knowledge_sync.raw_store import raw_root
from app.models.university_document import UniversityDocument
from app.orchestrator.context import ConversationContext
from app.orchestrator.engine import _handle_official_documents
from app.orchestrator.extractor import extract_entities
from app.orchestrator.planner import plan
from app.orchestrator.state import ConversationState
from app.university_documents import service as ud
from app.university_documents.routes import (
    _download_filename,
    _media_type,
    public_get_document_file,
)

GENERIC_INTENT = {
    "doc_types": ["official_notification", "other_official_document"],
    "programme": None,
    "q": None,
}


@pytest.fixture()
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


def _route(raw: str):
    return plan(raw, ConversationContext(), "ud-" + uuid.uuid4().hex[:8], extract_entities(raw))


def _doc(
    db,
    title: str,
    doc_type: str,
    *,
    published: bool = True,
    verified: bool = True,
    deleted: bool = False,
    file_path: str | None = None,
    programme_id: str | None = None,
    published_at: datetime | None = None,
) -> UniversityDocument:
    d = UniversityDocument(
        id=uuid.uuid4(),
        title=title,
        doc_type=doc_type,
        source="crawler",
        file_path=file_path,
        original_filename=None if file_path else f"{title}.pdf",
        file_type="pdf",
        programme_id=programme_id,
        status="published" if published else ("verified" if verified else "pending_review"),
        is_verified=verified,
        is_published=published,
        published_at=published_at if published else None,
        deleted_at=datetime(2026, 1, 1) if deleted else None,
    )
    db.add(d)
    db.commit()
    return d


def _drain(db, intent):
    async def _run():
        return [
            ev
            async for ev in _handle_official_documents(
                db, "ud-chat", ConversationState(chat_id="ud-chat"), dict(intent)
            )
        ]

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# 1. Planner routing (Rule 3ab)
# ---------------------------------------------------------------------------

_REQUIRED = [
    "show official notifications",
    "show official notices",
    "latest university notifications",
    "latest official documents",
    "show other official documents",
    "download official notification",
    "open official notice",
    "show notification about exam forms",
    "find the BCA notification",
    "latest BCA notifications",
    "any official notice related to BCA",
    "list official documents",
    "latest notifications",
    "official documents for MCA",
    "show BCA official notifications",
]


def test_planner_routes_all_required_queries():
    for raw in _REQUIRED:
        p = _route(raw)
        assert p.action == "official_documents", f"{raw!r} -> {p.action}"
        assert p.extra.get("doc_types")


def test_planner_generic_searches_both_and_other_narrows():
    p = _route("show official notifications")
    assert set(p.extra["doc_types"]) == {"official_notification", "other_official_document"}

    p = _route("show other official documents")
    assert p.extra["doc_types"] == ["other_official_document"]


def test_planner_extracts_programme():
    assert _route("find the BCA notification").extra["programme"] == "bca"
    assert _route("official documents for MCA").extra["programme"] == "mca"
    assert _route("latest BCA notifications").extra["programme"] == "bca"


def test_planner_control_flows_untouched():
    assert _route("latest notices").action == "news"
    assert _route("notices").action == "navigation"
    assert _route("circular for holidays").action == "news"
    assert _route("latest admission notice").action == "news"
    assert _route("holiday notice").action == "news"
    assert _route("bca 4th sem date sheet").action == "university_notices"
    assert _route("model papers for MCA").action == "examination"


# ---------------------------------------------------------------------------
# 2. Engine handler — published + verified gate
# ---------------------------------------------------------------------------

def test_handler_serves_only_published_verified_official_rows(db):
    _doc(db, "Admission Notification 2026", "official_notification",
         published_at=datetime(2026, 9, 1))
    _doc(db, "Revised Statutes", "other_official_document",
         published_at=datetime(2026, 9, 2))
    _doc(db, "Unverified Notification", "official_notification", published=False, verified=False)
    _doc(db, "Verified Not Published", "official_notification", published=False, verified=True)
    _doc(db, "Deleted Notification", "official_notification", deleted=True,
         published_at=datetime(2026, 9, 3))
    _doc(db, "BCA Date Sheet", "date_sheet", published_at=datetime(2026, 9, 4))

    events = _drain(db, GENERIC_INTENT)
    assert [e["type"] for e in events] == ["official_document_list", "done"]
    docs = events[0]["documents"]
    titles = {d["title"] for d in docs}
    assert titles == {"Admission Notification 2026", "Revised Statutes"}
    assert all(d["file_url"].startswith("/api/university-documents/") for d in docs)
    assert all("file_path" not in d for d in docs)
    assert all(d["doc_type"] != "date_sheet" for d in docs)


def test_handler_empty_is_honest(db):
    events = _drain(db, GENERIC_INTENT)
    assert [e["type"] for e in events] == ["official_document_list", "done"]
    assert events[0]["documents"] == []
    assert "I don't have information available" in events[0]["message"]


def test_handler_other_family_narrows(db):
    _doc(db, "Admission Notification 2026", "official_notification",
         published_at=datetime(2026, 9, 1))
    _doc(db, "Revised Statutes", "other_official_document",
         published_at=datetime(2026, 9, 2))

    events = _drain(db, {**GENERIC_INTENT, "doc_types": ["other_official_document"]})
    titles = {d["title"] for d in events[0]["documents"]}
    assert titles == {"Revised Statutes"}


# ---------------------------------------------------------------------------
# 3. Service-level programme soft match
# ---------------------------------------------------------------------------

def test_programme_filter_is_soft_on_agnostic_rows(db):
    _doc(db, "BCA Notification", "official_notification", programme_id="bca",
         published_at=datetime(2026, 9, 1))
    _doc(db, "Institutional Statutes", "other_official_document", programme_id=None,
         published_at=datetime(2026, 9, 2))

    bca = {d.title for d in ud.list_published_documents(db, programme="bca")}
    assert bca == {"BCA Notification", "Institutional Statutes"}

    mca = {d.title for d in ud.list_published_documents(db, programme="mca")}
    assert mca == {"Institutional Statutes"}


# ---------------------------------------------------------------------------
# 4. File resolution + public endpoint
# ---------------------------------------------------------------------------

def test_file_resolver_and_public_endpoint(db, tmp_path):
    root = raw_root()
    name = f"{uuid.uuid4().hex}.pdf"
    stored = root / name
    stored.write_bytes(b"%PDF-1.4 official document test")

    published = _doc(db, "Served Notification", "official_notification",
                     file_path=name, published_at=datetime(2026, 9, 1))
    resolved = ud.resolve_document_file(published)
    assert resolved == stored.resolve()

    resp = public_get_document_file(str(published.id), download=False, db=db)
    assert resp.status_code == 200
    assert Path(resp.path) == stored.resolve()

    unpublished = _doc(db, "Hidden Notification", "official_notification",
                       published=False, file_path=name)
    with pytest.raises(HTTPException) as exc:
        public_get_document_file(str(unpublished.id), download=False, db=db)
    assert exc.value.status_code == 404
    with pytest.raises(HTTPException):
        ud.resolve_document_file(unpublished)


def test_media_type_and_download_filename():
    pdf = SimpleNamespace(file_path="abc.pdf", original_filename="abc.pdf",
                          title="EnglishSem-1.pdf", file_type="document")
    assert _media_type(pdf) == "application/pdf"
    assert _download_filename(pdf) == "EnglishSem-1.pdf"

    docx = SimpleNamespace(file_path="x.docx", original_filename=None,
                           title="Scheme", file_type="document")
    assert _media_type(docx) == (
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    )
    assert _download_filename(docx) == "Scheme.docx"

    unknown = SimpleNamespace(file_path="blob", original_filename=None,
                              title="Mystery", file_type="document")
    assert _media_type(unknown) == "application/octet-stream"
    assert _download_filename(unknown) == "Mystery"


def test_file_resolver_refuses_escapes_and_foreign_paths(db, tmp_path):
    escaping = _doc(db, "Escape", "official_notification", file_path="../escape.pdf",
                    published_at=datetime(2026, 9, 1))
    with pytest.raises(HTTPException) as exc:
        ud.resolve_document_file(escaping)
    assert exc.value.status_code == 404

    foreign_file = tmp_path / "foreign.pdf"
    foreign_file.write_bytes(b"%PDF-1.4 foreign")
    foreign = _doc(db, "Foreign", "official_notification", file_path=str(foreign_file),
                   published_at=datetime(2026, 9, 1))
    with pytest.raises(HTTPException) as exc:
        ud.resolve_document_file(foreign)
    assert exc.value.status_code == 404
