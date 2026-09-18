"""
backend/tests/test_p3c8_admin_filter.py

Focused regression suite for the Admin -> University Documents category/filter
mismatch (Phase 3C-8 follow-up).

Contract under test:
  * The UI category "Model Question Papers" sends the canonical filter value
    ``model_paper`` (exact string, matching the backend enum).
  * The admin backend list endpoint filters the canonical repository
    ``UniversityDocument.doc_type`` verbatim.
  * Backfill of a WebsitePage whose ``category == "model-paper"`` lands in the
    canonical repo as ``doc_type == "model_paper"`` (NOT
    ``official_notification``).
  * The "Official Notifications" filter returns official_notification rows and
    never model-paper rows.
  * Date Sheets / Other Official Documents / Knowledge / Needs Review filters
    still return exactly their own category.
  * Manual reclassification to model_paper remains functional.

Run (from backend/):  python tests/test_p3c8_admin_filter.py
or:                    python -m pytest tests/test_p3c8_admin_filter.py -q
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

import hashlib

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402
from sqlalchemy import create_engine  # noqa: E402
from sqlalchemy.orm import Session, sessionmaker  # noqa: E402
from sqlalchemy.pool import StaticPool  # noqa: E402

from app.database import Base  # noqa: E402
from app.models.website_sync import WebsitePage  # noqa: E402
from app.university_documents import service as university_documents  # noqa: E402

CANONICAL_MODEL_PAPER = "model_paper"
UI_LABEL = "Model Question Papers"


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


def _page(db: Session, *, title: str, category: str) -> WebsitePage:
    p = WebsitePage(
        id=str(uuid.uuid4()),
        url=f"https://example.test/notices/{title}",
        base_url="https://example.test/notices",
        title=title,
        normalized_title=title.lower(),
        category=category,
        content_type="document",
        content="",
        content_hash=hashlib.sha256((title + category).encode("utf-8")).hexdigest(),
        http_status=200,
        version=1,
        status="new",
        doc_type="official",
        classification_confidence={"band": "high", "score": 100},
        classification_signals=[f"{category} matched"],
    )
    db.add(p)
    db.commit()
    return p


def _canonical_ids(db: Session) -> set[str]:
    return {str(d.site_page_id) for d in db.query(university_documents.UniversityDocument).all()}


def _seed_categories(db: Session, titles: dict[str, str]) -> None:
    for title, category in titles.items():
        _page(db, title=title, category=category)


def test_model_paper_filter_returns_model_paper_rows(db: Session):
    _seed_categories(db, {
        "AlgebraModelPaper.pdf": "model-paper",
        "ExamFeeCircular.pdf": "official-notification",
    })
    university_documents.backfill_from_notices(db, actor_id=str(uuid.uuid4()))
    rows = university_documents.list_documents(db, doc_type=CANONICAL_MODEL_PAPER)
    titles = [d.title for d in rows]
    assert "AlgebraModelPaper.pdf" in titles
    assert "ExamFeeCircular.pdf" not in titles
    assert all(d.doc_type == CANONICAL_MODEL_PAPER for d in rows)


def test_official_notification_filter_excludes_model_papers(db: Session):
    rows = university_documents.list_documents(db, doc_type="official_notification")
    titles = [d.title for d in rows]
    assert "ExamFeeCircular.pdf" in titles
    assert "AlgebraModelPaper.pdf" not in titles


def test_date_sheet_filter_still_works(db: Session):
    _page(db, title="MCA Date Sheet.pdf", category="date-sheet")
    university_documents.backfill_from_notices(db, actor_id=str(uuid.uuid4()))
    rows = university_documents.list_documents(db, doc_type="date_sheet")
    titles = [d.title for d in rows]
    assert "MCA Date Sheet.pdf" in titles
    assert all(d.doc_type == "date_sheet" for d in rows)


def test_other_official_document_filter_still_works(db: Session):
    _page(db, title="Statutes.pdf", category="other-official-document")
    university_documents.backfill_from_notices(db, actor_id=str(uuid.uuid4()))
    rows = university_documents.list_documents(db, doc_type="other_official_document")
    assert [d.title for d in rows] == ["Statutes.pdf"]


def test_knowledge_filter_still_works(db: Session):
    _page(db, title="Admission Guide", category="admissions")
    university_documents.backfill_from_notices(db, actor_id=str(uuid.uuid4()))
    rows = university_documents.list_documents(db, doc_type="knowledge")
    assert [d.title for d in rows] == ["Admission Guide"]


def test_needs_review_filter_still_works(db: Session):
    _page(db, title="Scanned-Unknown.pdf", category="ambiguous")
    university_documents.backfill_from_notices(db, actor_id=str(uuid.uuid4()))
    rows = university_documents.list_documents(db, doc_type="needs_review")
    assert [d.title for d in rows] == ["Scanned-Unknown.pdf"]


def test_ui_sends_canonical_model_paper_filter_value():
    ui_file = Path(__file__).resolve().parents[2] / "frontend" / "js" / "admin_university_documents.js"
    src = ui_file.read_text(encoding="utf-8")
    # The dropdown constant pairs the UI label with the canonical value...
    assert UI_LABEL in src
    assert f'{{ v: "{CANONICAL_MODEL_PAPER}",' in src.replace("\n", " ").replace("  ", " ")
    # ...and the list request sends that value verbatim as the backend param.
    assert 'doc_type=" + encodeURIComponent(STATE.cat)' in src.replace("\n", " ")


def test_manual_reclassify_to_model_paper_still_works(db: Session):
    d = university_documents.record_manual_upload(
        db,
        title="Manual Notification.pdf",
        doc_type="official_notification",
        file_path=None,
        original_filename="Manual Notification.pdf",
        file_type="pdf",
        file_size=1234,
        actor_id=str(uuid.uuid4()),
        actor_role="admin",
    )
    university_documents.reclassify_document(db, d, CANONICAL_MODEL_PAPER,
                                             actor_id=str(uuid.uuid4()), actor_role="admin")
    rows = university_documents.list_documents(db, doc_type=CANONICAL_MODEL_PAPER)
    assert any(x.title == "Manual Notification.pdf" for x in rows)


# ---------------------------------------------------------------------------
# Focused tests for the category-filter / filtered-total bug (stale pagination
# offset on category switch, and count/select filter consistency).
# ---------------------------------------------------------------------------
_ALL_CANONICAL = [
    "date_sheet",
    "model_paper",
    "official_notification",
    "other_official_document",
    "knowledge",
    "needs_review",
]


@pytest.fixture
def fresh_db():
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


def _doc(db: Session, title: str, doc_type: str, *, status: str = "published", source: str = "crawler"):
    d = university_documents.UniversityDocument(
        title=title,
        doc_type=doc_type,
        source=source,
        status=status,
    )
    db.add(d)
    db.commit()
    return d


def test_each_category_returns_only_matching_doc_type(fresh_db):
    for dt in _ALL_CANONICAL:
        for i in range(3):
            _doc(fresh_db, f"{dt}-{i}", dt)
    for dt in _ALL_CANONICAL:
        rows = university_documents.list_documents(fresh_db, doc_type=dt)
        assert rows, dt
        assert all(r.doc_type == dt for r in rows)


def test_each_category_total_equals_filtered_row_count(fresh_db):
    _doc(fresh_db, "ds1", "date_sheet")
    _doc(fresh_db, "ds2", "date_sheet")
    _doc(fresh_db, "mp1", "model_paper")
    for dt, expected in [("date_sheet", 2), ("model_paper", 1), ("knowledge", 0)]:
        assert university_documents.count_documents(fresh_db, doc_type=dt) == expected
        assert len(university_documents.list_documents(fresh_db, doc_type=dt)) == expected


def test_all_returns_every_document(fresh_db):
    for dt in _ALL_CANONICAL:
        _doc(fresh_db, f"{dt}-x", dt)
    total = university_documents.count_documents(fresh_db)
    rows = university_documents.list_documents(fresh_db, limit=200)
    assert total == len(_ALL_CANONICAL)
    assert len(rows) == total


def test_empty_category_returns_no_items_and_zero_total(fresh_db):
    _doc(fresh_db, "only-date-sheet", "date_sheet")
    assert university_documents.list_documents(fresh_db, doc_type="official_notification") == []
    assert university_documents.count_documents(fresh_db, doc_type="official_notification") == 0


def test_category_pagination_within_category(fresh_db):
    for i in range(7):
        _doc(fresh_db, f"mp-{i}", "model_paper")
    for i in range(5):
        _doc(fresh_db, f"ds-{i}", "date_sheet")
    page1 = university_documents.list_documents(fresh_db, doc_type="model_paper", limit=3, offset=0)
    page2 = university_documents.list_documents(fresh_db, doc_type="model_paper", limit=3, offset=3)
    page3 = university_documents.list_documents(fresh_db, doc_type="model_paper", limit=3, offset=6)
    assert len(page1) == 3 and len(page2) == 3 and len(page3) == 1
    assert all(r.doc_type == "model_paper" for r in page1 + page2 + page3)
    assert university_documents.count_documents(fresh_db, doc_type="model_paper") == 7


def test_category_page2_does_not_leak_other_categories(fresh_db):
    for i in range(4):
        _doc(fresh_db, f"kn-{i}", "knowledge")
    for i in range(4):
        _doc(fresh_db, f"nr-{i}", "needs_review")
    page2 = university_documents.list_documents(fresh_db, doc_type="knowledge", limit=2, offset=2)
    assert len(page2) == 2
    assert all(r.doc_type == "knowledge" for r in page2)


def test_combined_filters_total_is_intersection(fresh_db):
    _doc(fresh_db, "MCA Date Sheet", "date_sheet", source="crawler")
    _doc(fresh_db, "BCA Date Sheet", "date_sheet", source="crawler")
    _doc(fresh_db, "MCA Model Paper", "model_paper", source="crawler")
    _doc(fresh_db, "MCA Notification", "official_notification", source="manual_upload")
    assert university_documents.count_documents(fresh_db, doc_type="date_sheet", q="MCA") == 1
    assert university_documents.count_documents(fresh_db, doc_type="date_sheet", source="crawler") == 2
    assert university_documents.count_documents(fresh_db, doc_type="date_sheet", status="published") == 2
    assert university_documents.count_documents(
        fresh_db, doc_type="date_sheet", q="MCA", source="crawler"
    ) == 1
    items = university_documents.list_documents(fresh_db, doc_type="date_sheet", q="MCA")
    assert [d.title for d in items] == ["MCA Date Sheet"]


def test_ui_category_click_resets_pagination_offset():
    ui_file = Path(__file__).resolve().parents[2] / "frontend" / "js" / "admin_university_documents.js"
    src = ui_file.read_text(encoding="utf-8")
    flat = " ".join(src.split())
    # Switching category must reset the offset to page 1; otherwise a stale
    # "All page 2" offset is reused and the footer shows e.g. "0-25 of 6".
    assert "STATE.cat = this.dataset.v; STATE.offset = 0; render();" in flat


def _main() -> int:
    import app.database as database

    database.create_all()
    from app.database import SessionLocal

    db = SessionLocal()
    try:
        test_model_paper_filter_returns_model_paper_rows(db)
        test_official_notification_filter_excludes_model_papers(db)
        test_date_sheet_filter_still_works(db)
        test_other_official_document_filter_still_works(db)
        test_knowledge_filter_still_works(db)
        test_needs_review_filter_still_works(db)
        test_ui_sends_canonical_model_paper_filter_value()
        test_manual_reclassify_to_model_paper_still_works(db)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())