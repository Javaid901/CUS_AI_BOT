"""
backend/tests/test_document_categories.py - Phase 1 category organization.

Focused tests for the Sync Documents category organization update:
  * /stats returns by_category counts for every admin category chip
  * chip counts partition the review queue (each document appears once)
  * category chips are disjoint filter predicates over the same list endpoint
  * confidence band filter combines with category / status / search
  * verify keeps category + doc_type (model papers stay Model Question Papers;
    verify is NOT publication)
  * reprocess re-imposes the Model Paper review-hold invariant

Run:  python tests/test_document_categories.py   (or pytest tests/test_document_categories.py)
"""

from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Isolation FIRST: settings freeze at import time, before any app import.
os.environ.setdefault(
    "WEBSITE_SYNC_RAW_DIR",
    str(Path(os.environ.get("TEMP", tempfile.gettempdir())) / "_cus_phase1_raw"),
)

import app.models  # noqa: F401  (register tables before any session)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.auth.security import hash_password  # noqa: E402
from app.database import SessionLocal, create_all, utcnow  # noqa: E402
from app.main import app  # noqa: E402
from app.models import User  # noqa: E402
from app.models.website_sync import CrawlRun, WebsitePage  # noqa: E402

create_all()

PASS: list[str] = []
FAIL: list[str] = []

TOKENS: dict[str, str] = {}
_created_user_ids: list[str] = []
_page_ids: dict[str, str] = {}
_run_ids: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  PASS  {name}" + (f"  {detail}" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")


def _ensure_users() -> None:
    if TOKENS.get("admin"):
        return
    db = SessionLocal()
    admin_username = student_username = ""
    try:
        admin = User(
            id=uuid.uuid4(),
            username=f"__catg_admin_{uuid.uuid4().hex[:6]}",
            email=f"__catg_admin_{uuid.uuid4().hex[:6]}@test.local",
            hashed_password=hash_password("secret123"),
            role="superadmin",
            is_active=True,
        )
        db.add(admin)
        db.flush()
        admin_username = admin.username
        _created_user_ids.append(str(admin.id))
        student = User(
            id=uuid.uuid4(),
            username=f"__catg_student_{uuid.uuid4().hex[:6]}",
            email=f"__catg_student_{uuid.uuid4().hex[:6]}@test.local",
            hashed_password=hash_password("secret123"),
            role="student",
            is_active=True,
        )
        db.add(student)
        db.flush()
        student_username = student.username
        _created_user_ids.append(str(student.id))
        db.commit()
    finally:
        db.close()

    client = TestClient(app)
    r = client.post("/api/auth/login", data={"username": admin_username, "password": "secret123"})
    TOKENS["admin"] = f"Bearer {r.json()['access_token']}"
    r = client.post("/api/auth/login", data={"username": student_username, "password": "secret123"})
    TOKENS["student"] = f"Bearer {r.json()['access_token']}"


def _seed() -> None:
    db = SessionLocal()
    try:
        rows = [
            # (key, url, title, category, doc_type, band, score, status, content_type)
            ("date_sheet", "https://www.cusrinagar.edu.in/notices/UG_Date_Sheet_2026.pdf",
             "UG Date Sheet 2026", "date-sheet", "official", "high", 85, "pending_review", "document"),
            ("model_paper", "https://www.cusrinagar.edu.in/exams/Model_Paper_EVS.pdf",
             "Model Paper EVS", "model-paper", "official", "high", 90, "pending_review", "document"),
            ("notice", "https://www.cusrinagar.edu.in/notices/Notice_for_Submission_of_Examination_Forms.pdf",
             "Notice for Submission of Examination Forms", "official-notification", "official", "medium", 55, "pending_review", "document"),
            ("other", "https://www.cusrinagar.edu.in/docs/University_Regulations_2026.pdf",
             "University Regulations 2026", "other-official-document", "official", "high", 78, "verified", "document"),
            ("knowledge", "https://www.cusrinagar.edu.in/faculty/profiles",
             "Faculty Profile Cluster University Srinagar", "faculty", "knowledge", "medium", 60, "verified", "html"),
            ("ambiguous", "https://www.cusrinagar.edu.in/downloads/EVSSem1.pdf",
             "EVSSem1.pdf", "ambiguous", "ambiguous", "low", 30, "pending_review", "document"),
        ]
        for i, (key, url, title, category, doc_type, band, score, status, content_type) in enumerate(rows):
            page = WebsitePage(
                url=url,
                base_url=url.rsplit("/", 1)[0],
                title=title,
                category=category,
                content_type=content_type,
                doc_type=doc_type,
                classification_status=status,
                classification_confidence={"band": band, "score": score},
                classification_signals=[f"{category}: {title}"],
                doc_meta={"size_bytes": 100 + i, "sha256": f"{i:x}" * 64},
                content_hash=f"{i+5:x}" * 64,
                version=1,
                status="new",
                first_seen=utcnow(),
                last_synced=utcnow(),
            )
            db.add(page)
            db.flush()
            _page_ids[key] = page.id
        db.add(CrawlRun(
            trigger="manual", status="completed",
            base_url="https://www.cusrinagar.edu.in",
            started_at=utcnow(), finished_at=utcnow(),
            duration_seconds=1.0, total_urls=6, pages_found=6, new_pages=6,
        ))
        run = db.query(CrawlRun).order_by(CrawlRun.started_at.desc()).first()
        if run:
            _run_ids.append(str(run.id))
        db.commit()
    finally:
        db.close()


