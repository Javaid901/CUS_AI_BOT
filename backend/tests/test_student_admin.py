"""
backend/tests/test_student_admin.py

Phase A battery — Super Admin Student Management + DOB-as-password.

  1. DOB normalization is canonical and shared by hashing + verification.
  2. Student.hashed_password stores ONLY the bcrypt hash of the normalized DOB
     (never the plaintext DOB, never a random/old password).
  3. /api/student/verify authenticates Registration Number + DOB; the old
     password model is no longer functional; failures are generic.
  4. Super Admin CRUD (create / list / search / edit / toggle / reset-dob)
     with server-side require_superadmin on every endpoint.
  5. Atomic credential reset + deactivation revoke all existing sessions.
  6. Credential hygiene: DOB/hash never returned in API responses, never
     written to audit detail.
  7. Demo endpoints: ordinary Admins can no longer manipulate Student records.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.auth.security import hash_password
from app.database import SessionLocal, create_all
from app.main import app
from app.models import AuditLog, Student, StudentSession, User
from app.student.dob import hash_dob, normalize_dob, verify_dob
from app.student.session import resolve_session

create_all()

client = TestClient(app)

SUPER: dict[str, str] = {}
ADMIN: dict[str, str] = {}
STU: dict[str, str] = {}

_created_student_ids: list[str] = []
_created_user_ids: list[str] = []

_LOCAL = {"super": None, "admin": None, "student": None}


@pytest.fixture(autouse=True)
def _reset_rate_limit_bucket():
    """The verify endpoint is IP-rate-limited (5/min); clear the in-memory
    sliding window between tests so this battery can exercise the login
    contract freely without reimplementing the limiter."""
    yield
    from app.utils import rate_limit as _rl

    _rl._HITS.clear()


@pytest.fixture(scope="module", autouse=True)
def _bootstrap():
    db = SessionLocal()
    try:
        creds = [
            ("super", "superadmin"),
            ("admin", "admin"),
            ("student", "student"),
        ]
        for key, role in creds:
            username = f"__pa_{key}_{uuid.uuid4().hex[:6]}"
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
            _created_user_ids.append(str(user.id))
            _LOCAL[key] = username
        db.commit()
    finally:
        db.close()

    r = client.post("/api/auth/login", data={"username": _LOCAL["super"], "password": "secret123"})
    assert r.status_code == 200, r.text
    SUPER["Authorization"] = f"Bearer {r.json()['access_token']}"
    r = client.post("/api/auth/login", data={"username": _LOCAL["admin"], "password": "secret123"})
    assert r.status_code == 200, r.text
    ADMIN["Authorization"] = f"Bearer {r.json()['access_token']}"
    r = client.post("/api/auth/login", data={"username": _LOCAL["student"], "password": "secret123"})
    assert r.status_code == 200, r.text
    STU["Authorization"] = f"Bearer {r.json()['access_token']}"

    yield

    db = SessionLocal()
    try:
        for uid in _created_student_ids:
            db.query(StudentSession).filter(StudentSession.student_id == str(uid)).delete()
            db.query(Student).filter(Student.id == str(uid)).delete()
        for uid in _created_user_ids:
            db.query(AuditLog).filter(AuditLog.actor_id == uid).delete()
            db.query(User).filter(User.id == uid).delete()
        db.commit()
    finally:
        db.close()


def _payload(reg: str | None = None, dob: str = "2005-06-15", **over) -> dict:
    body = {
        "reg_no": reg or f"CUS-PA-{uuid.uuid4().hex[:6].upper()}",
        "name": "Phase A Student",
        "dob": dob,
        "programme": "bca",
        "current_semester": 2,
        "admission_year": 2023,
        "is_active": True,
    }
    body.update(over)
    return body


def _create(**over) -> tuple[dict, dict]:
    body = _payload(**over)
    r = client.post("/api/admin/students", json=body, headers=SUPER)
    assert r.status_code == 201, r.text
    dto = r.json()
    _created_student_ids.append(dto["id"])
    return dto, body


def _get_student(student_id: str) -> Student:
    db = SessionLocal()
    try:
        return db.get(Student, uuid.UUID(student_id))
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# DOB normalization + hashing
# --------------------------------------------------------------------------- #
def test_normalization_canonical_and_shared():
    assert normalize_dob("2004-07-15") == "2004-07-15"
    assert normalize_dob("15-Jul-2004") == "2004-07-15"
    assert normalize_dob("15 July 2004") == "2004-07-15"
    assert normalize_dob("Jul 15 2004") == "2004-07-15"
    assert normalize_dob("15-07-2004") == "2004-07-15"
    assert normalize_dob("15/07/2004") == "2004-07-15"
    assert normalize_dob("2004/07/15") == "2004-07-15"
    for bad in ("", "31-Feb-2004", "15-13-2004", "abc", "2004-07"):
        with pytest.raises(ValueError):
            normalize_dob(bad)


def test_hash_is_bcrypt_of_normalized_dob_only():
    h = hash_dob("15-Jul-2004")
    assert len(h) == 60 and h.startswith("$2b$")  # bcrypt from existing settings
    assert verify_dob("2004-07-15", h)
    assert verify_dob("15-07-2004", h)
    assert not verify_dob("student123", h)
    assert not verify_dob("2000-01-01", h)
    assert not verify_dob("garbage", h)
    # the stored value contains no plaintext DOB material
    assert "2004" not in h and "15-Jul" not in h


def test_seeders_hash_dob_not_student123():
    root = Path(__file__).resolve().parents[1]
    for rel in ("app/main.py", "app/seeders/demo_data.py"):
        text = Path(root, rel).read_text(encoding="utf-8")
        assert "student123" not in text, f"{rel} still uses the old password model"


# --------------------------------------------------------------------------- #
# Student Management — Super Admin
# --------------------------------------------------------------------------- #
def test_create_student_never_returns_credential():
    dto, body = _create()
    assert dto["reg_no"] == body["reg_no"].upper()
    assert "dob" not in dto and "hashed_password" not in dto

    detail_text = client.get(f"/api/admin/students/{dto['id']}", headers=SUPER).text
    assert "dob" not in detail_text and "hashed_password" not in detail_text

    student = _get_student(dto["id"])
    assert student.reg_no == body["reg_no"].upper()
    # DB stores bcrypt(normalized DOB), verifiable with any accepted format
    assert verify_dob(body["dob"], student.hashed_password)
    assert not verify_dob("student123", student.hashed_password)


def test_create_required_fields_validated():
    r = client.post("/api/admin/students", json={"reg_no": "CUS-X-1", "name": "Missing DOB"}, headers=SUPER)
    assert r.status_code == 422


def test_create_invalid_dob_rejected():
    r = client.post(
        "/api/admin/students",
        json=_payload("CUS-BAD-1", dob="31-Feb-2004"),
        headers=SUPER,
    )
    assert r.status_code == 422


def test_create_duplicate_reg_no_rejected():
    dto, body = _create()
    r = client.post("/api/admin/students", json=body, headers=SUPER)
    assert r.status_code == 409
    dup = dict(body)
    dup["reg_no"] = body["reg_no"].lower()  # case-insensitive duplicate
    r = client.post("/api/admin/students", json=dup, headers=SUPER)
    assert r.status_code == 409


def test_list_search_and_safe_payload():
    _create(reg="CUS-PA-SEARCH1", name="Alpha Search Student")
    _create(reg="CUS-PA-SEARCH2", name="Beta")

    r = client.get("/api/admin/students?q=Search", headers=SUPER)
    assert r.status_code == 200
    data = r.json()
    assert any(s["reg_no"] == "CUS-PA-SEARCH1" for s in data["students"])
    assert all("dob" not in s and "hashed_password" not in s for s in data["students"])

    r = client.get("/api/admin/students?q=nonexistent-zzz", headers=SUPER)
    assert r.json()["total"] == 0

    # frontend sends empty string filters (q= / status=) and numeric pagination
    r = client.get("/api/admin/students?q=&status=&page=1&page_size=20", headers=SUPER)
    assert r.status_code == 200

    # omitted pagination uses backend defaults (frontend always sends numbers)
    r = client.get("/api/admin/students", headers=SUPER)
    assert r.status_code == 200

    # status filter works
    r = client.get("/api/admin/students?status=inactive", headers=SUPER)
    assert r.status_code == 200
    assert all(s["is_active"] is False for s in r.json()["students"])


def test_edit_preserves_unrelated_fields_and_blocks_reg_no_change():
    dto, body = _create(reg="CUS-PA-EDIT1", dob="2005-06-15", college="Old College", batch="2023-2026")
    r = client.patch(f"/api/admin/students/{dto['id']}", json={"name": "Renamed"}, headers=SUPER)
    assert r.status_code == 200
    updated = r.json()
    assert updated["name"] == "Renamed"
    assert updated["college"] == "Old College"  # untouched
    assert updated["batch"] == "2023-2026"      # untouched
    assert "dob" not in updated

    # Attempting to smuggle reg_no / dob through the edit endpoint is ignored.
    r = client.patch(
        f"/api/admin/students/{dto['id']}",
        json={"reg_no": "CUS-EVIL-999", "dob": "1990-01-01"},
        headers=SUPER,
    )
    assert r.status_code == 200
    student = _get_student(dto["id"])
    assert student.reg_no == "CUS-PA-EDIT1"          # login handle unchanged
    assert verify_dob("2005-06-15", student.hashed_password)   # credential unchanged
    assert not verify_dob("1990-01-01", student.hashed_password)


def test_edit_uses_patch_single_endpoint():
    """Regression: admin student edit succeeds via PATCH (the method the
    frontend now sends); the backend exposes PATCH only — a PUT to the same
    URL must 405, proving there is exactly one correct method and no
    duplicate endpoint. Non-Super-Admin PATCH stays protected."""
    dto, body = _create(name="PATCH Target")
    r = client.patch(f"/api/admin/students/{dto['id']}", json={"name": "Patched Name"}, headers=SUPER)
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Patched Name"

    # The previous frontend bug sent PUT -> FastAPI routes it to 405.
    r = client.put(f"/api/admin/students/{dto['id']}", json={"name": "Should Not Apply"}, headers=SUPER)
    assert r.status_code == 405, r.text

    student = _get_student(dto["id"])
    assert student.name == "Patched Name"

    # Unauthorized / non-superadmin edits remain blocked on the matching method.
    assert client.patch(
        f"/api/admin/students/{dto['id']}", json={"name": "x"}, headers=ADMIN
    ).status_code == 403
    assert client.patch(f"/api/admin/students/{dto['id']}", json={"name": "x"}).status_code == 401


# --------------------------------------------------------------------------- #
# DOB-as-password login
# --------------------------------------------------------------------------- #
def test_login_reg_no_plus_dob():
    dto, body = _create(dob="2004-07-15")
    r = client.post("/api/student/verify", json={"reg_no": body["reg_no"], "dob": "2004-07-15"})
    assert r.status_code == 200
    payload = r.json()
    assert payload.get("verified") is True
    assert "dob" not in r.text and "hashed_password" not in r.text and "password" not in r.text.lower()

    # registration number lookup is case-insensitive
    r = client.post("/api/student/verify", json={"reg_no": body["reg_no"].lower(), "dob": "2004-07-15"})
    assert r.status_code == 200

    # identical canonical -> any accepted input format verifies
    r = client.post("/api/student/verify", json={"reg_no": body["reg_no"], "dob": "15-Jul-2004"})
    assert r.status_code == 200


def _msg(r):
    return r.json()["error"]["message"]


def test_login_wrong_dob_generic_failure():
    dto, body = _create(dob="2004-07-15")
    r = client.post("/api/student/verify", json={"reg_no": body["reg_no"], "dob": "2000-01-01"})
    assert r.status_code == 401
    assert _msg(r) == "Invalid registration number or Date of Birth."


def test_login_unknown_reg_equal_failure():
    r = client.post("/api/student/verify", json={"reg_no": "CUS-NOPE-999", "dob": "2000-01-01"})
    assert r.status_code == 401
    assert _msg(r) == "Invalid registration number or Date of Birth."


def test_login_unparseable_dob_generic_failure():
    dto, body = _create(dob="2004-07-15")
    r = client.post("/api/student/verify", json={"reg_no": body["reg_no"], "dob": "not-a-date"})
    assert r.status_code == 401
    assert _msg(r) == "Invalid registration number or Date of Birth."


def test_old_password_model_not_functional():
    dto, body = _create(dob="2004-07-15")
    # Old contract sent { reg_no, password } — that field no longer exists -> 422
    r = client.post("/api/student/verify", json={"reg_no": body["reg_no"], "password": "student123"})
    assert r.status_code == 422
    # Feeding the old password value in the DOB field -> generic 401
    r = client.post("/api/student/verify", json={"reg_no": body["reg_no"], "dob": "student123"})
    assert r.status_code == 401


# --------------------------------------------------------------------------- #
# Activation / deactivation + session revocation
# --------------------------------------------------------------------------- #
def test_deactivate_blocks_login_and_revokes_sessions():
    dto, body = _create(dob="2005-06-15")

    r = client.post("/api/student/verify", json={"reg_no": body["reg_no"], "dob": "2005-06-15"})
    assert r.status_code == 200
    sid = r.cookies.get("cus_student_sid")

    r = client.post(f"/api/admin/students/{dto['id']}/toggle", headers=SUPER)
    assert r.status_code == 200
    assert r.json()["is_active"] is False and r.json()["status"] == "deactivated"

    # record is NOT hard-deleted
    assert _get_student(dto["id"]) is not None

    # cannot authenticate while inactive (same generic failure)
    r = client.post("/api/student/verify", json={"reg_no": body["reg_no"], "dob": "2005-06-15"})
    assert r.status_code == 401

    # the previously-issued session is revoked server-side
    db = SessionLocal()
    try:
        assert resolve_session(db, sid) is None
    finally:
        db.close()

    # reactivate restores login
    r = client.post(f"/api/admin/students/{dto['id']}/toggle", headers=SUPER)
    assert r.status_code == 200 and r.json()["is_active"] is True
    r = client.post("/api/student/verify", json={"reg_no": body["reg_no"], "dob": "2005-06-15"})
    assert r.status_code == 200


def test_reset_dob_changes_hash_and_revokes_sessions_atomically():
    dto, body = _create(dob="2005-06-15")

    r = client.post("/api/student/verify", json={"reg_no": body["reg_no"], "dob": "2005-06-15"})
    assert r.status_code == 200
    sid = r.cookies.get("cus_student_sid")

    r = client.post(
        f"/api/admin/students/{dto['id']}/reset-dob",
        json={"dob": "1999-12-31"},
        headers=SUPER,
    )
    assert r.status_code == 200
    res = r.json()
    assert "dob" not in res and "hashed_password" not in res

    db = SessionLocal()
    try:
        student = db.get(Student, uuid.UUID(dto["id"]))
        assert not verify_dob("2005-06-15", student.hashed_password)  # old credential dead
        assert verify_dob("1999-12-31", student.hashed_password)      # new credential active
        assert resolve_session(db, sid) is None                       # old session revoked
        live = (
            db.query(StudentSession)
            .filter(StudentSession.student_id == student.id, StudentSession.revoked == False)
            .count()
        )
        assert live == 0
    finally:
        db.close()

    assert client.post(
        "/api/student/verify", json={"reg_no": body["reg_no"], "dob": "2005-06-15"}
    ).status_code == 401
    assert client.post(
        "/api/student/verify", json={"reg_no": body["reg_no"], "dob": "1999-12-31"}
    ).status_code == 200


# --------------------------------------------------------------------------- #
# Authorization — require_superadmin, IDOR-safe
# --------------------------------------------------------------------------- #
def test_admin_and_student_denied_everywhere():
    dto, body = _create()

    assert client.get("/api/admin/students", headers=ADMIN).status_code == 403
    assert client.get("/api/admin/students", headers=STU).status_code == 403
    assert client.get("/api/admin/students").status_code == 401

    assert client.post("/api/admin/students", json=_payload(), headers=ADMIN).status_code == 403
    assert client.post("/api/admin/students", json=_payload(), headers=STU).status_code == 403

    sid = dto["id"]
    assert client.get(f"/api/admin/students/{sid}", headers=ADMIN).status_code == 403
    assert client.get(f"/api/admin/students/{sid}", headers=STU).status_code == 403
    assert client.patch(f"/api/admin/students/{sid}", json={"name": "x"}, headers=ADMIN).status_code == 403
    assert client.post(f"/api/admin/students/{sid}/toggle", headers=ADMIN).status_code == 403
    assert client.post(
        f"/api/admin/students/{sid}/reset-dob", json={"dob": "2000-01-01"}, headers=ADMIN
    ).status_code == 403


def test_superadmin_can_reset_and_edit():
    dto, body = _create()
    r = client.patch(f"/api/admin/students/{dto['id']}", json={"name": "Ok"}, headers=SUPER)
    assert r.status_code == 200
    r = client.post(
        f"/api/admin/students/{dto['id']}/reset-dob", json={"dob": "1998-03-03"}, headers=SUPER
    )
    assert r.status_code == 200


# --------------------------------------------------------------------------- #
# Credential hygiene
# --------------------------------------------------------------------------- #
def test_plaintext_dob_column_never_stored():
    """The student's DOB is the password — plaintext must never be persisted.

    Regression for the audit finding: Student.dob existed as a plaintext mirror
    of the bcrypt credential. After the fix the column stays NULL on every
    write path (admin create, DOB reset); login depends on hashed_password only.
    """
    dto, body = _create(dob="2004-07-15")

    db = SessionLocal()
    try:
        student = db.get(Student, uuid.UUID(dto["id"]))
        assert student.dob is None, "plaintext DOB stored at create"
        assert verify_dob("2004-07-15", student.hashed_password)
    finally:
        db.close()

    assert client.post(
        "/api/student/verify", json={"reg_no": body["reg_no"], "dob": "2004-07-15"}
    ).status_code == 200

    r = client.post(
        f"/api/admin/students/{dto['id']}/reset-dob",
        json={"dob": "1999-12-31"},
        headers=SUPER,
    )
    assert r.status_code == 200

    db = SessionLocal()
    try:
        student = db.get(Student, uuid.UUID(dto["id"]))
        assert student.dob is None, "plaintext DOB stored at reset"
        assert not verify_dob("2004-07-15", student.hashed_password)
        assert verify_dob("1999-12-31", student.hashed_password)
    finally:
        db.close()


def test_audit_detail_never_logs_credentials():
    db = SessionLocal()
    try:
        rows = (
            db.query(AuditLog)
            .filter(AuditLog.action.in_(["student.create", "student.update", "student.toggle", "student.reset_dob"]))
            .all()
        )
        assert rows, "student management actions should be audited"
        for row in rows:
            blob = (row.detail or "") + "|" + (row.target or "")
            for banned in ("1999-12-31", "2005-06-15", "15-Jul-2004", "hashed_password", "student123"):
                assert banned not in blob
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Demo endpoint security
# --------------------------------------------------------------------------- #
def test_demo_endpoints_cannot_be_abused_by_admin():
    # Ensure at least one student exists so a superadmin seed call is cheap.
    _create(reg="CUS-PA-DEMOGUARD")

    destructive = [
        ("POST", "/api/admin/demo/seed"),
        ("POST", "/api/admin/demo/reset"),
        ("POST", "/api/admin/demo/regenerate"),
        ("GET", "/api/admin/demo/export"),       # export includes credential-adjacent data
        ("DELETE", "/api/admin/demo/students"),
    ]
    for method, url in destructive:
        r = client.request(method, url, headers=ADMIN)
        assert r.status_code == 403, f"admin {method} {url} -> {r.status_code}"
        r = client.request(method, url)
        assert r.status_code == 401, f"anon {method} {url} -> {r.status_code}"

    # Read-only demo status stays available to ordinary Admins.
    r = client.get("/api/admin/demo/status", headers=ADMIN)
    assert r.status_code == 200

    # Super Admin still reaches the demo seed (students exist -> cheap path).
    r = client.post("/api/admin/demo/seed", headers=SUPER)
    assert r.status_code == 200


def test_no_hard_delete_endpoint():
    """Phase A introduced activation/deactivation, not destructive deletion.

    Phase H supersedes this: a permanent DELETE route now exists and is
    enforced server-side as superadmin-only (403 admin / 403 student / 401
    anonymous). Full role + cascade behaviour lives in test_student_delete.py.
    """
    url = "/api/admin/students/CUS-PA-ANY"
    r = client.delete(url, headers=ADMIN)
    assert r.status_code == 403, f"admin -> {r.status_code}"
    r = client.delete(url, headers=STU)
    assert r.status_code == 403, f"student -> {r.status_code}"
    r = client.delete(url)
    assert r.status_code == 401, f"anon -> {r.status_code}"