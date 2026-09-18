"""test_p3c8_classification.py -- Phase 3C-8 content-based document
classification + canonical knowledge ingestion.

Content-first classification acceptance for the Website Sync pipeline:

  1. binary documents are classified primarily from EXTRACTED TEXT
     (model-paper structure, date-sheet tables), with filename/URL only
     secondary hints;
  2. the canonical repository (university_documents) receives the content-
     derived doc_type + confidence + explainable signals;
  3. backfill maps the crawler's fine category (not the coarse Phase 1
     doc_type) so date sheets / model papers never collapse into
     official_notification;
  4. reclassification only ever touches rows still awaiting review
     (manual/verified rows win).

Hermetic: pure classifier tests need no DB; service tests use an in-memory
StaticPool SQLite engine.

Run:  python tests/test_p3c8_classification.py  (or pytest tests/test_p3c8_classification.py)
"""

from __future__ import annotations

import hashlib
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  PASS  {name}" + (f"  {detail}" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")


PDF = b"%PDF-1.4 fake document bytes"

# A realistic model/question paper body: question envelope details + 5
# enumerated questions. Filename and URL are silent about the type.
MODEL_QP_BODY = (
    "UNIVERSITY OF DISTANCE EDUCATION\n"
    "MCA 3rd Semester Examination 2026\n"
    "Paper Code: MCA-304\n"
    "MAXIMUM MARKS: 60\n"
    "TIME ALLOWED: 3 hours\n"
    "Instructions to candidates: This paper contains five questions. "
    "Attempt all questions, each question carries equal marks.\n"
    "\n"
    "Q1. Define operating system and its types.\n"
    "Q2. Explain process scheduling algorithms.\n"
    "Q3. What is a deadlock? Describe necessary conditions.\n"
    "Q4. Describe memory management techniques.\n"
    "Q5. Write short notes on virtual memory and paging.\n"
)

# A content-only date sheet: day + time rows, opaque filename.
DATE_TABLE_BODY = (
    "OFFICE OF THE CONTROLLER OF EXAMINATIONS\n"
    "CLUSTER UNIVERSITY SRINAGAR\n"
    "Date Sheet for MCA 3rd Semester Regular Batch 2025\n"
    "Session 2026 Examination Time: 10:30 AM\n"
    "\n"
    "Mon  12-Jan-2026  10:30 AM\n"
    "Tue  13-Jan-2026  10:30 AM\n"
    "Wed  14-Jan-2026  01:00 PM\n"
    "Thu  15-Jan-2026  01:00 PM\n"
)

# Date sheet also has numbered rows (would trip naive question-line counters).
DATE_TABLE_NUMBERED = (
    "OFFICE OF THE CONTROLLER OF EXAMINATIONS\n"
    "Date Sheet for PG 1st Semester Regular Batch 2025\n"
    "Session 2026\n"
    "1. Statistics        Mon 12-Jan-2026 10:30 AM\n"
    "2. Data Structures   Tue 13-Jan-2026 10:30 AM\n"
    "3. Algorithms        Wed 14-Jan-2026 01:00 PM\n"
    "4. DBMS              Thu 15-Jan-2026 01:00 PM\n"
)

PREVIOUS_YEAR_QP_BODY = (
    "MCA Previous Year Question Paper 2025\n"
    "MAXIMUM MARKS: 60\n"
    "TIME ALLOWED: 3 hours\n"
    "Q1. Define operating system.\n"
    "Q2. Explain process scheduling.\n"
    "Q3. What is a deadlock?\n"
    "Q4. Describe memory management.\n"
    "Q5. Write short notes on virtual memory.\n"
)


def _doc(**kw) -> dict:
    base = {"content_type": "document", "raw": PDF}
    base.update(kw)
    return base


# ---------------------------------------------------------------------------
# Part A — pure content-first classifier
# ---------------------------------------------------------------------------

def test_content_structural_model_paper() -> None:
    print("-- content-only model/question paper (opaque filename, /notices URL) --")
    from app.knowledge_sync.document_classifier import classify_document

    r = classify_document(
        title="pgp2026sem1.pdf",
        url="https://www.cusrinagar.edu.in/notices/pgp2026sem1.pdf",
        text=MODEL_QP_BODY,
        **_doc(),
    )
    check("structural model paper -> official", r["doc_type"] == "official", str(r))
    check("structural model paper -> model-paper", r["category"] == "model-paper", str(r))
    check("structural model paper signal", any("question" in s for s in r["signals"]), str(r["signals"]))
    check("structural model paper confidence >= medium", r["confidence"]["band"] in ("medium", "high"), str(r["confidence"]))


