"""
backend/tests/test_student_search.py

Phase I battery — ADMIN STUDENT SEARCH (server-side, READ-ONLY, superadmin only).

  1. Authorization: anonymous 401, admin/authority/student 403, superadmin 200
     (same require_superadmin boundary as the whole Students area).
  2. Search by name (case-insensitive, tolerant, MULTIPLE matches all returned).
  3. Search by registration number (exact + the existing substring convention).
  4. Search by class/college roll number (Student.roll_no — never exam roll).
  5. No-match search is a normal 200 + empty list, NOT a 404/error.
  6. Response safety: the ACTUAL API JSON contains no dob/date_of_birth/
     hashed_password/password/session/credential material.
  7. Examination roll number is the separate StudentResult.exam_roll_no value
     and is NOT Student.roll_no.
  8. Conflicting exam rolls are REPORTED (exam_roll_conflict) and never
     silently resolved to one value.
  9. A permanently deleted student does not appear in search.
  10. q is never interpreted as a DOB lookup (searching a date returns empty).
  11. DOB regression: no plaintext DOB exposed, hash stays bcrypt, schema gets
      no new fields, and DOB authentication is untouched.

Shared SQLite live DB (sqlite:///./cus_ai.db) — every created row is cleaned
up in the module teardown (the local engine enforces no FK cascade).
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.auth.security import hash_password
from app.config import settings
from app.database import SessionLocal, create_all
from app.main import app
from app.models import (
    AuditLog,
    RefreshToken,
    Student,
    StudentResult,
    User,
)

create_all()

client = TestClient(app)

SUPER: dict[str, str] = {}
ADMIN: dict[str, str] = {}
AUTH: dict[str, str] = {}
STU: dict[str, str] = {}

_created_student_ids: list[str] = []
_created_result_ids: list[str] = []
_created_user_ids: list[str] = []

_LOCAL = {"super": None, "admin": None, "authority": None, "student": None}


@pytest.fixture(autouse=True)
def _reset_rate_limit_bucket():
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
            ("authority", "authority"),
            ("student", "student"),
        ]
        for key, role in creds:
            username = f"__ps_{key}_{uuid.uuid4().hex[:6]}"
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

    for key, bucket in (("super", SUPER), ("admin", ADMIN), ("authority", AUTH), ("student", STU)):
        r = client.post("/api/auth/login", data={"username": _LOCAL[key], "password": "secret123"})
        assert r.status_code == 200, r.text
        bucket["Authorization"] = f"Bearer {r.json()['access_token']}"

    yield

    db = SessionLocal()
    try:
        for rid in _created_result_ids:
            db.query(StudentResult).filter(StudentResult.id == str(rid)).delete(
                synchronize_session=False
            )
        for uid in _created_student_ids:
            db.query(Student).filter(Student.id == str(uid)).delete(synchronize_session=False)
        for uid in _created_user_ids:
            db.query(RefreshToken).filter(RefreshToken.user_id == str(uid)).delete(
                synchronize_session=False
            )
            db.query(AuditLog).filter(AuditLog.actor_id == str(uid)).delete(synchronize_session=False)
            db.query(User).filter(User.id == str(uid)).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


def _create_student(**over) -> dict:
    body = {
        "reg_no": f"CUS-PS-{uuid.uuid4().hex[:6].upper()}",
        "name": "Abid Ahmad",
        "dob": "2005-06-15",
        "programme": "bca",
        "current_semester": 2,
        "admission_year": 2024,
        "is_active": True,
        "roll_no": "230101",
    }
    over = dict(over)
    if "reg" in over:
        over["reg_no"] = over.pop("reg")
    body.update(over)
    r = client.post("/api/admin/students", json=body, headers=SUPER)
    assert r.status_code == 201, r.text
    dto = r.json()
    _created_student_ids.append(dto["id"])
    return dto


def _seed_result(student_id: str, exam_roll_no: str | None, semester: int = 1) -> str:
    db = SessionLocal()
    try:
        row = StudentResult(
            student_id=uuid.UUID(student_id),
            exam_roll_no=exam_roll_no,
            semester=semester,
            exam_type="Regular",
            subject_name="Mathematics",
            subject_code="MAT101",
            internal_marks=35,
            external_marks=55,
            total_marks=90,
            max_marks=100,
            grade="A",
            status="pass",
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        rid = str(row.id)
    finally:
        db.close()
    _created_result_ids.append(rid)
    return rid


def _search(q: str, **params) -> list:
    url = "/api/admin/students/search?q=" + q
    for k, v in params.items():
        url += f"&{k}={v}"
    r = client.get(url, headers=SUPER)
    assert r.status_code == 200, r.text
    return r.json()["students"]


# --------------------------------------------------------------------------- #
# Authorization (same require_superadmin boundary as the Students area)
# --------------------------------------------------------------------------- #
def test_search_requires_superadmin():
    dto = _create_student()
    url = "/api/admin/students/search?q=" + dto["reg_no"]
    assert client.get(url).status_code == 401
    assert client.get(url, headers=ADMIN).status_code == 403
    assert client.get(url, headers=AUTH).status_code == 403
    assert client.get(url, headers=STU).status_code == 403
    assert client.get(url, headers=SUPER).status_code == 200


# --------------------------------------------------------------------------- #
# Search by name
# --------------------------------------------------------------------------- #
def test_search_by_name_is_case_insensitive_and_tolerant():
    _create_student(reg="CUS-PS-NAMEA", name="Abid Ahmad", roll_no="1001")
    _create_student(reg="CUS-PS-NAMEB", name="Abid Hussain", roll_no="1002")
    _create_student(reg="CUS-PS-NAMEC", name="Abid Malik", roll_no="1003")

    for q in ("abid", "ABID", "Abi", "ABI"):
        rows = _search(q)
        regs = {r["reg_no"] for r in rows}
        assert {"CUS-PS-NAMEA", "CUS-PS-NAMEB", "CUS-PS-NAMEC"} <= regs, q

    rows = _search("huss")
    assert rows and rows[0]["reg_no"] == "CUS-PS-NAMEB"


# --------------------------------------------------------------------------- #
# Search by registration number
# --------------------------------------------------------------------------- #
def test_search_by_registration_number_exact_and_partial():
    dto = _create_student(reg="CUS-PS-REGX", name="Reg Search Student")
    rows = _search(dto["reg_no"].lower())  # case-insensitive exact
    assert any(r["reg_no"] == "CUS-PS-REGX" for r in rows)
    assert rows[0]["reg_no"] == "CUS-PS-REGX", "exact reg match must rank first"

    rows = _search("REGX")
    assert any(r["reg_no"] == "CUS-PS-REGX" for r in rows), "partial reg search preserved"


# --------------------------------------------------------------------------- #
# Search by class/college roll number (Student.roll_no)
# --------------------------------------------------------------------------- #
def test_search_by_class_roll_number():
    _create_student(reg="CUS-PS-ROLLA", name="Roll Student A", roll_no="301055")
    rows = _search("301055")
    assert rows and rows[0]["reg_no"] == "CUS-PS-ROLLA", "exact roll must rank first"
    rows = _search("3010")
    assert any(r["reg_no"] == "CUS-PS-ROLLA" for r in rows), "roll prefix search supported"


# --------------------------------------------------------------------------- #
# No result
# --------------------------------------------------------------------------- #
def test_search_no_result_is_empty_state_not_error():
    r = client.get("/api/admin/students/search?q=zzzz_no_such_student_zzzz", headers=SUPER)
    assert r.status_code == 200
    body = r.json()
    assert body["students"] == []
    assert body["total"] == 0
    assert body["page"] == 1


# --------------------------------------------------------------------------- #
# Response safety (raw JSON, not just UI)
# --------------------------------------------------------------------------- #
def test_search_response_contains_no_credential_material():
    dto = _create_student()
    r = client.get("/api/admin/students/search?q=" + dto["reg_no"], headers=SUPER)
    assert r.status_code == 200

    seen = set()
    for item in r.json()["students"]:
        seen.update(item.keys())
    assert set(seen) <= {
        "id", "name", "reg_no", "roll_no", "exam_roll_no", "exam_roll_conflict",
        "programme", "current_semester",
    }, f"unexpected keys: {seen}"

    blob = r.text.lower()
    for banned in (
        "dob", "date_of_birth", "hashed_password", "password", "token",
        "session", "bcrypt", "$2b$", "credential",
    ):
        assert banned not in blob, f"credential material leaked in search JSON: {banned}"


# --------------------------------------------------------------------------- #
# Examination roll number (separate field, consistency policy)
# --------------------------------------------------------------------------- #
def test_search_exam_roll_is_not_student_roll_no():
    dto = _create_student(reg="CUS-PS-EXAM", name="Exam Roll Student", roll_no="777001")
    _seed_result(dto["id"], exam_roll_no="90012")
    rows = _search(dto["reg_no"])
    assert len(rows) == 1
    hit = rows[0]
    assert hit["roll_no"] == "777001", "class roll must stay Student.roll_no"
    assert hit["exam_roll_no"] == "90012", "exam roll must come from StudentResult.exam_roll_no"
    assert hit["exam_roll_no"] != hit["roll_no"]
    assert hit["exam_roll_conflict"] is False


def test_search_reports_conflicting_exam_rolls_without_picking_one():
    dto = _create_student(reg="CUS-PS-CONF", name="Conflicting Rolls", roll_no="880101")
    _seed_result(dto["id"], exam_roll_no="99001", semester=1)
    _seed_result(dto["id"], exam_roll_no="99002", semester=2)
    rows = _search(dto["reg_no"])
    assert len(rows) == 1
    hit = rows[0]
    assert hit["exam_roll_conflict"] is True, "conflict must be reported"
    assert hit["exam_roll_no"] is None, "never silently pick one conflicting roll"


def test_search_no_results_means_no_exam_roll():
    dto = _create_student(reg="CUS-PS-NOEXAM", name="No Results Student")
    rows = _search(dto["reg_no"])
    assert rows[0]["exam_roll_no"] is None
    assert rows[0]["exam_roll_conflict"] is False


# --------------------------------------------------------------------------- #
# Deleted students
# --------------------------------------------------------------------------- #
def test_deleted_student_does_not_appear_in_search():
    dto = _create_student(reg="CUS-PS-TOMB", name="Ghost Student")
    _seed_result(dto["id"], exam_roll_no="12345")
    assert any(r["reg_no"] == "CUS-PS-TOMB" for r in _search("GHOST"))

    r = client.delete(f"/api/admin/students/{dto['id']}", headers=SUPER)
    assert r.status_code == 200

    rows = _search("GHOST")
    assert all(r["reg_no"] != "CUS-PS-TOMB" for r in rows), "deleted student must not appear"


# --------------------------------------------------------------------------- #
# Status filter preservation
# --------------------------------------------------------------------------- #
def test_search_respects_status_filter():
    dto = _create_student(reg="CUS-PS-INACT", name="Inactive Searcher")
    client.post(f"/api/admin/students/{dto['id']}/toggle", headers=SUPER)
    rows = _search("Inactive Searcher", status="active")
    assert all(r["reg_no"] != "CUS-PS-INACT" for r in rows)
    assert any(r["reg_no"] == "CUS-PS-INACT" for r in _search("Inactive Searcher", status="inactive"))


# --------------------------------------------------------------------------- #
# DOB security regression
# --------------------------------------------------------------------------- #
def test_search_does_not_support_dob_lookup():
    r = client.get("/api/admin/students/search?q=15-06-2005", headers=SUPER)
    assert r.status_code == 200
    assert r.json()["students"] == [], "a DOB value must never be searchable"


def test_search_introduces_no_plaintext_dob_and_leaves_auth_intact():
    dto = _create_student(reg="CUS-PS-SEC", name="Security Student")

    db = SessionLocal()
    try:
        student = db.query(Student).filter(Student.id == str(dto["id"])).one()
        assert student.hashed_password.startswith("$2"), "hash must remain bcrypt"
        assert "2005-06-15" not in student.hashed_password, "no plaintext DOB in hash"
        colset = {c.name for c in Student.__table__.columns}
        assert "hashed_password" in colset and "dob" in colset
        assert colset == {
            "id", "reg_no", "roll_no", "name", "father_name", "mother_name", "dob",
            "gender", "category", "email", "phone", "college", "programme",
            "academic_scheme", "current_semester", "admission_year", "batch",
            "address", "status", "hashed_password", "is_active", "created_at", "updated_at",
        }, "no new schema fields may be introduced by the search feature"
    finally:
        db.close()

    # DOB authentication is untouched: correct DOB still signs the student in.
    r = client.post("/api/student/verify", json={"reg_no": dto["reg_no"], "dob": "2005-06-15"})
    assert r.status_code == 200, r.text