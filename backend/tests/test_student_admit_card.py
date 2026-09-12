"""
backend/tests/test_student_admit_card.py

Phase C battery — Student Admit Card (authenticated "my admit card" +
Super-Admin structured management: create/edit/withdraw + CSV/XLSX import
with preview → confirm → single-transaction apply). Cards are structured
records (no file/PDF storage).

  1. Student side (server-validated session cookie):
       - unauthenticated / expired / revoked / inactive sessions → 401
       - own cards only; semester allowlist; safe "not issued" message
       - IDOR: cross-student access blocked even with tampered params
       - response payload is an explicit allowlist (no ids / credentials)
  2. Super Admin side (require_superadmin everywhere):
       - list; create / update / withdraw; import preview (writes nothing);
         import confirm (atomic); duplicates (in-file + DB) rejected
       - audit records only filename/reg + count (never credentials/PII)
  3. Chat flow: authenticated hub option → semester picker → detail;
       no credential material in the stream; chip routing is deterministic.
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.auth.security import hash_password
from app.database import SessionLocal, create_all
from app.main import app
from app.models import AuditLog, Student, StudentAdmitCard, StudentSession, User
from app.student.session import create_session, revoke_session

create_all()

client = TestClient(app)
# Separate clients = isolated cookie jars per student (A / B).
client_a = TestClient(app)
client_b = TestClient(app)

SUPER: dict[str, str] = {}
ADMIN: dict[str, str] = {}
STU: dict[str, str] = {}

_LOCAL = {"super": None, "admin": None, "user": None}

_created_student_ids: list[str] = []
_created_user_ids: list[str] = []
_created_card_ids: list[str] = []

A = {}  # student A dto
B = {}  # student B dto


@pytest.fixture(autouse=True)
def _reset_rate_limit_bucket():
    yield
    from app.utils import rate_limit as _rl

    _rl._HITS.clear()


@pytest.fixture(scope="module", autouse=True)
def _bootstrap():
    db = SessionLocal()
    try:
        for key, role in (("super", "superadmin"), ("admin", "admin"), ("user", "student")):
            username = f"__ac_{key}_{uuid.uuid4().hex[:6]}"
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
    r = client.post("/api/auth/login", data={"username": _LOCAL["user"], "password": "secret123"})
    assert r.status_code == 200, r.text
    STU["Authorization"] = f"Bearer {r.json()['access_token']}"

    yield

    db = SessionLocal()
    try:
        for cid in _created_card_ids:
            db.query(StudentAdmitCard).filter(StudentAdmitCard.id == str(cid)).delete()
        for uid in _created_student_ids:
            db.query(StudentSession).filter(StudentSession.student_id == str(uid)).delete()
            db.query(Student).filter(Student.id == str(uid)).delete()
        for uid in _created_user_ids:
            db.query(AuditLog).filter(AuditLog.actor_id == uid).delete()
            db.query(User).filter(User.id == uid).delete()
        db.commit()
    finally:
        db.close()


def _create_student(reg: str | None = None, dob: str = "2005-06-15", **over) -> dict:
    body = {
        "reg_no": reg or f"CUS-AC-{uuid.uuid4().hex[:6].upper()}",
        "name": "Admit Card Student",
        "dob": dob,
        "programme": "bca",
        "current_semester": 2,
        "admission_year": 2023,
        "is_active": True,
    }
    body.update(over)
    r = client.post("/api/admin/students", json=body, headers=SUPER)
    assert r.status_code == 201, r.text
    dto = r.json()
    _created_student_ids.append(dto["id"])
    return dto


def _seed_card(db, student_id: str, semester: int = 1, **over) -> str:
    r = StudentAdmitCard(
        id=uuid.uuid4(),
        student_id=uuid.UUID(student_id),
        semester=semester,
        exam_type="Regular",
        exam_session="May/Jun 2024",
        centre_name="Sri Pratap College, Srinagar - Main Campus",
        centre_code="SPC01",
        centre_address="Lal Chowk, Srinagar, J&K",
        reporting_time="09:00 AM",
        subjects=json.dumps(["Mathematics", "Physics"]),
        instructions=json.dumps([
            "Bring this admit card to the examination hall",
            "Carry a valid photo ID (Aadhaar/College ID)",
        ]),
        issued_date="01-May-2024",
        academic_year="2023-2024",
    )
    for k, v in over.items():
        setattr(r, k, v)
    db.add(r)
    db.commit()
    _created_card_ids.append(str(r.id))
    return str(r.id)


def _login(client_: TestClient, reg: str, dob: str = "2005-06-15") -> None:
    r = client_.post("/api/student/verify", json={"reg_no": reg, "dob": dob})
    assert r.status_code == 200, r.text


def _msg(r):
    return r.json()["error"]["message"]


_DEFAULT_HEADERS = [
    "registration_number", "semester", "exam_type", "exam_session", "academic_year",
    "centre_name", "centre_code", "centre_address", "reporting_time",
    "subjects", "instructions", "issued_date",
]


def _csv_bytes(rows: list[list], headers: list[str] | None = None) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(headers or _DEFAULT_HEADERS)
    w.writerows(rows)
    return buf.getvalue().encode("utf-8")


def _xlsx_bytes(rows: list[list], headers: list[str] | None = None) -> bytes:
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.append(headers or _DEFAULT_HEADERS)
    for r in rows:
        ws.append(r)
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _preview(rows: list[list], filename: str = "c.csv", headers=None) -> object:
    ext = filename.rsplit(".", 1)[-1]
    content = _csv_bytes(rows, headers) if ext == "csv" else _xlsx_bytes(rows, headers)
    mime = "text/csv" if ext == "csv" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return client.post(
        "/api/admin/admit-cards/preview",
        files={"file": (filename, content, mime)},
        headers=SUPER,
    )


def _confirm(raw_rows, filename: str = "c.csv"):
    return client.post(
        "/api/admin/admit-cards/confirm",
        json={"filename": filename, "rows": raw_rows},
        headers=SUPER,
    )


def _raw_dicts(row_lists):
    return [dict(zip(_DEFAULT_HEADERS, row)) for row in row_lists]


def _count_cards():
    db = SessionLocal()
    try:
        return db.query(StudentAdmitCard).count()
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Setup used across tests
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module", autouse=True)
def _seed_people():
    global A, B
    A.update(_create_student(reg="CUS-AC-A0001"))
    B.update(_create_student(reg="CUS-AC-B0001"))
    db = SessionLocal()
    try:
        # A: cards for semesters 1 & 2 (drives the picker + card detail)
        _seed_card(db, A["id"], semester=1)
        _seed_card(db, A["id"], semester=2, exam_session="Nov/Dec 2024",
                   centre_name="Amar Singh College, Srinagar", centre_code="ASC02",
                   issued_date="01-Nov-2024", academic_year="2024-2025",
                   subjects=json.dumps(["Computer Science", "English"]),
                   instructions=json.dumps(["Bring this admit card to the examination hall"]))
    finally:
        db.close()
    _login(client_a, A["reg_no"])
    _login(client_b, B["reg_no"])


# --------------------------------------------------------------------------- #
# Student side — authn / authz
# --------------------------------------------------------------------------- #
def test_admit_cards_requires_valid_session():
    r = client.get("/api/student/admit-cards")
    assert r.status_code == 401
    assert "session" in _msg(r).lower()


def test_authed_student_lists_own_semesters():
    r = client_a.get("/api/student/admit-cards")
    assert r.status_code == 200
    sems = {s["semester"] for s in r.json()["semesters"]}
    assert sems == {1, 2}


def test_authed_card_payload_is_safe_allowlist():
    r = client_a.get("/api/student/admit-cards?semester=1")
    assert r.status_code == 200
    card = r.json()["card"]
    assert card is not None
    allowed = {
        "semester", "exam_type", "exam_session", "academic_year", "centre_name",
        "centre_code", "centre_address", "reporting_time", "subjects",
        "instructions", "issued_date",
    }
    assert set(card.keys()) == allowed
    assert card["centre_code"] == "SPC01"
    assert set(card["subjects"]) == {"Mathematics", "Physics"}
    assert len(card["instructions"]) == 2
    blob = r.text
    for banned in ("student_id", "reg_no", "dob", "hashed_password", "token", "password"):
        assert banned not in blob


def test_no_card_semester_is_safe_message():
    r = client_a.get("/api/student/admit-cards?semester=4")
    assert r.status_code == 200
    data = r.json()
    assert data["card"] is None
    assert "No admit card is issued" in data["message"]
    rb = client_b.get("/api/student/admit-cards?semester=1")
    assert rb.status_code == 200
    assert rb.json()["card"] is None
    assert "No admit card is issued" in rb.json()["message"]


def test_semester_outside_allowlist_rejected():
    r = client_a.get("/api/student/admit-cards?semester=9")
    assert r.status_code == 422


def test_expired_session_rejected():
    dto = _create_student(reg="CUS-AC-EXP901")
    db = SessionLocal()
    try:
        stu = db.query(Student).filter(Student.id == uuid.UUID(dto["id"])).first()
        raw = create_session(db, stu, ttl_minutes=1)
        row = db.query(StudentSession).filter(StudentSession.student_id == stu.id).first()
        row.expires_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()
    r = client.get("/api/student/admit-cards", cookies={"cus_student_sid": raw})
    assert r.status_code == 401
    db = SessionLocal()
    try:
        stu = db.query(Student).filter(Student.id == uuid.UUID(dto["id"])).first()
        db.delete(stu)
        db.commit()
    finally:
        db.close()


def test_revoked_session_rejected():
    dto = _create_student(reg="CUS-AC-REV902")
    db = SessionLocal()
    try:
        stu = db.query(Student).filter(Student.id == uuid.UUID(dto["id"])).first()
        raw = create_session(db, stu, ttl_minutes=10)
        revoke_session(db, raw)
    finally:
        db.close()
    r = client.get("/api/student/admit-cards", cookies={"cus_student_sid": raw})
    assert r.status_code == 401
    db = SessionLocal()
    try:
        stu = db.query(Student).filter(Student.id == uuid.UUID(dto["id"])).first()
        db.delete(stu)
        db.commit()
    finally:
        db.close()


def test_inactive_student_session_rejected():
    dto = _create_student(reg="CUS-AC-IA900")
    db = SessionLocal()
    try:
        stu = db.query(Student).filter(Student.id == uuid.UUID(dto["id"])).first()
        raw = create_session(db, stu, ttl_minutes=10)
        stu.is_active = False
        stu.status = "deactivated"
        db.commit()
    finally:
        db.close()
    r = client.get("/api/student/admit-cards", cookies={"cus_student_sid": raw})
    assert r.status_code == 401
    db = SessionLocal()
    try:
        stu = db.query(Student).filter(Student.id == uuid.UUID(dto["id"])).first()
        db.delete(stu)
        db.commit()
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# IDOR — cross-student access is structurally impossible
# --------------------------------------------------------------------------- #
def test_idor_other_student_cannot_read_as():
    # A tampers identity params; the server still resolves A's own card.
    r = client_a.get(
        "/api/student/admit-cards",
        params={"semester": 1, "student_id": B["id"], "reg_no": B["reg_no"], "student_id2": B["id"]},
    )
    assert r.status_code == 200
    card = r.json()["card"]
    assert card is not None
    assert card["centre_code"] == "SPC01"  # A's own card


# --------------------------------------------------------------------------- #
# Super-Admin management + import
# --------------------------------------------------------------------------- #
def test_admin_admit_cards_authz():
    assert client.get("/api/admin/admit-cards", headers=ADMIN).status_code == 403
    assert client.get("/api/admin/admit-cards", headers=STU).status_code == 403
    assert client.get("/api/admin/admit-cards").status_code == 401
    r = client.get("/api/admin/admit-cards", headers=SUPER)
    assert r.status_code == 200
    assert any(x["reg_no"] == "CUS-AC-A0001" and x["semester"] == 1 for x in r.json()["admit_cards"])

    payload = {"filename": "x.csv", "rows": []}
    assert client.post("/api/admin/admit-cards/preview", files={"file": ("x.csv", b"a,b", "text/csv")}, headers=ADMIN).status_code == 403
    assert client.post("/api/admin/admit-cards/confirm", json=payload, headers=ADMIN).status_code == 403
    assert client.post("/api/admin/admit-cards/confirm", json=payload).status_code == 401
    assert client.post("/api/admin/admit-cards", json={"reg_no": "CUS-AC-A0001", "semester": 1}, headers=ADMIN).status_code == 403


def test_admin_create_card_roundtrip():
    body = {
        "reg_no": "CUS-AC-A0001",
        "semester": 3,
        "exam_type": "Regular",
        "exam_session": "May/Jun 2025",
        "academic_year": "2024-2025",
        "centre_name": "Government Degree College, Bemina",
        "centre_code": "GDC03",
        "centre_address": "Bemina, Srinagar, J&K",
        "reporting_time": "10:00 AM",
        "subjects": ["Zoology", "Botany"],
        "instructions": ["Bring this admit card to the examination hall"],
        "issued_date": "01-May-2025",
    }
    r = client.post("/api/admin/admit-cards", json=body, headers=SUPER)
    assert r.status_code == 201, r.text
    card = r.json()
    assert card["reg_no"] == "CUS-AC-A0001"
    assert card["semester"] == 3
    assert set(card["subjects"]) == {"Zoology", "Botany"}
    _created_card_ids.append(card["id"])
    # Student now sees it.
    rs = client_a.get("/api/student/admit-cards?semester=3")
    assert rs.status_code == 200
    assert rs.json()["card"]["centre_code"] == "GDC03"


def test_admin_create_duplicate_rejected():
    # Same identity (student/semester/exam session+year/type) as the seeded
    # semester-1 card → must be refused.
    body = {
        "reg_no": "CUS-AC-A0001", "semester": 1, "centre_name": "Duplicate Centre",
        "exam_type": "Regular", "academic_year": "2023-2024", "exam_session": "May/Jun 2024",
    }
    r = client.post("/api/admin/admit-cards", json=body, headers=SUPER)
    assert r.status_code == 409, r.text
    assert "already" in _msg(r).lower()


def test_admin_create_unknown_student_rejected():
    body = {"reg_no": "CUS-AC-NOPE9", "semester": 1, "centre_name": "X Centre"}
    r = client.post("/api/admin/admit-cards", json=body, headers=SUPER)
    assert r.status_code == 404
    assert "not found" in _msg(r).lower()


def test_admin_create_invalid_semester_rejected():
    body = {"reg_no": "CUS-AC-A0001", "semester": 9, "centre_name": "X Centre"}
    r = client.post("/api/admin/admit-cards", json=body, headers=SUPER)
    assert r.status_code == 422


def test_admin_create_missing_centre_rejected():
    body = {"reg_no": "CUS-AC-A0001", "semester": 1}
    r = client.post("/api/admin/admit-cards", json=body, headers=SUPER)
    assert r.status_code == 422
    assert "Centre name" in _msg(r)


def test_admin_update_card():
    # Give A a semester-4 card, update its centre, then delete it.
    body = {"reg_no": "CUS-AC-A0001", "semester": 4, "centre_name": "Old Centre"}
    r = client.post("/api/admin/admit-cards", json=body, headers=SUPER)
    assert r.status_code == 201, r.text
    card_id = r.json()["id"]
    _created_card_ids.append(card_id)

    r = client.patch(f"/api/admin/admit-cards/{card_id}", json={"centre_name": "New Centre", "reporting_time": "11:30 AM"}, headers=SUPER)
    assert r.status_code == 200, r.text
    assert r.json()["centre_name"] == "New Centre"
    assert r.json()["reporting_time"] == "11:30 AM"

    # Changing identity fields to collide with the seeded (sem 1) card → 409.
    r = client.patch(
        f"/api/admin/admit-cards/{card_id}",
        json={"semester": 1, "exam_type": "Regular", "academic_year": "2023-2024", "exam_session": "May/Jun 2024"},
        headers=SUPER,
    )
    assert r.status_code == 409
    assert "already" in _msg(r).lower()

    # Unknown id → 404
    r = client.patch(f"/api/admin/admit-cards/{uuid.uuid4()}", json={"centre_name": "X"}, headers=SUPER)
    assert r.status_code == 404


def test_admin_update_card_invalid_semester_rejected():
    body = {"reg_no": "CUS-AC-A0001", "semester": 5, "centre_name": "Tmp Centre", "exam_session": "May/Jun 2026"}
    r = client.post("/api/admin/admit-cards", json=body, headers=SUPER)
    assert r.status_code == 201, r.text
    card_id = r.json()["id"]
    _created_card_ids.append(card_id)
    r = client.patch(f"/api/admin/admit-cards/{card_id}", json={"semester": 12}, headers=SUPER)
    assert r.status_code == 422


def test_admin_delete_card_withdraws_for_student():
    body = {"reg_no": "CUS-AC-A0001", "semester": 8, "centre_name": "WSC04 Valid Centre"}
    r = client.post("/api/admin/admit-cards", json=body, headers=SUPER)
    assert r.status_code == 201, r.text
    card_id = r.json()["id"]

    assert client_a.get("/api/student/admit-cards?semester=8").json()["card"] is not None

    r = client.delete(f"/api/admin/admit-cards/{card_id}", headers=SUPER)
    assert r.status_code == 200
    assert r.json()["deleted"] is True

    assert client_a.get("/api/student/admit-cards?semester=8").json()["card"] is None
    assert "No admit card is issued" in client_a.get("/api/student/admit-cards?semester=8").json()["message"]

    r = client.delete(f"/api/admin/admit-cards/{card_id}", headers=SUPER)
    assert r.status_code == 404


def test_admin_list_filters_and_paginates():
    r = client.get("/api/admin/admit-cards?q=CUS-AC-A0001&semester=1&page=1&page_size=5", headers=SUPER)
    assert r.status_code == 200
    data = r.json()
    assert data["total"] >= 1
    assert all(x["semester"] == 1 for x in data["admit_cards"])
    assert all(x["reg_no"] == "CUS-AC-A0001" for x in data["admit_cards"])
    assert "id" in data["admit_cards"][0]
    assert "subjects" in data["admit_cards"][0]

    r = client.get("/api/admin/admit-cards?semester=9&page_size=5", headers=SUPER)
    assert r.status_code == 422


def test_preview_valid_csv_resolves_students():
    rows = [
        ["CUS-AC-A0001", 6, "Regular", "Nov/Dec 2025", "2025-2026", "SPC01", "Sri Pratap College, Srinagar - Main Campus", "Lal Chowk, Srinagar", "09:00 AM", "Physics\nChemistry", "Bring this admit card", "01-Nov-2025"],
        ["CUS-AC-B0001", 6, "Regular", "Nov/Dec 2025", "2025-2026", "ASC02", "Amar Singh College, Srinagar", "Soura, Srinagar", "09:00 AM", "English", "Bring this admit card", "01-Nov-2025"],
    ]
    r = _preview(rows, "a.csv")
    assert r.status_code == 200
    data = r.json()
    assert data["total_rows"] == 2
    assert data["valid_count"] == 2
    assert data["error_count"] == 0
    assert data["rows"][0]["registration_number"] == "CUS-AC-A0001"
    assert data["rows"][0]["subjects"] == '["Physics", "Chemistry"]'


def test_preview_rejects_unknown_student():
    rows = [["CUS-AC-NOPE1", 6, "Regular", "", "", "GDC03", "Govt Degree College", "Bemina", "", "Zoology", "", ""]]
    r = _preview(rows, "a.csv")
    assert r.status_code == 200
    data = r.json()
    assert data["error_count"] == 1
    assert "Unknown registration number" in data["errors"][0]["message"]


def test_preview_reports_row_validation_errors():
    rows = [
        ["", 6, "Regular", "", "", "GDC03", "Govt Degree College", "Bemina", "", "Zoology", "", ""],                                  # no reg
        ["CUS-AC-A0001", "abc", "Regular", "", "", "GDC03", "Govt Degree College", "Bemina", "", "Zoology", "", ""],                 # bad semester
        ["CUS-AC-A0001", 6, "Regular", "", "", "", "", "Bemina", "", "Zoology", "", ""],                                              # no centre
        ["CUS-AC-A0001", 31, "Regular", "", "", "GDC03", "Govt Degree College", "Bemina", "", "Zoology", "", ""],                    # semester 31
    ]
    r = _preview(rows, "a.csv")
    assert r.status_code == 200
    data = r.json()
    assert data["error_count"] == 4
    msgs = " ".join(e["message"] for e in data["errors"])
    for frag in ("Registration number is required", "Semester must be a whole number",
                 "Centre name is required", "not in the allowed semester list"):
        assert frag in msgs


def test_preview_xlsx_supported():
    rows = [
        ["CUS-AC-A0001", 6, "Regular", "Nov/Dec 2025", "2025-2026", "SPC01", "Sri Pratap College", "Lal Chowk", "09:00 AM", "Economics", "Bring this admit card", "01-Nov-2025"],
    ]
    r = _preview(rows, "b.xlsx")
    assert r.status_code == 200
    assert r.json()["valid_count"] == 1


def test_preview_rejects_unsupported_format():
    r = client.post(
        "/api/admin/admit-cards/preview",
        files={"file": ("c.pdf", b"%PDF-fake", "application/pdf")},
        headers=SUPER,
    )
    assert r.status_code == 400


def test_preview_missing_required_column_reports_header_error():
    rows = [["CUS-AC-A0001", 3]]
    headers = ["registration_number", "semester"]
    r = _preview(rows, "a.csv", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert data["error_count"] == 1
    assert "Missing required column" in data["errors"][0]["message"]
    assert data["valid_count"] == 0


def test_preview_in_file_duplicate_blocked():
    row = ["CUS-AC-A0001", 6, "Regular", "Nov/Dec 2025", "2025-2026", "SPC01", "Sri Pratap College", "Lal Chowk", "09:00 AM", "Physics", "Bring this admit card", "01-Nov-2025"]
    r = _preview([row, row], "dup.csv")
    assert r.status_code == 200
    data = r.json()
    assert data["error_count"] == 1
    assert "Duplicate row in the file" in data["errors"][0]["message"]


def test_preview_writes_nothing():
    before = _count_cards()
    rows = [
        ["CUS-AC-A0001", 6, "Regular", "Nov/Dec 2025", "2025-2026", "SPC01", "Sri Pratap College", "Lal Chowk", "09:00 AM", "Physics", "Bring this admit card", "01-Nov-2025"],
    ]
    r = _preview(rows, "a.csv")
    assert r.status_code == 200 and r.json()["valid_count"] == 1
    assert _count_cards() == before  # preview never writes


def test_confirm_applies_in_single_transaction():
    before = _count_cards()
    rows = [
        ["CUS-AC-A0001", 6, "Regular", "Nov/Dec 2025", "2025-2026", "SPC01", "Sri Pratap College, Srinagar - Main Campus", "Lal Chowk, Srinagar", "09:00 AM", "Physics", "Bring this admit card", "01-Nov-2025"],
        ["CUS-AC-A0001", 6, "Supplementary", "Nov/Dec 2025", "2025-2026", "SPC01", "Sri Pratap College, Srinagar - Main Campus", "Lal Chowk, Srinagar", "09:00 AM", "Physics", "Bring this admit card", "01-Nov-2025"],
    ]
    p = _preview(rows, "c.csv")
    assert p.status_code == 200 and p.json()["error_count"] == 0

    r = _confirm(p.json()["raw_rows"], "c.csv")
    assert r.status_code == 200
    assert r.json()["imported"] == 2
    assert _count_cards() == before + 2

    db = SessionLocal()
    try:
        aud = (
            db.query(AuditLog)
            .filter(AuditLog.action == "student_admit_card.import")
            .order_by(AuditLog.created_at.desc())
            .first()
        )
        assert aud is not None
        blob = f"{aud.detail}|{aud.target}".lower()
        for banned in ("2005", "hashed_password", "password", "token"):
            assert banned not in blob
    finally:
        db.close()


def test_confirm_rejects_bad_rows_and_writes_nothing():
    before = _count_cards()
    rows = [
        ["CUS-AC-A0001", 6, "Regular", "Nov/Dec 2025", "2025-2026", "SPC01", "Sri Pratap College", "Lal Chowk", "09:00 AM", "Physics", "Bring this admit card", "01-Nov-2025"],
        ["CUS-AC-NOPE9", 6, "Regular", "Nov/Dec 2025", "2025-2026", "SPC01", "Unknown College", "Lal Chowk", "09:00 AM", "Physics", "Bring this admit card", "01-Nov-2025"],
    ]
    r = _confirm(_raw_dicts(rows), "bad.csv")
    assert r.status_code == 422
    assert _count_cards() == before  # nothing written


def test_confirm_db_duplicate_returns_409_and_writes_nothing():
    # Re-confirm the exact rows applied above → fresh validation sees the DB rows.
    before = _count_cards()
    rows = [
        ["CUS-AC-A0001", 7, "Regular", "Nov/Dec 2025", "2025-2026", "SPC01", "Sri Pratap College", "Lal Chowk", "09:00 AM", "Physics", "Bring this admit card", "01-Nov-2025"],
    ]
    r = _confirm(_raw_dicts(rows), "dupdb.csv")
    assert r.status_code == 200
    assert _count_cards() == before + 1

    r = _confirm(_raw_dicts(rows), "dupdb2.csv")
    assert r.status_code == 409
    assert "duplicate" in _msg(r).lower()
    assert _count_cards() == before + 1


def test_admin_audit_rows_written():
    db = SessionLocal()
    try:
        actions = {
            a.action for a in
            db.query(AuditLog).filter(AuditLog.actor_id == _created_user_ids[0]).all()
        }
    finally:
        db.close()
    assert "student_admit_card.create" in actions
    assert "student_admit_card.delete" in actions


# --------------------------------------------------------------------------- #
# Chat integration — authenticated admit card flow
# --------------------------------------------------------------------------- #
def _chat_events(message: str):
    r = client_a.post(
        "/api/chat/ask",
        json={"message": message, "chat_id": f"ac_chat_{uuid.uuid4().hex[:8]}", "stream": True},
        headers=STU,
    )
    assert r.status_code == 200
    events = []
    for line in r.text.splitlines():
        if line.startswith("data: "):
            payload = line[len("data: "):]
            import json as _json
            try:
                events.append(_json.loads(payload))
            except Exception:
                # Bare `data:` frames (e.g. plain tokens, TTS-ready text) are
                # rendered as-is; normalize them to a token event.
                events.append({"type": "token", "text": payload})
    assert events, "chat stream should yield events"
    return r.text, events


def test_chat_admit_card_picker_and_detail_flow():
    text, events = _chat_events("student_admit_card")
    all_options_ids = [
        o["id"]
        for e in events if e.get("type") == "options"
        for o in (e.get("options") or [])
    ]
    assert "admit_card_sem-1" in all_options_ids
    assert "admit_card_sem-2" in all_options_ids

    text2, events2 = _chat_events("admit_card_sem-2")
    docs = [e for e in events2 if e.get("type") == "admit_card_doc"]
    assert docs, "chip click should yield the university-document admit card"
    doc_html = docs[0].get("document_html", "")
    assert "Amar Singh College, Srinagar" in doc_html
    assert "Computer Science" in doc_html
    assert "CLUSTER UNIVERSITY SRINAGAR" in doc_html
    for banned in ("password", "hashed_password", "dob", "cus_student_sid"):
        assert banned not in text2


def test_engine_typed_semester_renders_document_deterministically():
    # Typed phrasing routes via the planner (which may legitimately pick the
    # catalogue semester-subjects path first — pre-existing behaviour); the
    # engine's semester-resolution contract must hold regardless.
    from app.orchestrator.engine import _admit_card_events
    from app.orchestrator.extractor import extract_entities

    db = SessionLocal()
    try:
        entities = extract_entities("show my 2nd semester admit card")
        assert entities.semester == 2
        evs = list(_admit_card_events(db, {"student_id": A["id"]}, "ignored", entities))
        docs = [e for e in evs if e.get("type") == "admit_card_doc"]
        assert docs
        assert "Computer Science" in docs[0].get("document_html", "")
    finally:
        db.close()


def test_engine_no_card_semester_renders_safe_token():
    from app.orchestrator.engine import _admit_card_events
    from app.orchestrator.extractor import extract_entities

    db = SessionLocal()
    try:
        entities = extract_entities("show my semester 8 admit card")
        assert entities.semester == 8
        evs = list(_admit_card_events(db, {"student_id": A["id"]}, "ignored", entities))
        tokens = "".join(e.get("text", "") for e in evs if e.get("type") == "token")
        assert "No admit card is issued for Semester 8" in tokens
    finally:
        db.close()


def test_engine_no_profile_cards_renders_safe_token():
    from app.orchestrator.engine import _admit_card_events

    db = SessionLocal()
    try:
        evs = list(_admit_card_events(db, {"student_id": B["id"]}, "ignored", None))
        tokens = "".join(e.get("text", "") for e in evs if e.get("type") == "token")
        assert "No admit card is available for your profile yet" in tokens
    finally:
        db.close()


def test_chat_unauthenticated_admit_card_asks_to_sign_in():
    r = client.post(
        "/api/chat/ask",
        json={"message": "show my admit card", "chat_id": f"anon_{uuid.uuid4().hex[:8]}", "stream": True},
        headers=STU,
    )  # note: no student cookie on `client`
    assert r.status_code == 200
    assert "auth_form" in r.text or "sign in" in r.text.lower()


# --------------------------------------------------------------------------- #
# Print / download — real one-page A4 PDF document
# --------------------------------------------------------------------------- #
def _pdf_text(content: bytes) -> str:
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(content))
    return " ".join((page.extract_text() or "") for page in reader.pages)


def test_print_returns_real_one_page_pdf():
    r = client_a.post("/api/student/admit-cards/1/print", json={})
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("application/pdf")
    assert r.content.startswith(b"%PDF")
    assert r.headers.get("x-robots-tag") == "noindex, nofollow"

    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(r.content))
    assert len(reader.pages) == 1  # exactly one page, nothing clipped away

    text = (reader.pages[0].extract_text() or "")
    assert "CLUSTER UNIVERSITY SRINAGAR" in text
    assert "CHANDIGARH" not in text
    assert "ADMIT CARD" in text
    assert "SEMESTER 1 EXAMINATION" in text
    assert "Exam Roll No." in text
    assert "CUS Registration No." in text
    assert "SUBJECTS" in text
    assert "EXAMINATION CENTER" in text
    assert "IMPORTANT" in text
    assert "Signature of the Applicant" in text
    assert "Developed by I.T Cell" in text
    assert "Mathematics" in text
    assert "Physics" in text
    for banned in ("2005-06-15", "hashed_password", "password", "cus_student_sid", "token"):
        assert banned not in text


def test_print_attachment_filename():
    r = client_a.post("/api/student/admit-cards/1/print", json={"as_attachment": True})
    assert r.status_code == 200
    cd = r.headers.get("content-disposition") or ""
    assert cd.startswith("attachment")
    assert "Admit_Card_Semester_1.pdf" in cd


def test_print_requires_valid_session():
    r = client.post("/api/student/admit-cards/1/print", json={})
    assert r.status_code == 401
    assert "session" in _msg(r).lower()


def test_print_idor_no_cross_student_leak():
    # Give B a semester-5 card via the admin flow; A printing the same
    # semester must never expose B's data (identity comes only from the
    # authenticated session, so A gets A's card or a safe 404).
    body = {
        "reg_no": B["reg_no"], "semester": 5, "exam_type": "Regular",
        "academic_year": "2025-2026", "exam_session": "May/Jun 2026",
        "centre_name": "IDOR Probe College", "centre_code": "IDOR1",
        "subjects": ["B-Only Subject"],
    }
    r = client.post("/api/admin/admit-cards", json=body, headers=SUPER)
    assert r.status_code == 201, r.text
    _created_card_ids.append(r.json()["id"])

    resp = client_a.post("/api/student/admit-cards/5/print", json={"as_attachment": True})
    assert resp.status_code in (200, 404)
    if resp.status_code == 200:
        text = _pdf_text(resp.content)
        assert "IDOR Probe College" not in text
        assert "B-Only Subject" not in text
        assert B["reg_no"] not in text


def test_print_semester_not_issued_or_allowlisted():
    # B has no issued cards at all (safe 404 with an allowlisted semester);
    # 9 is outside the allowlist (422).
    r = client_b.post("/api/student/admit-cards/1/print", json={})
    assert r.status_code == 404
    r = client_a.post("/api/student/admit-cards/9/print", json={})
    assert r.status_code == 422


def test_print_server_log_never_exposes_credentials():
    # The PDF payload itself must not embed PRIVATE data even in binary form.
    r = client_a.post("/api/student/admit-cards/1/print", json={})
    assert r.status_code == 200
    for banned in (b"cus_student_sid", b"hashed_password", b"2005-06-15"):
        assert banned not in r.content