def test_content_phrase_model_paper_notice_name() -> None:
    print("-- 'Model Question Paper' phrase in content beats /notices URL --")
    from app.knowledge_sync.document_classifier import classify_document

    r = classify_document(
        title="notice_mca2ndsem.pdf",
        url="https://www.cusrinagar.edu.in/notices/notice_mca2ndsem.pdf",
        text=("OFFICE OF THE CONTROLLER OF EXAMINATIONS\n"
              "Model Question Paper for MCA 2nd Semester\n"
              "MAXIMUM MARKS: 60  TIME ALLOWED: 3 hours\n"
              "Instructions to candidates: attempt all questions.\n"
              "Q1. Explain data structures.\nQ2. Write about DBMS.\n"
              "Q3. Define networking.\nQ4. Describe OS concepts.\n"),
        **_doc(),
    )
    check("phrase model paper -> model-paper", r["category"] == "model-paper", str(r))
    check("phrase model paper not notification", r["category"] != "official-notification", str(r))


def test_previous_year_structure_never_model_paper() -> None:
    print("-- structured previous-year paper stays OUT of model-paper --")
    from app.knowledge_sync.document_classifier import classify_document

    r = classify_document(
        title="MCA Previous Year Question Paper.pdf",
        url="https://www.cusrinagar.edu.in/notices/MCA_Previous_Year_Question_Paper.pdf",
        text=PREVIOUS_YEAR_QP_BODY,
        **_doc(),
    )
    check("previous-year NOT model-paper", r["category"] != "model-paper", str(r))
    check("previous-year still classified (not crash)", r["doc_type"] in ("official", "ambiguous"), str(r))


def test_content_date_table_no_title_signal() -> None:
    print("-- content-only date table with opaque filename --")
    from app.knowledge_sync.document_classifier import classify_document

    r = classify_document(
        title="pg3rdsem2026.pdf",
        url="https://www.cusrinagar.edu.in/downloads/pg3rdsem2026.pdf",
        text=DATE_TABLE_BODY,
        **_doc(),
    )
    check("date table -> official", r["doc_type"] == "official", str(r))
    check("date table -> date-sheet", r["category"] == "date-sheet", str(r))


def test_numbered_date_sheet_not_model_paper() -> None:
    print("-- numbered date sheet rows must NOT flip to model-paper --")
    from app.knowledge_sync.document_classifier import classify_document

    r = classify_document(
        title="pg1stsemregularbatch2025.pdf",
        url="https://www.cusrinagar.edu.in/FolderManager/Downloads/pg1stsemregularbatch2025.pdf",
        text=DATE_TABLE_NUMBERED,
        **_doc(),
    )
    check("numbered date sheet -> date-sheet", r["category"] == "date-sheet", str(r))
    check("numbered date sheet NOT model-paper", r["category"] != "model-paper", str(r))


def test_empty_unparseable_binary_ambiguous() -> None:
    print("-- unparseable/empty binary text -> ambiguous (needs_review) --")
    from app.knowledge_sync.document_classifier import classify_document

    r = classify_document(title="scan2026.pdf", url="https://x/d/scan2026.pdf", text="", **_doc())
    check("empty text -> ambiguous", r["doc_type"] == "ambiguous" and r["category"] == "ambiguous", str(r))


def test_html_knowledge_preserved() -> None:
    print("-- HTML pages keep knowledge classification --")
    from app.knowledge_sync.document_classifier import classify_document

    r = classify_document(
        title="Admissions 2026", url="https://www.cusrinagar.edu.in/admissions",
        text="PG admission notification body", content_type="html",
    )
    check("html -> knowledge", r["doc_type"] == "knowledge" and r["category"] == "admissions", str(r))