def _wipe() -> None:
    """Delete ONLY the rows this test created (never live dev data)."""
    db = SessionLocal()
    try:
        if _page_ids:
            db.query(WebsitePage).filter(WebsitePage.id.in_(list(_page_ids.values()))).delete(synchronize_session=False)
        if _run_ids:
            db.query(CrawlRun).filter(CrawlRun.id.in_(_run_ids)).delete(synchronize_session=False)
        if _created_user_ids:
            db.query(User).filter(User.id.in_(_created_user_ids)).delete(synchronize_session=False)
        db.commit()
        _page_ids.clear()
        _run_ids.clear()
        _created_user_ids.clear()
    finally:
        db.close()


def _list_where(client, A, **filters) -> list[str]:
    qs = "&".join(f"{k}={v}" for k, v in filters.items() if v is not None)
    r = client.get("/api/admin/sync-documents" + (("?" + qs) if qs else ""), headers=A)
    return [i["id"] for i in r.json().get("items", [])]


def test_category_organization() -> None:
    print("-- category organization (stats -> chips -> disjoint filters -> model-paper hold) --")
    del PASS[:]
    del FAIL[:]
    _ensure_users()
    _seed()
    client = TestClient(app)
    A = {"Authorization": TOKENS["admin"]}

    # stats.by_category
    r = client.get("/api/admin/sync-documents/stats", headers=A)
    st = r.json()
    check("stats 200", r.status_code == 200, str(r.status_code))
    bc = st.get("by_category", {})
    check("by_category present", isinstance(bc, dict), str(bc))
    check("by_category.date-sheet 1", bc.get("date-sheet") == 1, str(bc))
    check("by_category.model-paper 1", bc.get("model-paper") == 1, str(bc))
    check("by_category.official-notification 1", bc.get("official-notification") == 1, str(bc))
    check("by_category.other-official-document 1", bc.get("other-official-document") == 1, str(bc))
    check("by_category.knowledge 1", bc.get("knowledge") == 1, str(bc))
    check("by_category.ambiguous 1", bc.get("ambiguous") == 1, str(bc))
    check("by_category unknown key ignored", all(k in {"date-sheet", "model-paper", "official-notification", "other-official-document", "knowledge", "ambiguous"} for k in bc), str(sorted(bc)))
    check("by_category partitions queue", sum(bc.values()) == st.get("total_pages") == 6, f"{sum(bc.values())}/{st.get('total_pages')}")
    check("by_doc_type intact", st.get("by_doc_type", {}).get("official") == 4 and st.get("by_doc_type", {}).get("knowledge") == 1 and st.get("by_doc_type", {}).get("ambiguous") == 1, str(st.get("by_doc_type")))

    # chip predicates over the same list endpoint
    chip_ids = {}
    chip_ids["date-sheet"] = _list_where(client, A, category="date-sheet")
    chip_ids["model-paper"] = _list_where(client, A, category="model-paper")
    chip_ids["official-notification"] = _list_where(client, A, category="official-notification")
    chip_ids["other-official-document"] = _list_where(client, A, category="other-official-document")
    chip_ids["knowledge"] = _list_where(client, A, doc_type="knowledge")
    chip_ids["ambiguous"] = _list_where(client, A, doc_type="ambiguous")
    all_ids = _list_where(client, A)
    total_chip = sum(len(v) for v in chip_ids.values())
    check("each chip exactly 1", {k: len(v) for k, v in chip_ids.items()} == {k: 1 for k in chip_ids}, str({k: len(v) for k, v in chip_ids.items()}))
    seen: set[str] = set()
    overlap = False
    for k, v in chip_ids.items():
        if seen.intersection(v):
            overlap = True
        seen.update(v)
    check("chips disjoint (one document once)", not overlap, str(seen))
    check("chips cover the queue", total_chip == len(all_ids) == 6, f"{total_chip}/{len(all_ids)}")

    # combined filters
    comb = _list_where(client, A, category="model-paper", classification_status="pending_review")
    check("category+status combine", comb == [_page_ids["model_paper"]], str(comb))
    comb_none = _list_where(client, A, category="model-paper", classification_status="verified")
    check("category+status no false positive", comb_none == [], str(comb_none))

    r = client.get("/api/admin/sync-documents?confidence=high", headers=A)
    check("confidence=high filter", r.status_code == 200 and r.json()["total"] == 3, str(r.json().get("total")))
    r = client.get("/api/admin/sync-documents?confidence=medium", headers=A)
    check("confidence=medium filter", r.status_code == 200 and r.json()["total"] == 2, str(r.json().get("total")))
    r = client.get("/api/admin/sync-documents?confidence=low", headers=A)
    check("confidence=low filter", r.status_code == 200 and r.json()["total"] == 1, str(r.json().get("total")))
    r = client.get("/api/admin/sync-documents?confidence=high&category=other-official-document&q=Regulations", headers=A)
    check("category+confidence+q combine", r.status_code == 200 and r.json()["total"] == 1, str(r.json().get("total")))
    r = client.get("/api/admin/sync-documents?confidence=high&category=date-sheet&q=UG", headers=A)
    check("date-sheet high + q", r.status_code == 200 and r.json()["total"] == 1, str(r.json().get("total")))
    r = client.get("/api/admin/sync-documents?confidence=unknown", headers=A)
    check("unknown confidence is empty", r.status_code == 200 and r.json()["total"] == 0, str(r.json().get("total")))

    # Model Paper: verify is trust, NOT publication; category/doc_type intact.
    r = client.post(f"/api/admin/sync-documents/{_page_ids['model_paper']}/verify", json={"review_note": "confirmed model paper"}, headers=A)
    v = r.json()
    check("verify model-paper 200", r.status_code == 200, str(r.status_code))
    check("verify keeps category=model-paper", v.get("category") == "model-paper", f"{v.get('doc_type')}/{v.get('category')}/{v.get('classification_status')}")
    check("verify keeps doc_type=official", v.get("doc_type") == "official", str(v.get("doc_type")))
    check("verify sets verified", v.get("classification_status") == "verified", str(v.get("classification_status")))
    check("verify sets reviewed_by", bool(v.get("reviewed_by")), str(v.get("reviewed_by")))

    # Confirm the verified model paper is STILL a Model Question Paper in every surface.
    r = client.get("/api/admin/sync-documents?category=model-paper", headers=A)
    check("verified model paper still in model-paper chip", r.json()["total"] == 1, str(r.json().get("total")))

    # Reprocess re-imposes the review hold invariant.
    r = client.post(f"/api/admin/sync-documents/{_page_ids['model_paper']}/reprocess", headers=A)
    rp = r.json()
    check("reprocess model-paper 200", r.status_code == 200, str(r.status_code))
    check("reprocess keeps category=model-paper", rp.get("category") == "model-paper", f"{rp.get('category')}/{rp.get('classification_status')}")
    check("reprocess holds for review", rp.get("classification_status") == "pending_review", str(rp.get("classification_status")))
    check("reprocess clears stale reviewer", not rp.get("reviewed_by"), str(rp.get("reviewed_by")))

    # Untouched docs unaffected: date-sheet still there, stats still partition.
    r = client.get("/api/admin/sync-documents/stats", headers=A)
    bc2 = r.json().get("by_category", {})
    check("stats still partition after actions", sum(bc2.values()) == 6, str(bc2))

    _wipe()


def main() -> None:
    create_all()
    _ensure_users()
    test_category_organization()
    print("SUMMARY")
    print(f"  Passed: {len(PASS)}/{len(PASS) + len(FAIL)}")
    print(f"  Failed: {len(FAIL)}/{len(PASS) + len(FAIL)}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()