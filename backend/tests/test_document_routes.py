"""
backend/tests/test_document_routes.py - Phase 1 admin review endpoints.

  * 401 unauthenticated / 403 non-admin on every sync-documents route
  * GET list with filters + total
  * GET stats: by_doc_type / by_status / pending_review / last_run
  * GET detail: versions + has_raw / has_raw_on_disk
  * POST verify: sets verified + reviewer, allowlist category override,
    rejects unknown category (422), 404 for missing page
  * POST ambiguous: re-label + pending_review
  * POST hide: hidden_hold (raw bytes untouched)
  * POST reprocess: re-classifies + re-extracts (model paper stays held)

Run:  python tests/test_document_routes.py   (or pytest tests/test_document_routes.py)
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
            username=f"__drd_admin_{uuid.uuid4().hex[:6]}",
            email=f"__drd_admin_{uuid.uuid4().hex[:6]}@test.local",
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
            username=f"__drd_student_{uuid.uuid4().hex[:6]}",
            email=f"__drd_student_{uuid.uuid4().hex[:6]}@test.local",
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


def _seed() -> dict[str, str]:
    db = SessionLocal()
    ids: dict[str, str] = {}
    try:
        from app.knowledge_sync.raw_store import store_raw

        ds_raw = b"%PDF-1.4 UG date sheet"
        ds_info = store_raw(ds_raw, "pdf")

        m = WebsitePage(
            url="https://www.cusrinagar.edu.in/notices/UG_Date_Sheet_2026.pdf",
            base_url="https://www.cusrinagar.edu.in/notices",
            title="UG Date Sheet 2026",
            category="date-sheet",
            content_type="document",
            doc_type="official",
            classification_status="pending_review",
            classification_confidence={"band": "high", "score": 85},
            classification_signals=["date-sheet: Date Sheet"],
            doc_meta={"content_type": "pdf", "mime": "application/pdf", "size_bytes": len(ds_raw), "sha256": ds_info["sha256"]},
            raw_path=ds_info["rel_path"],
            raw_sha256=ds_info["sha256"],
            raw_size=ds_info["size"],
            content_hash="0" * 64,
            version=1,
            status="new",
            first_seen=utcnow(),
            last_synced=utcnow(),
        )
        db.add(m)
        db.flush()
        ids["date_sheet"] = m.id

        mp = WebsitePage(
            url="https://www.cusrinagar.edu.in/exams/Model_Paper_EVS.pdf",
            base_url="https://www.cusrinagar.edu.in/exams",
            title="Model Paper EVS",
            category="model-paper",
            content_type="document",
            doc_type="official",
            classification_status="pending_review",
            classification_confidence={"band": "high", "score": 90},
            classification_signals=["model-paper: Model Paper", "model paper: label + hold for review"],
            doc_meta={"content_type": "pdf", "mime": "application/pdf"},
            content_hash="1" * 64,
            version=1,
            status="new",
            first_seen=utcnow(),
            last_synced=utcnow(),
        )
        db.add(mp)
        db.flush()
        ids["model_paper"] = mp.id

        run = CrawlRun(
            trigger="manual",
            status="completed",
            base_url="https://www.cusrinagar.edu.in/notices",
            started_at=utcnow(),
            finished_at=utcnow(),
            duration_seconds=1.2,
            total_urls=5,
            pages_found=3,
            new_pages=3,
        )
        db.add(run)
        db.commit()
    finally:
        db.close()
    return ids


def _wipe() -> None:
    db = SessionLocal()
    try:
        for model in (CrawlRun, WebsitePage):
            db.query(model).delete()
        for uid in _created_user_ids:
            db.query(User).filter(User.id == uid).delete()
        db.commit()
    finally:
        db.close()


def test_routes() -> None:
    print("-- admin sync-documents routes --")
    del PASS[:]
    del FAIL[:]
    _ensure_users()
    ids = _seed()
    client = TestClient(app)

    # authorization
    r = client.get("/api/admin/sync-documents")
    check("unauthenticated rejected (401)", r.status_code in (401, 403), str(r.status_code))
    r = client.get("/api/admin/sync-documents", headers={"Authorization": TOKENS["student"]})
    check("student rejected (403)", r.status_code == 403, str(r.status_code))

    A = {"Authorization": TOKENS["admin"]}

    r = client.get("/api/admin/sync-documents", headers=A)
    data = r.json()
    check("list 200", r.status_code == 200, str(r.status_code))
    check("list total 2", data["total"] == 2, str(data["total"]))
    check("list items carry doc_type", all("doc_type" in i for i in data["items"]))

    r = client.get("/api/admin/sync-documents?doc_type=official&classification_status=pending_review", headers=A)
    check("filter ok", r.status_code == 200 and r.json()["total"] == 2, str(r.status_code))
    r = client.get("/api/admin/sync-documents?category=date-sheet", headers=A)
    check("category filter", r.status_code == 200 and r.json()["total"] == 1, str(r.json().get("total")))
    r = client.get("/api/admin/sync-documents?q=Model Paper", headers=A)
    check("q filter", r.status_code == 200 and r.json()["total"] == 1, str(r.json().get("total")))

    r = client.get("/api/admin/sync-documents/stats", headers=A)
    st = r.json()
    check("stats 200", r.status_code == 200, str(r.status_code))
    check("stats by_doc_type.official 2", st.get("by_doc_type", {}).get("official") == 2, str(st.get("by_doc_type")))
    check("stats by_status.pending_review 2", st.get("by_status", {}).get("pending_review") == 2, str(st.get("by_status")))
    check("stats pending_review 2", st.get("pending_review") == 2, str(st.get("pending_review")))
    check("stats last_run present", st.get("last_run") is not None and st["last_run"].get("status") == "completed", str(st.get("last_run")))

    # Stats-contract assertions: category chips must be DB-backed and GLOBAL.
    bc = st.get("by_category", {})
    check("stats by_category present", isinstance(bc, dict), str(bc))
    canonical = {"date-sheet", "model-paper", "official-notification", "other-official-document", "knowledge", "ambiguous"}
    check("by_category has 6 canonical keys", set(bc) == canonical, str(sorted(bc)))
    check("by_category sums to total", sum(bc.values()) == st.get("total_pages") == 2, f"{sum(bc.values())}/{st.get('total_pages')}")
    check("by_category.model-paper counts model-paper rows",
          bc.get("model-paper") == client.get("/api/admin/sync-documents?category=model-paper", headers=A).json().get("total") == 1,
          str(bc.get("model-paper")))
    check("zero categories are zero", bc.get("date-sheet") == 1 and bc.get("official-notification") == 0
          and bc.get("other-official-document") == 0 and bc.get("knowledge") == 0 and bc.get("ambiguous") == 0, str(bc))
    # Stats are independent of list filters: filtered lists return subsets while
    # stats keep the full dataset totals.
    filtered = client.get("/api/admin/sync-documents?classification_status=pending_review", headers=A).json()["total"]
    subset = client.get("/api/admin/sync-documents?q=UG_Date_Sheet", headers=A).json()["total"]
    check("stats global vs list filters", filtered == 2 and subset == 1
          and st.get("total_pages") == 2 and bc.get("model-paper") == 1, f"{filtered}/{subset}")
    check("filtered list independent of stats", subset == 1 and st.get("total_pages") == 2, f"{subset}/{st.get('total_pages')}")

    r = client.get(f"/api/admin/sync-documents/{ids['date_sheet']}", headers=A)
    detail = r.json()
    check("detail 200", r.status_code == 200, str(r.status_code))
    check("detail has versions array", isinstance(detail.get("versions"), list), str(type(detail.get("versions"))))
    check("detail has raw", detail.get("has_raw") is True and detail.get("has_raw_on_disk") is True, str(detail.get("has_raw_on_disk")))

    r = client.get(f"/api/admin/sync-documents/{uuid.uuid4()}", headers=A)
    check("missing page 404", r.status_code == 404, str(r.status_code))

    # verify
    r = client.post(f"/api/admin/sync-documents/{ids['date_sheet']}/verify", json={"review_note": "confirmed NEP sheet", "category": "date-sheet"}, headers=A)
    v = r.json()
    check("verify 200", r.status_code == 200, str(r.status_code))
    check("verify sets verified", v.get("classification_status") == "verified", v.get("classification_status"))
    check("verify records reviewer", bool(v.get("reviewed_by")), str(v.get("reviewed_by")))
    check("verify records note", v.get("review_note") == "confirmed NEP sheet", str(v.get("review_note")))
    check("verify review timestamp", v.get("reviewed_at") is not None)

    r = client.post(f"/api/admin/sync-documents/{ids['date_sheet']}/verify", json={"category": "not-a-category"}, headers=A)
    check("verify rejects unknown category (422)", r.status_code == 422, str(r.status_code))

    # ambiguous
    r = client.post(f"/api/admin/sync-documents/{ids['date_sheet']}/ambiguous", json={"review_note": "unclear"}, headers=A)
    a = r.json()
    check("ambiguous 200", r.status_code == 200, str(r.status_code))
    check("ambiguous label", a.get("doc_type") == "ambiguous" and a.get("category") == "ambiguous", f"{a.get('doc_type')}/{a.get('category')}")
    check("ambiguous pending_review", a.get("classification_status") == "pending_review", a.get("classification_status"))

    # hide (raw must remain untouched)
    r = client.post(f"/api/admin/sync-documents/{ids['date_sheet']}/hide", json={"review_note": "not relevant"}, headers=A)
    h = r.json()
    check("hide 200", r.status_code == 200, str(r.status_code))
    check("hide sets hidden_hold", h.get("classification_status") == "hidden_hold", h.get("classification_status"))
    check("hide keeps raw", h.get("has_raw") is True, str(h.get("has_raw")))

    # reprocess a model paper -> stays classified held-for-review
    r = client.post(f"/api/admin/sync-documents/{ids['model_paper']}/reprocess", headers=A)
    rp = r.json()
    check("reprocess 200", r.status_code == 200, str(r.status_code))
    check("reprocess keeps model-paper", rp.get("category") == "model-paper", str(rp.get("category")))
    check("reprocess holds for review", rp.get("classification_status") == "pending_review", rp.get("classification_status"))
    check("reprocess refreshed signals", isinstance(rp.get("classification_signals"), list) and len(rp.get("classification_signals", [])) >= 1, str(rp.get("classification_signals")))

    _wipe()


def main() -> None:
    create_all()
    _ensure_users()
    test_routes()
    print("SUMMARY")
    print(f"  Passed: {len(PASS)}/{len(PASS) + len(FAIL)}")
    print(f"  Failed: {len(FAIL)}/{len(PASS) + len(FAIL)}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()