def test_canonical_doc_type_mapping() -> None:
    print("-- canonical doc_type mapping --")
    from app.knowledge_sync.document_classifier import canonical_doc_type_for, canonical_doc_type

    cases = [
        ({"doc_type": "official", "category": "date-sheet"}, "date_sheet"),
        ({"doc_type": "official", "category": "model-paper"}, "model_paper"),
        ({"doc_type": "official", "category": "official-notification"}, "official_notification"),
        ({"doc_type": "official", "category": "other-official-document"}, "other_official_document"),
        ({"doc_type": "knowledge", "category": "admissions"}, "knowledge"),
        ({"doc_type": "ambiguous", "category": "ambiguous"}, "needs_review"),
    ]
    for classification, expected in cases:
        check(
            f"canonical {classification.get('category')} -> {expected}",
            canonical_doc_type_for(classification) == expected,
            canonical_doc_type_for(classification),
        )
    check("canonical_doc_type('date-sheet')", canonical_doc_type("date-sheet") == "date_sheet")
    check("canonical_doc_type('ambiguous')", canonical_doc_type("ambiguous") == "needs_review")
    check("canonical_doc_type('admissions') -> knowledge", canonical_doc_type("admissions") == "knowledge")


# ---------------------------------------------------------------------------
# Part B — hermetic DB (StaticPool SQLite)
# ---------------------------------------------------------------------------

def _db():
    import pytest
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from app.database import Base
    import app.models  # noqa: F401  (register all tables)

    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    return factory()


def _page(db, *, title, url, category, doc_type, content, content_type="document",
          confidence=None):
    from app.models.website_sync import WebsitePage

    p = WebsitePage(
        id=str(uuid.uuid4()),
        url=url,
        base_url=url.rsplit("/", 1)[0],
        title=title,
        normalized_title=title.lower(),
        category=category,
        content_type=content_type,
        content=content,
        content_hash=hashlib.sha256(content.encode("utf-8")).hexdigest(),
        http_status=200,
        version=1,
        status="new",
        doc_type=doc_type,
        classification_confidence=confidence
        or {"band": "high", "score": 90},
        classification_signals=[f"{category} matched"],
    )
    db.add(p)
    db.commit()
    return p


def _notice(db, *, title, notice_type, sha=None):
    from app.models import UniversityNotice

    n = UniversityNotice(
        id=str(uuid.uuid4()),
        title=title,
        notice_type=notice_type,
        sha256=sha or hashlib.sha256((title + str(uuid.uuid4())).encode()).hexdigest(),
        is_verified=True,
        is_published=False,
    )
    db.add(n)
    db.commit()
    return n


def test_backfill_maps_fine_category_to_canonical() -> None:
    print("-- backfill: WebsitePage category (not coarse doc_type) drives canonical --")
    from app.models import UniversityDocument
    from app.university_documents import service as s

    db = _db()
    try:
        _page(db, title="MCA Model Paper", url="https://x/notices/m.pdf",
              category="model-paper", doc_type="official", content=MODEL_QP_BODY)
        _page(db, title="PG Date Sheet", url="https://x/d/ds.pdf",
              category="date-sheet", doc_type="official", content=DATE_TABLE_BODY)
        _page(db, title="Admissions Page", url="https://x/admissions",
              category="admissions", doc_type="knowledge", content="Admissions 2026", content_type="html")
        _page(db, title="Unknown scan", url="https://x/d/scan.pdf",
              category="ambiguous", doc_type="ambiguous", content="")
        r = s.backfill_from_notices(db, actor_id=str(uuid.uuid4()))
        rows = db.query(UniversityDocument).all()
        types = {row.doc_type for row in rows}
        check("backfill created 4", r["created"] == 4, str(r))
        check("model_paper present", "model_paper" in types, str(types))
        check("date_sheet present", "date_sheet" in types, str(types))
        check("knowledge present", "knowledge" in types, str(types))
        check("needs_review present", "needs_review" in types, str(types))
        check("no false official_notification", "official_notification" not in types, str(types))
        mp = next(x for x in rows if x.doc_type == "model_paper")
        check("confidence recorded", (mp.confidence or {}).get("band") in ("medium", "high"), str(mp.confidence))
        check("provenance.signals recorded", isinstance((mp.provenance or {}).get("signals"), list), str(mp.provenance))
        r2 = s.backfill_from_notices(db, actor_id=str(uuid.uuid4()))
        check("backfill idempotent", r2["created"] == 0, str(r2))
    finally:
        db.close()


