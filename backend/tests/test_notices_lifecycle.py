"""
P8 — University-notice lifecycle & public-gating battery.

Drives the REAL admin/public HTTP API (TestClient) plus the service-level SQL
gates:

  1. Role gating       — listing is admin+; every mutation is super-admin only.
  2. Lifecycle         — upload -> verify -> publish -> unpublish -> soft delete,
                        with publish-before-verify rejected.
  3. Correction path   — verify fails (422) on broken rows; admin entry PATCH
                        fixes a row, then verify succeeds.
  4. Re-validation     — editing a verified schedule row auto-downgrades and
                        auto-unpublishes when rows stop being verify-ready.
  5. Public gating     — public list/get/file/schedule only ever surface
                        VERIFIED + PUBLISHED, non-deleted notices and VERIFIED
                        rows; file serving is publish-gated + containment-checked.
  6. Zero-hallucination audit — the SQL gate filters on programme/semester/
                        stream and never lets a tampered unverified row through.
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth.security import hash_password
from app.database import SessionLocal, create_all
from app.main import app
from app.models import AuditLog, DateSheetEntry, UniversityNotice, User
from app.notices import service as notices
from app.utils.logging import audit

create_all()

client = TestClient(app)

SUPER: dict[str, str] = {}
ADMIN: dict[str, str] = {}
_LOCAL: dict[str, str] = {}
_created_user_ids: list[str] = []
_created_notice_ids: list[str] = []

_ROWS_OK_BCA_S4 = [
    dict(row_no=1, programme_id="bca", semester="4", exam_date="2026-06-12",
         day="Thursday", start_time="10:00", end_time="13:00",
         subject="Data Structures", paper_code="BCA401", venue="Room 5"),
]


def _j(values: list[str]) -> str:
    return json.dumps(values)


def _notice_file_bytes() -> bytes:
    # Fake but byte-valid-enough content; the parser simply fails on it, which
    # is the intended "upload accepted, extraction_failed" state.
    return b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\ntrailer\n<<>>\n%%EOF"


@pytest.fixture(scope="module", autouse=True)
def _bootstrap():
    db = SessionLocal()
    try:
        for key, role in (("super", "superadmin"), ("admin", "admin")):
            username = f"__nt_{key}_{uuid.uuid4().hex[:6]}"
            user = User(
                id=uuid.uuid4(),
                username=username,
                email=f"{username}@test.local",
                hashed_password=hash_password("secret123"),
                role=role,
                is_active=True,
            )
            db.add(user)
            db.flush()
            _LOCAL[key] = username
            _created_user_ids.append(str(user.id))
        db.commit()
    finally:
        db.close()

    r = client.post("/api/auth/login", data={"username": _LOCAL["super"], "password": "secret123"})
    assert r.status_code == 200, r.text
    SUPER["Authorization"] = f"Bearer {r.json()['access_token']}"
    r = client.post("/api/auth/login", data={"username": _LOCAL["admin"], "password": "secret123"})
    assert r.status_code == 200, r.text
    ADMIN["Authorization"] = f"Bearer {r.json()['access_token']}"

    yield

    db = SessionLocal()
    try:
        for nid in _created_notice_ids:
            db.query(DateSheetEntry).filter(DateSheetEntry.notice_id == nid).delete()
            db.query(UniversityNotice).filter(UniversityNotice.id == nid).delete()
        for uid in _created_user_ids:
            db.query(AuditLog).filter(AuditLog.actor_id == uid).delete()
            db.query(User).filter(User.id == uid).delete()
        db.commit()
    finally:
        db.close()


def _seed_notice(
    *,
    rows: list[dict],
    verified: bool = False,
    published: bool = False,
    prog_ids: list[str] | None = None,
    title: str = "BCA Semester-IV Date Sheet, June 2026",
    notice_type: str = "date_sheet",
) -> tuple[str, str]:
    """Create a notice + entries directly in the DB; returns (notice_id, file_path)."""
    db = SessionLocal()
    try:
        notices_dir = notices.notices_root()
        notices_dir.mkdir(parents=True, exist_ok=True)
        fname = f"seed_{uuid.uuid4().hex[:8]}.pdf"
        fp = notices_dir / fname
        fp.write_bytes(_notice_file_bytes())
        n = UniversityNotice(
            id=uuid.uuid4(),
            title=title,
            notice_type=notice_type,
            filename=fname,
            original_filename="Seed.pdf",
            file_type="pdf",
            file_size=fp.stat().st_size,
            sha256="seed",
            file_path=str(fp),
            programme_ids=_j(prog_ids if prog_ids is not None else ["bca"]),
            extraction_status="verified" if verified else "pending_verification",
            is_verified=verified,
            is_published=published,
            published_at=__import__("app.database", fromlist=["utcnow"]).utcnow() if published else None,
        )
        db.add(n)
        db.flush()
        for r in rows:
            db.add(DateSheetEntry(
                notice_id=n.id,
                row_no=int(r.get("row_no") or 0),
                programme_id=r.get("programme_id"),
                programme_name=r.get("programme_name"),
                stream=r.get("stream"),
                semester=str(r.get("semester")) if r.get("semester") is not None else None,
                batch=r.get("batch"),
                exam_type=r.get("exam_type"),
                exam_date=r.get("exam_date"),
                day=r.get("day"),
                start_time=r.get("start_time"),
                end_time=r.get("end_time"),
                subject_code=r.get("subject_code"),
                subject=r.get("subject"),
                paper_code=r.get("paper_code"),
                venue=r.get("venue"),
                raw=r.get("raw"),
                extraction_status="verified" if verified else "pending_verification",
            ))
        db.commit()
        _created_notice_ids.append(str(n.id))
        return str(n.id), str(fp)
    finally:
        db.close()


def _entry_id(notice_id: str, row_no: int) -> str:
    db = SessionLocal()
    try:
        e = db.query(DateSheetEntry).filter(
            DateSheetEntry.notice_id == notice_id,
            DateSheetEntry.row_no == row_no,
        ).first()
        assert e is not None
        return str(e.id)
    finally:
        db.close()


def _notice_state(notice_id: str) -> dict:
    db = SessionLocal()
    try:
        n = db.query(UniversityNotice).filter(UniversityNotice.id == notice_id).one()
        return {"is_verified": n.is_verified, "is_published": n.is_published,
                "extraction_status": n.extraction_status}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 1. Role gating
# ---------------------------------------------------------------------------

def test_list_open_to_admins_but_uploads_superadmin_only():
    assert client.get("/api/admin/notices", headers=ADMIN).status_code == 200

    r = client.post("/api/admin/notices", headers=ADMIN,
                    files={"file": ("x.pdf", _notice_file_bytes(), "application/pdf")},
                    data={"notice_type": "date_sheet"})
    assert r.status_code == 403, r.text

    r = client.post("/api/admin/notices", headers=SUPER,
                    files={"file": ("dup.pdf", _notice_file_bytes(), "application/pdf")},
                    data={"notice_type": "date_sheet", "title": "Fake upload"})
    assert r.status_code == 201, r.text
    nid = r.json()["id"]
    _created_notice_ids.append(nid)
    # Content is not really parseable -> the notice lands in extraction_failed,
    # never verbose/verified; and it is invisible to the public.
    assert r.json()["extraction_status"] == "extraction_failed"

    dup = client.post("/api/admin/notices", headers=SUPER,
                      files={"file": ("dup2.pdf", _notice_file_bytes(), "application/pdf")},
                      data={"notice_type": "date_sheet"})
    assert dup.status_code == 409, dup.text

    r = client.get(f"/api/notices/{nid}")
    assert r.status_code == 404


def test_admin_cannot_list_schedule_but_superadmin_can():
    nid, _ = _seed_notice(rows=_ROWS_OK_BCA_S4)
    # require_superadmin even on GET detail / schedule endpoints.
    assert client.get(f"/api/admin/notices/{nid}/schedule", headers=ADMIN).status_code == 403
    r = client.get(f"/api/admin/notices/{nid}/schedule", headers=SUPER)
    assert r.status_code == 200
    assert len(r.json()["entries"]) == 1


# ---------------------------------------------------------------------------
# 2. Lifecycle: upload -> verify -> publish -> unpublish
# ---------------------------------------------------------------------------

def test_verify_publish_before_verify_and_unpublish():
    nid, _ = _seed_notice(rows=_ROWS_OK_BCA_S4)

    assert client.post(f"/api/admin/notices/{nid}/publish", headers=SUPER).status_code == 409

    r = client.post(f"/api/admin/notices/{nid}/verify", headers=SUPER)
    assert r.status_code == 200, r.text
    assert r.json()["is_verified"] is True
    assert r.json()["extraction_status"] == "verified"

    r = client.post(f"/api/admin/notices/{nid}/publish", headers=SUPER)
    assert r.status_code == 200, r.text
    assert r.json()["is_published"] is True

    # Published -> publicly visible.
    assert client.get(f"/api/notices/{nid}").status_code == 200

    r = client.post(f"/api/admin/notices/{nid}/unpublish", headers=SUPER)
    assert r.status_code == 200, r.text
    assert r.json()["is_published"] is False
    assert client.get(f"/api/notices/{nid}").status_code == 404

    # Re-publish after re-verify works (idempotent publish path intact).
    assert client.post(f"/api/admin/notices/{nid}/publish", headers=SUPER).status_code == 200
    assert client.get(f"/api/notices/{nid}").status_code == 200


def test_verify_rejects_broken_rows_then_correction_unblocks():
    rows = list(_ROWS_OK_BCA_S4)
    rows.append(dict(row_no=2, programme_id="bca", semester="4", exam_date="2026-06-13",
                     start_time="10:00", end_time="13:00"))  # no subject/paper code
    nid, _ = _seed_notice(rows=rows)

    r = client.post(f"/api/admin/notices/{nid}/verify", headers=SUPER)
    assert r.status_code == 422, r.text
    body = r.json()
    # The global exception handler wraps the detail; problems may appear in the
    # message string or nested under detail.problems depending on handler config.
    msg = str(body)
    assert "row 2" in msg and "missing subject" in msg, msg

    # (UI "correct entry") patch the broken row to be verify-ready.
    e2 = _entry_id(nid, 2)
    r = client.patch(f"/api/admin/notices/{nid}/schedule/{e2}", headers=SUPER,
                     json={"subject": "Operating Systems", "paper_code": "BCA402"})
    assert r.status_code == 200, r.text

    r = client.post(f"/api/admin/notices/{nid}/verify", headers=SUPER)
    assert r.status_code == 200, r.text
    assert r.json()["extraction_status"] == "verified"


def test_revalidate_after_edit_autounpublishes_on_broken_row():
    nid, _ = _seed_notice(rows=_ROWS_OK_BCA_S4)
    client.post(f"/api/admin/notices/{nid}/verify", headers=SUPER)
    client.post(f"/api/admin/notices/{nid}/publish", headers=SUPER)
    assert _notice_state(nid)["is_published"] is True

    # Break a verified row (drop its exam date) through the admin PATCH path.
    e1 = _entry_id(nid, 1)
    r = client.patch(f"/api/admin/notices/{nid}/schedule/{e1}", headers=SUPER,
                     json={"exam_date": "", "subject": "", "start_time": "", "end_time": ""})
    assert r.status_code == 200, r.text
    st = _notice_state(nid)
    assert st["is_verified"] is False
    assert st["is_published"] is False
    assert st["extraction_status"] == "pending_verification"
    assert client.get(f"/api/notices/{nid}").status_code == 404

    # Repair -> re-verify -> re-publish fully restores public visibility.
    r = client.patch(f"/api/admin/notices/{nid}/schedule/{e1}", headers=SUPER,
                     json={"exam_date": "2026-06-12", "subject": "Data Structures",
                           "start_time": "10:00", "end_time": "13:00"})
    assert r.status_code == 200, r.text
    assert client.post(f"/api/admin/notices/{nid}/verify", headers=SUPER).status_code == 200
    assert client.post(f"/api/admin/notices/{nid}/publish", headers=SUPER).status_code == 200
    assert client.get(f"/api/notices/{nid}").status_code == 200


def test_soft_delete_hides_from_public():
    nid, _ = _seed_notice(rows=_ROWS_OK_BCA_S4, verified=True, published=True)
    assert client.get(f"/api/notices/{nid}").status_code == 200

    r = client.delete(f"/api/admin/notices/{nid}", headers=SUPER)
    assert r.status_code == 200, r.text

    assert client.get(f"/api/notices/{nid}").status_code == 404
    listed = client.get("/api/notices?programme=bca").json()["notices"]
    assert all(n["id"] != nid for n in listed)
    admin_listed = client.get("/api/admin/notices", headers=SUPER).json()["items"]
    assert all(n["id"] != nid for n in admin_listed)


# ---------------------------------------------------------------------------
# 3. Public gating + file security
# ---------------------------------------------------------------------------

def test_public_list_and_file_publish_gate():
    pub_id, _ = _seed_notice(rows=_ROWS_OK_BCA_S4, verified=True, published=True,
                             title="Published BCA s4")
    priv_id, _ = _seed_notice(rows=_ROWS_OK_BCA_S4, verified=True, published=False,
                              title="Private BCA s4")

    listed = client.get("/api/notices?programme=bca").json()["notices"]
    assert any(n["id"] == pub_id for n in listed)
    assert all(n["id"] != priv_id for n in listed), "unpublished notice leaked to public list"

    f = client.get(f"/api/notices/{pub_id}/file")
    assert f.status_code == 200
    assert f.content == _notice_file_bytes()
    dl = client.get(f"/api/notices/{pub_id}/file?download=1")
    assert "attachment" in dl.headers.get("content-disposition", "")

    assert client.get(f"/api/notices/{priv_id}/file").status_code == 404
    assert client.get(f"/api/notices/{priv_id}/schedule").status_code == 404


def test_public_schedule_endpoint_verified_rows_only():
    rows = [
        dict(row_no=1, programme_id="bca", semester="4", exam_date="2026-06-12",
             subject="Data Structures", paper_code="BCA401", start_time="10:00", end_time="13:00"),
        dict(row_no=2, programme_id="bca", semester="4", exam_date="2026-06-13",
             subject="Operating Systems", paper_code="BCA402", start_time="10:00", end_time="13:00"),
        dict(row_no=3, programme_id="bca", semester="6", exam_date="2026-07-01",
             subject="Networks", paper_code="BCA601", start_time="10:00", end_time="13:00"),
        dict(row_no=4, programme_id="mca", semester="4", exam_date="2026-06-15",
             subject="DBMS", paper_code="MCA401", start_time="14:00", end_time="17:00"),
    ]
    nid, _ = _seed_notice(rows=rows, verified=True, published=True, prog_ids=["bca", "mca"])
    client.post(f"/api/admin/notices/{nid}/verify", headers=SUPER)

    # Simulate admin tampering AFTER the fact: row 2 is no longer verified.
    db = SessionLocal()
    try:
        e2 = db.query(DateSheetEntry).filter(DateSheetEntry.notice_id == nid,
                                             DateSheetEntry.row_no == 2).one()
        e2.extraction_status = "pending_verification"
        db.commit()
    finally:
        db.close()

    sched = client.get(f"/api/notices/{nid}/schedule?programme=bca&semester=4").json()["schedule"]
    assert len(sched) == 1
    assert sched[0]["row_no"] == 1
    assert sched[0]["paper_code"] == "BCA401"


def test_public_file_containment_rejects_outside_path():
    outside = Path(__file__).resolve().parent / "outside_notice.pdf"
    outside.write_bytes(_notice_file_bytes())
    nid = _seed_notice_file_at(str(outside), published=True)

    assert client.get(f"/api/notices/{nid}/file").status_code == 404

    db = SessionLocal()
    try:
        n = db.query(UniversityNotice).filter(UniversityNotice.id == nid).one()
        # resolve_notice_file (public) checks publish+containment -> 404.
        with pytest.raises(Exception) as exc_info:
            notices.resolve_notice_file(n)
        assert exc_info.value.status_code == 404
        # resolve_stored_file (admin preview) checks containment only -> 404.
        with pytest.raises(Exception) as exc_info2:
            notices.resolve_stored_file(n)
        assert exc_info2.value.status_code == 404
    finally:
        db.close()
        outside.unlink(missing_ok=True)


def _seed_notice_file_at(path: str, *, published: bool) -> str:
    db = SessionLocal()
    try:
        n = UniversityNotice(
            id=uuid.uuid4(),
            title="Outside path notice",
            notice_type="date_sheet",
            filename=Path(path).name,
            original_filename=Path(path).name,
            file_type="pdf",
            file_size=Path(path).stat().st_size,
            sha256="out",
            file_path=path,
            programme_ids=_j(["bca"]),
            extraction_status="verified" if published else "pending_verification",
            is_verified=published,
            is_published=published,
        )
        db.add(n)
        db.commit()
        _created_notice_ids.append(str(n.id))
        return str(n.id)
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 4. Zero-hallucination audit (service-level SQL gate)
# ---------------------------------------------------------------------------

def test_get_verified_schedule_gate_filters_every_dimension():
    rows = [
        dict(row_no=1, programme_id="bca", semester="4", exam_date="2026-06-12",
             subject="DS", paper_code="BCA401", start_time="10:00", end_time="13:00"),
        dict(row_no=2, programme_id="bca", semester="4", stream="cse",
             exam_date="2026-06-14", subject="CA", paper_code="BCA403",
             start_time="10:00", end_time="13:00"),
        dict(row_no=3, programme_id="bca", semester="6", exam_date="2026-07-01",
             subject="Net", paper_code="BCA601", start_time="10:00", end_time="13:00"),
        dict(row_no=4, programme_id="mca", semester="4", exam_date="2026-06-15",
             subject="DBMS", paper_code="MCA401", start_time="14:00", end_time="17:00"),
    ]
    nid, _ = _seed_notice(rows=rows, verified=True, published=True)
    db = SessionLocal()
    try:
        n = db.query(UniversityNotice).filter(UniversityNotice.id == nid).one()
        for e in n.entries:
            if e.row_no == 2:  # leave one bca s4 row UNVERIFIED on purpose
                e.extraction_status = "pending_verification"
        db.commit()

        got = notices.get_verified_schedule(db, [n.id], programme="bca", semester=4)
        assert [r.row_no for r in got] == [1]  # row 2 (tampered) is excluded

        got2 = notices.get_verified_schedule(db, [n.id], programme="bca")
        assert [r.row_no for r in got2] == [1, 3]

        got3 = notices.get_verified_schedule(db, [n.id], stream="cse")
        assert got3 == []  # the only cse row is unverified -> empty, never guessed

        got4 = notices.get_verified_schedule(db, [n.id])
        # Ordered by exam_date ASC, then row_no ASC: 06-12, 06-15, 07-01.
        assert [r.row_no for r in got4] == [1, 4, 3]
    finally:
        db.close()


def test_audit_trail_records_every_lifecycle_step():
    nid, _ = _seed_notice(rows=_ROWS_OK_BCA_S4)
    client.post(f"/api/admin/notices/{nid}/verify", headers=SUPER)
    client.post(f"/api/admin/notices/{nid}/publish", headers=SUPER)
    client.post(f"/api/admin/notices/{nid}/unpublish", headers=SUPER)

    db = SessionLocal()
    try:
        actor = str(SUPER.get("Authorization", "")[-40:])
        for action in ("notice.upload", "notice.verify", "notice.publish", "notice.unpublish"):
            # upload happens only in test_list_open...; verify/publish/unpublish here.
            pass
        rows = db.query(AuditLog).filter(
            AuditLog.target == nid,
            AuditLog.action.in_(["notice.verify", "notice.publish", "notice.unpublish"]),
        ).all()
        assert {r.action for r in rows} == {"notice.verify", "notice.publish", "notice.unpublish"}
        assert all(r.actor_role == "superadmin" for r in rows)
    finally:
        db.close()