def test_record_crawled_sha256_dedup_with_provenance() -> None:
    print("-- record_crawled_document: explicit sha256 idempotency + metadata --")
    from app.models import UniversityDocument
    from app.university_documents import service as s

    db = _db()
    try:
        sha = hashlib.sha256(MODEL_QP_BODY.encode()).hexdigest()
        d1 = s.record_crawled_document(
            db, title="MCA Model Paper", doc_type="model_paper",
            sha256=sha, source_url="https://x/notices/m.pdf",
            site_page_id="page-1", confidence={"band": "high", "score": 90},
            provenance={"signals": ["model-paper content structure: 5 enumerated questions"]},
        )
        d2 = s.record_crawled_document(
            db, title="MCA Model Paper", doc_type="model_paper",
            sha256=sha, source_url="https://x/notices/m.pdf", site_page_id="page-1",
        )
        check("sha256 dedup returns same row", str(d1.id) == str(d2.id), f"{d1.id} vs {d2.id}")
        n = db.query(UniversityDocument).count()
        check("single canonical row", n == 1, str(n))
        row = db.query(UniversityDocument).first()
        check("source_url stored", row.source_url == "https://x/notices/m.pdf", str(row.source_url))
        check("site_page_id stored", row.site_page_id == "page-1", str(row.site_page_id))
        check("provenance stored", (row.provenance or {}).get("signals"), str(row.provenance))
        check("confidence stored", (row.confidence or {}).get("band") == "high", str(row.confidence))
    finally:
        db.close()


def _seed_reclassify(db):
    from app.university_documents import service as s

    p = _page(db, title="StatePaper", url="https://x/notices/c.pdf",
              category="official-notification", doc_type="official",
              content=MODEL_QP_BODY)
    _page(db, title="Admissions Page", url="https://x/admissions",
          category="admissions", doc_type="knowledge", content="Admissions body", content_type="html")
    s.backfill_from_notices(db, actor_id=str(uuid.uuid4()))
    return p


def test_reclassify_documents_from_website() -> None:
    print("-- safe auto-reclassification of pending_review crawler rows --")
    from app.models import UniversityDocument
    from app.university_documents import service as s

    db = _db()
    try:
        p = _seed_reclassify(db)
        # The seeded 'official-notification' row now carries content that is a
        # structural question paper -> auto-correct to model_paper.
        target = (
            db.query(UniversityDocument)
            .filter(UniversityDocument.site_page_id == p.id)
            .first()
        )
        check("seed row is official_notification", target.doc_type == "official_notification", target.doc_type)

        report = s.reclassify_documents_from_website(db, actor_id=str(uuid.uuid4()), dry_run=True)
        check("dry_run reports change", report["changed"] == 1, str(report["corrected"]))
        check("dry_run leaves doc_type", target.doc_type == "official_notification", target.doc_type)

        report = s.reclassify_documents_from_website(db, actor_id=str(uuid.uuid4()), dry_run=False)
        check("reclassify corrected 1", report["changed"] == 1, str(report["corrected"]))
        db.refresh(target)
        check("doc_type corrected", target.doc_type == "model_paper", target.doc_type)
        check("confidence refreshed", (target.confidence or {}).get("band") in ("medium", "high"), str(target.confidence))
        check("signals refreshed", any("question" in s_ for s_ in (target.provenance or {}).get("signals", [])), str(target.provenance))
        db.refresh(target)
        knowledge_row = (
            db.query(UniversityDocument)
            .filter(UniversityDocument.doc_type == "knowledge")
            .first()
        )
        check("knowledge row untouched", knowledge_row is not None, "")
        # Idempotent: nothing left to change.
        report2 = s.reclassify_documents_from_website(db, actor_id=str(uuid.uuid4()))
        check("second pass no changes", report2["changed"] == 0, str(report2))
    finally:
        db.close()


def test_reclassify_never_touches_verified_rows() -> None:
    print("-- reclassification respects manual/verified rows --")
    from app.models import UniversityDocument
    from app.university_documents import service as s

    db = _db()
    try:
        p = _page(db, title="VerifiedNotice", url="https://x/notices/v.pdf",
                  category="official-notification", doc_type="official",
                  content=MODEL_QP_BODY)
        s.backfill_from_notices(db, actor_id=str(uuid.uuid4()))
        row = (
            db.query(UniversityDocument)
            .filter(UniversityDocument.site_page_id == p.id)
            .first()
        )
        s.verify_document(db, row, actor_id=str(uuid.uuid4()), actor_role="admin")
        db.refresh(row)
        report = s.reclassify_documents_from_website(db, actor_id=str(uuid.uuid4()), dry_run=False)
        db.refresh(row)
        check("verified row skipped", report["changed"] == 0, str(report))
        check("verified row doc_type unchanged", row.doc_type == "official_notification", row.doc_type)
        check("verified row still verified", row.is_verified is True and row.status == "verified", row.status)
    finally:
        db.close()


def test_crawler_sync_publishes_canonical_row() -> None:
    print("-- web_engine integration: _apply_document_attributes -> canonical --")
    from sqlalchemy.orm import sessionmaker

    from app.knowledge_sync.document_classifier import classify_document
    from app.knowledge_sync.web_engine import WebsiteSyncEngine
    from app.models import UniversityDocument

    db = _db()
    try:
        classification = classify_document(
            title="pgp2026sem1.pdf",
            url="https://x/notices/pgp2026sem1.pdf",
            text=MODEL_QP_BODY,
            content_type="document",
            raw=PDF,
        )
        from app.knowledge_sync.web_crawler import CrawlResult

        result = CrawlResult("https://x/notices/pgp2026sem1.pdf")
        result.kind = "document"
        result.ok = True
        result.title = "pgp2026sem1.pdf"
        result.text = MODEL_QP_BODY
        result.raw = PDF
        result.http_status = 200

        from app.models.website_sync import WebsitePage

        page = WebsitePage(
            id=str(uuid.uuid4()),
            url=result.url,
            base_url="https://x",
            title=result.title,
            normalized_title=result.title,
            content_type="document",
            content=MODEL_QP_BODY,
            content_hash=hashlib.sha256(MODEL_QP_BODY.encode()).hexdigest(),
            http_status=200,
            version=1,
            status="new",
        )
        sha = hashlib.sha256(PDF).hexdigest()
        engine = WebsiteSyncEngine(db, base_url="https://x", index_rag=False)
        engine._apply_document_attributes(
            page, result, classification,
            meta={"page_count": 1},
            raw_info=("raw/abc.pdf", sha, len(PDF)),
        )
        row = (
            db.query(UniversityDocument)
            .filter(UniversityDocument.sha256 == sha)
            .first()
        )
        check("canonical row created from crawl", row is not None, f"sha={sha}")
        if row:
            check("canonical doc_type = model_paper", row.doc_type == "model_paper", row.doc_type)
            check("canonical source = crawler", row.source == "crawler", row.source)
            check("canonical site_page_id linked", row.site_page_id == page.id, str(row.site_page_id))
            check("canonical confidence stored", (row.confidence or {}).get("band"), str(row.confidence))
            check("canonical provenance.signals", (row.provenance or {}).get("signals"), str(row.provenance))
        # Idempotency across a re-crawl of the same bytes.
        engine._apply_document_attributes(
            page, result, classification,
            meta={"page_count": 1},
            raw_info=("raw/abc.pdf", sha, len(PDF)),
        )
        n = db.query(UniversityDocument).filter(UniversityDocument.sha256 == sha).count()
        check("re-crawl does not duplicate canonical row", n == 1, str(n))
    finally:
        db.close()


def main() -> None:
    test_content_structural_model_paper()
    test_content_phrase_model_paper_notice_name()
    test_previous_year_structure_never_model_paper()
    test_content_date_table_no_title_signal()
    test_numbered_date_sheet_not_model_paper()
    test_empty_unparseable_binary_ambiguous()
    test_html_knowledge_preserved()
    test_canonical_doc_type_mapping()
    test_backfill_maps_fine_category_to_canonical()
    test_record_crawled_sha256_dedup_with_provenance()
    test_reclassify_documents_from_website()
    test_reclassify_never_touches_verified_rows()
    test_crawler_sync_publishes_canonical_row()
    print()
    print("SUMMARY")
    print(f"  Passed: {len(PASS)}/{len(PASS) + len(FAIL)}")
    print(f"  Failed: {len(FAIL)}/{len(PASS) + len(FAIL)}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()