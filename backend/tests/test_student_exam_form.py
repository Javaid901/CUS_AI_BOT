"""
backend/tests/test_student_exam_form.py

Phase D battery — Student Exam Form (authenticated "my exam form" +
Super-Admin structured management: create/edit/withdraw/status + CSV/XLSX
import with preview → confirm → single-transaction apply).

  1. Student side (server-validated session cookie):
       - unauthenticated / expired / revoked / inactive sessions → 401
       - own forms only; fill (new Pending) → submit (Pending → Submitted)
       - a student can never express fee/payment fields (server owns them);
         the student DTO is an explicit allowlist (no reg_no / transaction_id)
       - IDOR: cross-student access blocked even with tampered params/body
  2. Super Admin side (require_superadmin everywhere):
       - list; create / update / status / withdraw; payment fields admin-only
       - import preview (writes nothing); confirm (atomic); duplicates rejected
         (in-file + DB); audit records only filename/reg + count
  3. Chat flow: authenticated hub option → form picker → structured detail;
       chip routing is deterministic; unauthenticated → auth_form.
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
from app.models import AuditLog, Student, StudentExamForm, StudentSession, User
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
_created_form_ids: list[str] = []

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
            username = f"__ef_{key}_{uuid.uuid4().hex[:6]}"
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
        for fid in _created_form_ids:
            db.query(StudentExamForm).filter(StudentExamForm.id == str(fid)).delete()
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
        "reg_no": reg or f"CUS-EF-{uuid.uuid4().hex[:6].upper()}",
        "name": "Exam Form Student",
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


def _seed_form(db, student_id: str, semester: int = 1, **over) -> str:
    r = StudentExamForm(
        id=uuid.uuid4(),
        student_id=uuid.UUID(student_id),
        semester=semester,
        exam_type="Regular",
        form_status="Pending",
        subjects=json.dumps(["Mathematics", "Physics"]),
        academic_year="2023-2024",
    )
    for k, v in over.items():
        setattr(r, k, v)
    db.add(r)
    db.commit()
    _created_form_ids.append(str(r.id))
    return str(r.id)


def _login(client_: TestClient, reg: str, dob: str = "2005-06-15") -> None:
    r = client_.post("/api/student/verify", json={"reg_no": reg, "dob": dob})
    assert r.status_code == 200, r.text


def _msg(r):
    return r.json()["error"]["message"]


_DEFAULT_HEADERS = [
    "registration_number", "semester", "exam_type", "academic_year", "subjects",
    "form_status", "fee_status", "fee_amount", "transaction_id", "submission_date",
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
        "/api/admin/exam-forms/preview",
        files={"file": (filename, content, mime)},
        headers=SUPER,
    )


def _confirm(raw_rows, filename: str = "c.csv"):
    return client.post(
        "/api/admin/exam-forms/confirm",
        json={"filename": filename, "rows": raw_rows},
        headers=SUPER,
    )


def _raw_dicts(row_lists):
    return [dict(zip(_DEFAULT_HEADERS, row)) for row in row_lists]


def _count_forms():
    db = SessionLocal()
    try:
        return db.query(StudentExamForm).count()
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Setup used across tests
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module", autouse=True)
def _seed_people():
    global A, B
    A.update(_create_student(reg="CUS-EF-A0001"))
    B.update(_create_student(reg="CUS-EF-B0001"))
    db = SessionLocal()
    try:
        # A: Regular forms for semesters 1 & 2 (drives picker + detail).
        _seed_form(db, A["id"], semester=1)
        _seed_form(db, A["id"], semester=2, subjects=json.dumps(["Computer Science", "English"]),
                   academic_year="2024-2025")
        # B: one form (used for IDOR cross-access tests).
        _seed_form(db, B["id"], semester=3, subjects=json.dumps(["Zoology"]))
    finally:
        db.close()
    _login(client_a, A["reg_no"])
    _login(client_b, B["reg_no"])


# --------------------------------------------------------------------------- #
# Student side — authn / authz
# --------------------------------------------------------------------------- #
def test_exam_forms_requires_valid_session():
    r = client.get("/api/student/exam-forms")
    assert r.status_code == 401
    assert "session" in _msg(r).lower()


def test_authed_student_lists_own_forms():
    r = client_a.get("/api/student/exam-forms")
    assert r.status_code == 200
    forms = r.json()["forms"]
    assert {(f["semester"], f["exam_type"]) for f in forms} == {(1, "Regular"), (2, "Regular")}


def test_authed_picker_payload_is_safe_allowlist():
    r = client_a.get("/api/student/exam-forms")
    assert r.status_code == 200
    form = r.json()["forms"][0]
    allowed = {"id", "semester", "exam_type", "academic_year", "form_status"}
    assert set(form.keys()) == allowed
    blob = r.text
    for banned in ("student_id", "reg_no", "transaction_id", "dob", "hashed_password", "token", "password"):
        assert banned not in blob


def test_no_form_student_gets_only_own_forms():
    # B has one seeded form (semester 3) and must never see A's forms.
    r = client_b.get("/api/student/exam-forms")
    assert r.status_code == 200
    forms = r.json()["forms"]
    assert {(f["semester"], f["exam_type"]) for f in forms} == {(3, "Regular")}


def test_expired_session_rejected():
    dto = _create_student(reg="CUS-EF-EXP901")
    db = SessionLocal()
    try:
        stu = db.query(Student).filter(Student.id == uuid.UUID(dto["id"])).first()
        raw = create_session(db, stu, ttl_minutes=1)
        row = db.query(StudentSession).filter(StudentSession.student_id == stu.id).first()
        row.expires_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()
    r = client.get("/api/student/exam-forms", cookies={"cus_student_sid": raw})
    assert r.status_code == 401
    db = SessionLocal()
    try:
        stu = db.query(Student).filter(Student.id == uuid.UUID(dto["id"])).first()
        db.delete(stu)
        db.commit()
    finally:
        db.close()


def test_revoked_session_rejected():
    dto = _create_student(reg="CUS-EF-REV902")
    db = SessionLocal()
    try:
        stu = db.query(Student).filter(Student.id == uuid.UUID(dto["id"])).first()
        raw = create_session(db, stu, ttl_minutes=10)
        revoke_session(db, raw)
    finally:
        db.close()
    r = client.get("/api/student/exam-forms", cookies={"cus_student_sid": raw})
    assert r.status_code == 401
    db = SessionLocal()
    try:
        stu = db.query(Student).filter(Student.id == uuid.UUID(dto["id"])).first()
        db.delete(stu)
        db.commit()
    finally:
        db.close()


def test_inactive_student_session_rejected():
    dto = _create_student(reg="CUS-EF-IA900")
    db = SessionLocal()
    try:
        stu = db.query(Student).filter(Student.id == uuid.UUID(dto["id"])).first()
        raw = create_session(db, stu, ttl_minutes=10)
        stu.is_active = False
        stu.status = "deactivated"
        db.commit()
    finally:
        db.close()
    r = client.get("/api/student/exam-forms", cookies={"cus_student_sid": raw})
    assert r.status_code == 401
    db = SessionLocal()
    try:
        stu = db.query(Student).filter(Student.id == uuid.UUID(dto["id"])).first()
        db.delete(stu)
        db.commit()
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Student Fill / Submit workflow
# --------------------------------------------------------------------------- #
def test_student_fill_creates_pending_form():
    r = client_a.post("/api/student/exam-forms", json={
        "semester": 4, "exam_type": "Regular", "academic_year": "2025-2026",
        "subjects": ["Mathematics", "English"],
    })
    assert r.status_code == 201, r.text
    form = r.json()
    assert form["form_status"] == "Pending"
    assert form["submission_date"] == ""
    assert set(form["subjects"]) == {"Mathematics", "English"}
    assert form["fee_status"] == "Unpaid"
    assert form["fee_amount"] is None
    _created_form_ids.append(form["id"])


def test_student_fill_ignores_forged_admin_fields():
    r = client_a.post("/api/student/exam-forms", json={
        "semester": 5, "exam_type": "Regular",
        "fee_status": "Paid", "transaction_id": "TXN-FORGED",
        "form_status": "Approved", "submission_date": "01-Jul-2026",
        "fee_amount": 999999,
    })
    assert r.status_code == 201, r.text
    form = r.json()
    # None of the admin-only fields may leak through the student DTO.
    for banned in ("transaction_id", "student_id", "reg_no"):
        assert banned not in form
    assert form["form_status"] == "Pending"
    assert form["fee_status"] == "Unpaid"
    assert form["fee_amount"] is None
    assert form["submission_date"] == ""
    _created_form_ids.append(form["id"])


def test_student_fill_duplicate_rejected():
    # Resubmit the exact identity (semester 4 / Regular / 2025-2026) → 409.
    r = client_a.post("/api/student/exam-forms", json={
        "semester": 4, "exam_type": "Regular", "academic_year": "2025-2026",
    })
    assert r.status_code == 409, r.text
    assert "already" in _msg(r).lower()


def test_student_fill_invalid_exam_type_rejected():
    r = client_a.post("/api/student/exam-forms", json={"semester": 5, "exam_type": "Supplementary"})
    assert r.status_code == 422


def test_student_fill_invalid_semester_rejected():
    r = client_a.post("/api/student/exam-forms", json={"semester": 9, "exam_type": "Regular"})
    assert r.status_code == 422
    assert "semester" in _msg(r).lower()


def test_student_submit_transitions_and_stamps_date():
    # The seeded semester-2 form is still Pending → submit it.
    r = client_a.get("/api/student/exam-forms")
    f2 = next(f for f in r.json()["forms"] if f["semester"] == 2)
    r = client_a.post(f"/api/student/exam-forms/{f2['id']}/submit", json={"confirm": True})
    assert r.status_code == 200, r.text
    form = r.json()
    assert form["form_status"] == "Submitted"
    assert form["submission_date"]  # stamped server-side


def test_student_submit_requires_confirm_flag():
    r = client_a.get("/api/student/exam-forms")
    f1 = next(f for f in r.json()["forms"] if f["semester"] == 1)
    r = client_a.post(f"/api/student/exam-forms/{f1['id']}/submit", json={"confirm": False})
    assert r.status_code == 422
    assert "confirmation" in _msg(r).lower()


def test_student_resubmit_rejected():
    r = client_a.get("/api/student/exam-forms")
    f2 = next(f for f in r.json()["forms"] if f["semester"] == 2)
    r = client_a.post(f"/api/student/exam-forms/{f2['id']}/submit", json={"confirm": True})
    assert r.status_code == 409
    assert "already been submitted" in _msg(r).lower()


def test_student_submit_unknown_form_404():
    r = client_a.post(f"/api/student/exam-forms/{uuid.uuid4()}/submit", json={"confirm": True})
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# Print flow (own form id) + IDOR
# --------------------------------------------------------------------------- #
def test_student_print_own_form_safe_allowlist():
    r = client_a.get("/api/student/exam-forms")
    f1 = next(f for f in r.json()["forms"] if f["semester"] == 1)
    r = client_a.get(f"/api/student/exam-forms/{f1['id']}/print")
    assert r.status_code == 200
    form = r.json()["form"]
    allowed = {"id", "semester", "exam_type", "academic_year", "subjects",
               "form_status", "submission_date", "fee_status", "fee_amount"}
    assert set(form.keys()) == allowed
    blob = r.text
    for banned in ("transaction_id", "student_id", "reg_no", "dob", "hashed_password", "token", "password"):
        assert banned not in blob
    assert set(form["subjects"]) == {"Mathematics", "Physics"}


def test_student_print_unknown_form_404():
    r = client_a.get(f"/api/student/exam-forms/{uuid.uuid4()}/print")
    assert r.status_code == 404


def test_idor_print_other_student_form_forbidden():
    db = SessionLocal()
    try:
        b_form = (db.query(StudentExamForm)
                  .filter(StudentExamForm.student_id == uuid.UUID(B["id"])).first())
        b_id = str(b_form.id)
    finally:
        db.close()
    r = client_a.get(f"/api/student/exam-forms/{b_id}/print")
    assert r.status_code == 403


def test_idor_submit_other_student_form_forbidden():
    db = SessionLocal()
    try:
        b_form = (db.query(StudentExamForm)
                  .filter(StudentExamForm.student_id == uuid.UUID(B["id"])).first())
        b_id = str(b_form.id)
    finally:
        db.close()
    r = client_a.post(f"/api/student/exam-forms/{b_id}/submit", json={"confirm": True})
    assert r.status_code == 403
    assert "not authorized" in _msg(r).lower()


def test_idor_fill_body_tamper_still_uses_session_identity():
    r = client_a.post("/api/student/exam-forms", json={
        "semester": 6, "exam_type": "Regular",
        "student_id": B["id"], "reg_no": B["reg_no"],
    })
    assert r.status_code == 201, r.text
    form = r.json()
    _created_form_ids.append(form["id"])
    # The tampered identity fields are structurally ignored: the form belongs
    # to A (the session holder), so B must NOT see it.
    rb = client_b.get("/api/student/exam-forms")
    assert rb.status_code == 200
    assert all(f["semester"] != 6 for f in rb.json()["forms"])


# --------------------------------------------------------------------------- #
# Super-Admin management + import
# --------------------------------------------------------------------------- #
def test_admin_exam_forms_authz():
    assert client.get("/api/admin/exam-forms", headers=ADMIN).status_code == 403
    assert client.get("/api/admin/exam-forms", headers=STU).status_code == 403
    assert client.get("/api/admin/exam-forms").status_code == 401
    r = client.get("/api/admin/exam-forms", headers=SUPER)
    assert r.status_code == 200
    assert any(x["reg_no"] == "CUS-EF-A0001" and x["semester"] == 1 for x in r.json()["exam_forms"])

    payload = {"filename": "x.csv", "rows": []}
    assert client.post("/api/admin/exam-forms/preview", files={"file": ("x.csv", b"a,b", "text/csv")}, headers=ADMIN).status_code == 403
    assert client.post("/api/admin/exam-forms/confirm", json=payload, headers=ADMIN).status_code == 403
    assert client.post("/api/admin/exam-forms/confirm", json=payload).status_code == 401
    assert client.post("/api/admin/exam-forms", json={"reg_no": "CUS-EF-A0001", "semester": 4}, headers=ADMIN).status_code == 403
    assert client.delete("/api/admin/exam-forms/does-not-exist", headers=ADMIN).status_code == 403


def test_admin_create_form_roundtrip_with_payment_fields():
    body = {
        "reg_no": "CUS-EF-A0001",
        "semester": 7,
        "exam_type": "Regular",
        "academic_year": "2025-2026",
        "subjects": ["Zoology", "Botany"],
        "form_status": "Pending",
        "fee_status": "Paid",
        "fee_amount": 2500,
        "transaction_id": "TXN-ADMIN-7",
    }
    r = client.post("/api/admin/exam-forms", json=body, headers=SUPER)
    assert r.status_code == 201, r.text
    form = r.json()
    assert form["reg_no"] == "CUS-EF-A0001"
    assert form["fee_status"] == "Paid"
    assert form["fee_amount"] == 2500
    assert form["transaction_id"] == "TXN-ADMIN-7"
    assert set(form["subjects"]) == {"Zoology", "Botany"}
    _created_form_ids.append(form["id"])

    # Student sees the provisioned form but never the payment reference.
    rs = client_a.get("/api/student/exam-forms")
    ids = {f["semester"]: f for f in rs.json()["forms"]}
    assert ids[7]["form_status"] == "Pending"
    assert "transaction_id" not in ids[7]


def test_admin_create_duplicate_rejected():
    body = {"reg_no": "CUS-EF-A0001", "semester": 1, "exam_type": "Regular", "academic_year": "2023-2024"}
    r = client.post("/api/admin/exam-forms", json=body, headers=SUPER)
    assert r.status_code == 409, r.text
    assert "already" in _msg(r).lower()


def test_admin_create_unknown_student_rejected():
    body = {"reg_no": "CUS-EF-NOPE9", "semester": 1}
    r = client.post("/api/admin/exam-forms", json=body, headers=SUPER)
    assert r.status_code == 404
    assert "not found" in _msg(r).lower()


def test_admin_create_invalid_semester_rejected():
    body = {"reg_no": "CUS-EF-A0001", "semester": 9}
    r = client.post("/api/admin/exam-forms", json=body, headers=SUPER)
    assert r.status_code == 422


def test_admin_create_invalid_exam_type_rejected():
    body = {"reg_no": "CUS-EF-A0001", "semester": 1, "exam_type": "Supplementary"}
    r = client.post("/api/admin/exam-forms", json=body, headers=SUPER)
    assert r.status_code == 422
    assert "exam type" in _msg(r).lower()


def test_admin_update_form():
    body = {"reg_no": "CUS-EF-A0001", "semester": 8, "exam_type": "Regular", "fee_status": "Unpaid"}
    r = client.post("/api/admin/exam-forms", json=body, headers=SUPER)
    assert r.status_code == 201, r.text
    form_id = r.json()["id"]
    _created_form_ids.append(form_id)

    r = client.patch(f"/api/admin/exam-forms/{form_id}", json={
        "fee_status": "Paid", "fee_amount": 2200, "transaction_id": "TXN-UPD",
    }, headers=SUPER)
    assert r.status_code == 200, r.text
    updated = r.json()
    assert updated["fee_status"] == "Paid"
    assert updated["fee_amount"] == 2200
    assert updated["transaction_id"] == "TXN-UPD"

    # Changing identity to collide with the seeded semester-1 form → 409.
    r = client.patch(
        f"/api/admin/exam-forms/{form_id}",
        json={"semester": 1, "exam_type": "Regular", "academic_year": "2023-2024"},
        headers=SUPER,
    )
    assert r.status_code == 409
    assert "already" in _msg(r).lower()

    # Unknown id → 404
    r = client.patch(f"/api/admin/exam-forms/{uuid.uuid4()}", json={"fee_status": "Paid"}, headers=SUPER)
    assert r.status_code == 404


def test_admin_update_invalid_fields_rejected():
    body = {"reg_no": "CUS-EF-A0001", "semester": 6, "exam_type": "Backlog"}
    r = client.post("/api/admin/exam-forms", json=body, headers=SUPER)
    assert r.status_code == 201, r.text
    form_id = r.json()["id"]
    _created_form_ids.append(form_id)

    r = client.patch(f"/api/admin/exam-forms/{form_id}", json={"semester": 31}, headers=SUPER)
    assert r.status_code == 422
    r = client.patch(f"/api/admin/exam-forms/{form_id}", json={"exam_type": "Supplementary"}, headers=SUPER)
    assert r.status_code == 422


def test_admin_form_status_endpoint():
    # Target the pre-seeded semester-2 form (submitted during the student flow).
    r2 = client.get("/api/admin/exam-forms?semester=2&page_size=100", headers=SUPER)
    assert r2.status_code == 200
    target = next(x for x in r2.json()["exam_forms"] if x["academic_year"] == "2024-2025")
    form_id = target["id"]

    r = client.post(f"/api/admin/exam-forms/{form_id}/status", json={"form_status": "Approved"}, headers=SUPER)
    assert r.status_code == 200, r.text
    assert r.json()["form_status"] == "Approved"

    r = client.post(f"/api/admin/exam-forms/{form_id}/status", json={"form_status": "Rejected"}, headers=SUPER)
    assert r.status_code == 200
    assert r.json()["form_status"] == "Rejected"

    r = client.post(f"/api/admin/exam-forms/{form_id}/status", json={"form_status": "Fu"}, headers=SUPER)
    assert r.status_code == 422


def test_admin_delete_form_withdraws_for_student():
    body = {"reg_no": "CUS-EF-A0001", "semester": 3, "exam_type": "Regular"}
    r = client.post("/api/admin/exam-forms", json=body, headers=SUPER)
    assert r.status_code == 201, r.text
    form_id = r.json()["id"]

    assert any(f["semester"] == 3 for f in client_a.get("/api/student/exam-forms").json()["forms"])

    r = client.delete(f"/api/admin/exam-forms/{form_id}", headers=SUPER)
    assert r.status_code == 200, r.text
    assert r.json()["deleted"] is True

    assert all(f["semester"] != 3 for f in client_a.get("/api/student/exam-forms").json()["forms"])

    r = client.delete(f"/api/admin/exam-forms/{form_id}", headers=SUPER)
    assert r.status_code == 404


def test_admin_list_filters_and_paginates():
    r = client.get("/api/admin/exam-forms?q=CUS-EF-A0001&semester=1&page=1&page_size=5", headers=SUPER)
    assert r.status_code == 200
    data = r.json()
    assert data["total"] >= 1
    assert all(x["semester"] == 1 for x in data["exam_forms"])
    assert all(x["reg_no"] == "CUS-EF-A0001" for x in data["exam_forms"])
    assert "id" in data["exam_forms"][0]
    assert "subjects" in data["exam_forms"][0]

    r = client.get("/api/admin/exam-forms?q=Zoo&form_status=Pending&page_size=5", headers=SUPER)
    assert r.status_code == 200

    r = client.get("/api/admin/exam-forms?semester=9&page_size=5", headers=SUPER)
    assert r.status_code == 422
    r = client.get("/api/admin/exam-forms?exam_type=Supplementary", headers=SUPER)
    assert r.status_code == 422
    r = client.get("/api/admin/exam-forms?form_status=Fu", headers=SUPER)
    assert r.status_code == 422


def test_preview_valid_csv_resolves_students():
    rows = [
        ["CUS-EF-A0001", 5, "Regular", "2025-2026", "Physics\nChemistry", "Pending", "Paid", 1500, "TXN-IMP-1", ""],
        ["CUS-EF-B0001", 5, "Regular", "2025-2026", "English", "Pending", "Unpaid", "", "", ""],
    ]
    r = _preview(rows, "a.csv")
    assert r.status_code == 200
    data = r.json()
    assert data["total_rows"] == 2
    assert data["valid_count"] == 2
    assert data["error_count"] == 0
    assert data["rows"][0]["registration_number"] == "CUS-EF-A0001"
    assert data["rows"][0]["subjects"] == '["Physics", "Chemistry"]'


def test_preview_rejects_unknown_student():
    rows = [["CUS-EF-NOPE1", 5, "Regular", "", "", "", "", "", "", ""]]
    r = _preview(rows, "a.csv")
    assert r.status_code == 200
    data = r.json()
    assert data["error_count"] == 1
    assert "Unknown registration number" in data["errors"][0]["message"]


def test_preview_reports_row_validation_errors():
    rows = [
        ["", 5, "Regular", "", "", "", "", "", "", ""],                        # no reg
        ["CUS-EF-A0001", "abc", "Regular", "", "", "", "", "", "", ""],        # bad semester
        ["CUS-EF-A0001", 31, "Regular", "", "", "", "", "", "", ""],           # semester 31
        ["CUS-EF-A0001", 5, "Supplementary", "", "", "", "", "", "", ""],      # bad type
        ["CUS-EF-A0001", 5, "Regular", "", "", "Fu", "", "", "", ""],          # bad form status
        ["CUS-EF-A0001", 5, "Regular", "", "", "", "", -5, "", ""],            # negative fee
    ]
    r = _preview(rows, "a.csv")
    assert r.status_code == 200
    data = r.json()
    assert data["error_count"] == 6
    msgs = " ".join(e["message"] for e in data["errors"])
    for frag in ("Registration number is required", "Semester must be a whole number",
                 "not in the allowed semester list", "Exam type must be one of",
                 "Form status must be one of", "Fee amount must be a positive whole number"):
        assert frag in msgs


def test_preview_xlsx_supported():
    rows = [
        ["CUS-EF-A0001", 5, "Regular", "2025-2026", "Economics", "Pending", "", "", "", ""],
    ]
    r = _preview(rows, "b.xlsx")
    assert r.status_code == 200
    assert r.json()["valid_count"] == 1


def test_preview_rejects_unsupported_format():
    r = client.post(
        "/api/admin/exam-forms/preview",
        files={"file": ("c.pdf", b"%PDF-fake", "application/pdf")},
        headers=SUPER,
    )
    assert r.status_code == 400


def test_preview_missing_required_column_reports_header_error():
    rows = [["CUS-EF-A0001", 5]]
    headers = ["registration_number", "semester"]
    r = _preview(rows, "a.csv", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert data["error_count"] == 1
    assert "Missing required column" in data["errors"][0]["message"]
    assert data["valid_count"] == 0


def test_preview_in_file_duplicate_blocked():
    row = ["CUS-EF-A0001", 5, "Regular", "2025-2026", "Physics", "Pending", "", "", "", ""]
    r = _preview([row, row], "dup.csv")
    assert r.status_code == 200
    data = r.json()
    assert data["error_count"] == 1
    assert "Duplicate row in the file" in data["errors"][0]["message"]


def test_preview_writes_nothing():
    before = _count_forms()
    rows = [["CUS-EF-A0001", 5, "Regular", "2025-2026", "Physics", "Pending", "", "", "", ""]]
    r = _preview(rows, "a.csv")
    assert r.status_code == 200 and r.json()["valid_count"] == 1
    assert _count_forms() == before  # preview never writes


def test_confirm_applies_in_single_transaction():
    before = _count_forms()
    rows = [
        ["CUS-EF-A0001", 5, "Regular", "2025-2026", "Physics\nChemistry", "Pending", "Paid", 1500, "TXN-IMP-1", "01-May-2026"],
        ["CUS-EF-B0001", 5, "Backlog", "2025-2026", "Mathematics", "Pending", "Unpaid", "", "", ""],
    ]
    p = _preview(rows, "c.csv")
    assert p.status_code == 200 and p.json()["error_count"] == 0

    r = _confirm(p.json()["raw_rows"], "c.csv")
    assert r.status_code == 200
    assert r.json()["imported"] == 2
    assert _count_forms() == before + 2

    db = SessionLocal()
    try:
        aud = (
            db.query(AuditLog)
            .filter(AuditLog.action == "student_exam_form.import")
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
    before = _count_forms()
    rows = [
        ["CUS-EF-A0001", 5, "Regular", "2025-2026", "Physics", "Pending", "", "", "", ""],
        ["CUS-EF-NOPE9", 5, "Regular", "2025-2026", "Physics", "Pending", "", "", "", ""],
    ]
    r = _confirm(_raw_dicts(rows), "bad.csv")
    assert r.status_code == 422
    assert _count_forms() == before  # nothing written


def test_confirm_db_duplicate_returns_409_and_writes_nothing():
    before = _count_forms()
    # The seeded semester-1 form for A already has this identity → DB collision.
    rows = [
        ["CUS-EF-A0001", 1, "Regular", "2023-2024", "Mathematics", "Pending", "", "", "", ""],
    ]
    r = _confirm(_raw_dicts(rows), "dupdb.csv")
    assert r.status_code == 409
    assert "duplicate" in _msg(r).lower()
    assert _count_forms() == before


def test_admin_audit_rows_written():
    db = SessionLocal()
    try:
        actions = {
            a.action for a in
            db.query(AuditLog).filter(AuditLog.actor_id == _created_user_ids[0]).all()
        }
    finally:
        db.close()
    assert "student_exam_form.create" in actions
    assert "student_exam_form.delete" in actions
    assert "student_exam_form.status_change" in actions


# --------------------------------------------------------------------------- #
# Chat integration — authenticated exam form flow
# --------------------------------------------------------------------------- #
def _chat_events(message: str):
    r = client_a.post(
        "/api/chat/ask",
        json={"message": message, "chat_id": f"ef_chat_{uuid.uuid4().hex[:8]}", "stream": True},
        headers=STU,
    )
    assert r.status_code == 200
    events = []
    for line in r.text.splitlines():
        if line.startswith("data: "):
            payload = line[len("data: "):]
            try:
                events.append(json.loads(payload))
            except Exception:
                events.append({"type": "token", "text": payload})
    assert events, "chat stream should yield events"
    return r.text, events


def test_chat_exam_form_picker_and_detail_flow():
    text, events = _chat_events("student_exam_form")
    all_options_ids = [
        o["id"]
        for e in events if e.get("type") == "options"
        for o in (e.get("options") or [])
    ]
    assert "exam_formregular1" in all_options_ids
    assert "exam_formregular2" in all_options_ids

    text2, events2 = _chat_events("exam_formregular2")
    details = [e for e in events2 if e.get("type") == "detail"]
    assert details, "chip click should yield an exam form detail"
    joined = " ".join(f["label"] + " " + f["value"] for f in details[0].get("fields", []))
    assert "Computer Science" in joined
    for banned in ("password", "hashed_password", "dob", "transaction_id", "cus_student_sid"):
        assert banned not in text2


def test_engine_typed_semester_renders_detail_deterministically():
    from app.orchestrator.engine import _exam_form_events
    from app.orchestrator.extractor import extract_entities

    db = SessionLocal()
    try:
        entities = extract_entities("show my 2nd semester exam form")
        assert entities.semester == 2
        evs = list(_exam_form_events(db, {"student_id": A["id"]}, "ignored", entities))
        details = [e for e in evs if e.get("type") == "detail"]
        assert details
        joined = " ".join(f["label"] + " " + f["value"] for f in details[0].get("fields", []))
        assert "English" in joined
        assert "Semester 2" in details[0].get("title", "")
    finally:
        db.close()


def test_engine_no_form_semester_renders_safe_token():
    from app.orchestrator.engine import _exam_form_events
    from app.orchestrator.extractor import extract_entities

    db = SessionLocal()
    try:
        entities = extract_entities("show my semester 9 exam form")
        assert entities.semester == 9
        evs = list(_exam_form_events(db, {"student_id": A["id"]}, "ignored", entities))
        tokens = "".join(e.get("text", "") for e in evs if e.get("type") == "token")
        assert "No exam form is available for Regular · Semester 9" in tokens
    finally:
        db.close()


def test_engine_no_profile_forms_renders_safe_token():
    from app.orchestrator.engine import _exam_form_events

    db = SessionLocal()
    try:
        # Use a brand-new student with zero forms.
        dto = _create_student(reg="CUS-EF-EMPTY1")
        evs = list(_exam_form_events(db, {"student_id": dto["id"]}, "ignored", None))
        tokens = "".join(e.get("text", "") for e in evs if e.get("type") == "token")
        assert "No Exam Form is available for your profile yet" in tokens
    finally:
        db.close()


def test_chat_unauthenticated_exam_form_asks_to_sign_in():
    r = client.post(
        "/api/chat/ask",
        json={"message": "show my exam form", "chat_id": f"anon_{uuid.uuid4().hex[:8]}", "stream": True},
        headers=STU,
    )  # note: no student cookie on `client`
    assert r.status_code == 200
    assert "auth_form" in r.text or "sign in" in r.text.lower()


# --------------------------------------------------------------------------- #
# Phase D2 — Exam Session model: fill / eligibility gate / payment / document
# --------------------------------------------------------------------------- #
import re

from datetime import timedelta

from app.models import (
    ExamApplicationSubject,
    ExamEligibility,
    ExamPayment,
    ExamSession,
    StudentAttendance,
    StudentResult,
)

client_c = TestClient(app)

C: dict[str, str] = {}
_SESSION_ID = ""
_DRAFT_SESSION_ID = ""
_ZERO_SESSION_ID = ""

_created_session_ids: list[str] = []
_created_result_ids: list[str] = []
_created_attendance_ids: list[str] = []


def _open_session(code: str, semester: int = 3, base_fee: int = 1500, **over) -> str:
    """Create an OPEN session directly (server-independent test fixture)."""
    now = datetime.now(timezone.utc)
    _created_session_ids.append("__x")  # placeholder removed below
    data = {
        "id": uuid.uuid4(),
        "name": f"Session {code}",
        "code": code,
        "programme": "bca",
        "batch": "2023",
        "semester": semester,
        "exam_type": "Regular",
        "academic_year": "2025-2026",
        "application_open_at": now - timedelta(days=5),
        "last_date_normal": now + timedelta(days=20),
        "last_date_late": now + timedelta(days=35),
        "base_fee": base_fee,
        "late_fee": 300,
        "status": "Open",
        "form_seq": 0,
    }
    data.update(over)
    db = SessionLocal()
    try:
        s = ExamSession(**data)
        db.add(s)
        db.commit()
        db.refresh(s)
        sid = str(s.id)
    finally:
        db.close()
    _created_session_ids[-1] = sid
    return sid


@pytest.fixture(scope="module", autouse=True)
def _seed_session_people():
    """Student C + three sessions. Also seeds C's semester-3 evidence so the
    eligibility gate passes (internals 60% ≥ 40%, attendance 85% ≥ 75%)."""
    global C, _SESSION_ID, _DRAFT_SESSION_ID, _ZERO_SESSION_ID

    db = SessionLocal()
    try:
        from app.catalogue.models import Programme, ProgrammeSubject
        from sqlalchemy import func

        # Hermetic subject derivation: the session-fill tests expect the results-
        # fallback subject path for the exam fixture programme ("bca" -> catalogue
        # code BCA). Catalogue test modules seed BCA semester-3 ProgrammeSubjects
        # into the shared test database ahead of this module, so drop them here to
        # keep subject derivation deterministic regardless of collection order.
        prog_ids = [p.id for p in db.query(Programme).filter(func.lower(Programme.code) == "bca").all()]
        for pid in prog_ids:
            db.query(ProgrammeSubject).filter(ProgrammeSubject.programme_id == pid).delete()
        db.commit()
    finally:
        db.close()

    C.update(_create_student(reg="CUS-EF-C0001", current_semester=3, batch="2023"))
    _login(client_c, C["reg_no"])

    db = SessionLocal()
    try:
        for name, code, internal in (("Data Structures", "BCAD301", 62), ("English-II", "BCAD302", 58)):
            r = StudentResult(
                id=uuid.uuid4(), student_id=uuid.UUID(C["id"]), semester=3,
                exam_type="Regular", subject_name=name, subject_code=code,
                internal_marks=internal, max_marks=100, status="pass",
            )
            db.add(r)
            _created_result_ids.append(str(r.id))
        a = StudentAttendance(
            id=uuid.uuid4(), student_id=uuid.UUID(C["id"]), semester=3,
            subject_name="Data Structures", subject_code="BCAD301", percentage="85",
        )
        db.add(a)
        _created_attendance_ids.append(str(a.id))
        db.commit()
    finally:
        db.close()

    _SESSION_ID = _open_session("TESTBCA3")            # fee 1500
    _ZERO_SESSION_ID = _open_session("TESTBCA0", base_fee=0)  # zero fee
    _open_session_placeholder_draft()                   # Draft session

    yield

    db = SessionLocal()
    try:
        for fid in _created_form_ids:
            db.query(ExamPayment).filter(ExamPayment.form_id == str(fid)).delete()
            db.query(ExamEligibility).filter(ExamEligibility.form_id == str(fid)).delete()
            db.query(ExamApplicationSubject).filter(ExamApplicationSubject.form_id == str(fid)).delete()
            db.query(StudentExamForm).filter(StudentExamForm.id == str(fid)).delete()
        for sid in _created_session_ids:
            db.query(ExamPayment).filter(ExamPayment.session_id == str(sid)).delete()
            db.query(ExamEligibility).filter(ExamEligibility.session_id == str(sid)).delete()
            db.query(ExamSession).filter(ExamSession.id == str(sid)).delete()
        for rid in _created_result_ids:
            db.query(StudentResult).filter(StudentResult.id == str(rid)).delete()
        for aid in _created_attendance_ids:
            db.query(StudentAttendance).filter(StudentAttendance.id == str(aid)).delete()
        db.commit()
    finally:
        db.close()


def _open_session_placeholder_draft():
    global _DRAFT_SESSION_ID
    now = datetime.now(timezone.utc)

    def _mk(code):
        return ExamSession(
            id=uuid.uuid4(), name=f"Session {code}", code=code, programme="bca",
            batch="2023", semester=3, exam_type="Regular", academic_year="2025-2026",
            application_open_at=now - timedelta(days=5),
            last_date_normal=now + timedelta(days=20),
            last_date_late=now + timedelta(days=35),
            base_fee=1500, late_fee=300, status="Draft", form_seq=0,
        )

    db = SessionLocal()
    try:
        s = _mk("TESTDRAFT3")
        db.add(s)
        db.commit()
        db.refresh(s)
        _DRAFT_SESSION_ID = str(s.id)
        _created_session_ids.append(_DRAFT_SESSION_ID)
    finally:
        db.close()


def _fill_session(session_id: str, student_client: TestClient = None) -> dict:
    sc = student_client or client_c
    r = sc.post("/api/student/exam-forms", json={"exam_session_id": session_id})
    assert r.status_code == 201, r.text
    form = r.json()
    _created_form_ids.append(form["id"])
    return form


def _new_eligible_bca_client(reg: str) -> TestClient:
    """A fresh BCA semester-3 student with internals+attendance seeded so the
    eligibility gate passes, and NO existing exam form (so the Fill flow can
    reach the session picker legitimately)."""
    st = _create_student(reg=reg, current_semester=3, batch="2023")
    cl = TestClient(app)
    _login(cl, st["reg_no"])
    db = SessionLocal()
    try:
        for name, code, internal in (("Data Structures", "BCAD301", 62), ("English-II", "BCAD302", 58)):
            r = StudentResult(
                id=uuid.uuid4(), student_id=uuid.UUID(st["id"]), semester=3,
                exam_type="Regular", subject_name=name, subject_code=code,
                internal_marks=internal, max_marks=100, status="pass",
            )
            db.add(r)
            _created_result_ids.append(str(r.id))
        a = StudentAttendance(
            id=uuid.uuid4(), student_id=uuid.UUID(st["id"]), semester=3,
            subject_name="Data Structures", subject_code="BCAD301", percentage="85",
        )
        db.add(a)
        _created_attendance_ids.append(str(a.id))
        db.commit()
    finally:
        db.close()
    return cl


def test_session_picker_shows_only_matching_open_sessions():
    r = client_c.get("/api/student/exam-forms/sessions")
    assert r.status_code == 200
    codes = {s["code"] for s in r.json()["sessions"]}
    assert "TESTBCA3" in codes
    assert "TESTDRAFT3" not in codes  # Draft sessions are never visible
    ra = client_a.get("/api/student/exam-forms/sessions")
    assert ra.status_code == 200
    # A is current-semester 2 → the semester-3 sessions are profile-blocked.
    assert all(s["semester"] != 3 or s["programme"] != "bca" for s in ra.json()["sessions"])


def test_session_fill_derives_subjects_fee_and_snapshot():
    form = _fill_session(_SESSION_ID)
    assert form["form_status"] == "Pending"
    assert form["semester"] == 3
    assert form["exam_type"] == "Regular"
    assert form["fee_amount"] == 1500
    assert form["fee_status"] == "Unpaid"
    assert "Data Structures" in form["subjects"] and "English-II" in form["subjects"]

    db = SessionLocal()
    try:
        row = db.get(StudentExamForm, uuid.UUID(form["id"]))
        assert re.fullmatch(r"TESTBCA3-3-\d{5}", row.form_no), row.form_no
        assert row.exam_session_id == uuid.UUID(_SESSION_ID)
        assert row.academic_year == "2025-2026"
        snap = json.loads(row.eligibility_snapshot or "{}")
        assert snap["eligible"] is True
        assert any(r_["rule"] == "internals_ok" and r_["passed"] for r_ in snap["rules"])
        elig = db.query(ExamEligibility).filter(ExamEligibility.form_id == row.id).first()
        assert elig is not None and elig.eligible is True
        subs = db.query(ExamApplicationSubject).filter(ExamApplicationSubject.form_id == row.id).all()
        assert {s.subject_name for s in subs} == {"Data Structures", "English-II"}
        assert all(s.source == "system" for s in subs)
    finally:
        db.close()


def test_session_fill_draft_session_blocked():
    r = client_c.post("/api/student/exam-forms", json={"exam_session_id": _DRAFT_SESSION_ID})
    assert r.status_code == 422
    assert "not open" in _msg(r).lower()


def test_session_fill_duplicate_rejected():
    sid = _open_session("TESTBCDUP")
    form = _fill_session(sid)
    r = client_c.post("/api/student/exam-forms", json={"exam_session_id": sid})
    assert r.status_code == 409
    assert "already" in _msg(r).lower()


def test_session_fill_missing_session_422():
    r = client_c.post("/api/student/exam-forms", json={"exam_session_id": "not-a-uuid"})
    assert r.status_code == 404  # malformed → treated as unknown session


def test_session_fill_eligibility_blocked_on_semester_mismatch():
    d = _create_student(reg="CUS-EF-D0001", current_semester=2)
    client_d = TestClient(app)
    r_login = client_d.post("/api/student/verify", json={"reg_no": d["reg_no"], "dob": "2005-06-15"})
    assert r_login.status_code == 200, r_login.text
    r = client_d.post("/api/student/exam-forms", json={"exam_session_id": _SESSION_ID})
    assert r.status_code == 422, r.text
    assert "eligibility" in _msg(r).lower()


def test_session_submit_blocked_while_unpaid():
    sid = _open_session("TESTBCUP1")
    form = _fill_session(sid)
    r = client_c.post(f"/api/student/exam-forms/{form['id']}/submit", json={"confirm": True})
    assert r.status_code == 422
    assert "unpaid" in _msg(r).lower()


def test_session_payment_initiate_confirm_auto_approves():
    sid = _open_session("TESTBCPAY")
    form = _fill_session(sid)

    r = client_c.post(f"/api/student/exam-forms/{form['id']}/payments/initiate")
    assert r.status_code == 200, r.text
    pay = r.json()
    assert pay["status"] == "initiated"
    assert pay["amount"] == 1500
    assert pay["form_id"] == form["id"]

    r = client_c.post(f"/api/student/exam-forms/{form['id']}/payments/{pay['id']}/confirm")
    assert r.status_code == 200, r.text
    pay2 = r.json()
    assert pay2["status"] == "success"
    assert pay2["gateway_ref"]

    # A successful payment auto-approves the form (no manual admin approval)
    # and stamps the submission date in the project's canonical format.
    db = SessionLocal()
    try:
        row = db.get(StudentExamForm, uuid.UUID(form["id"]))
        assert row.form_status == "Approved"
        assert row.fee_status == "Paid"
        assert row.fee_amount == 1500
        assert row.transaction_id == pay2["gateway_ref"]
        assert row.submission_date == datetime.now().strftime("%d-%b-%Y")
        assert row.printed_at is None  # untouched by payment
    finally:
        db.close()

    # The old "confirm → submit" step is superseded: the affirmed form is already
    # in its final Approved state, so a further submit is rejected idempotently.
    r = client_c.post(f"/api/student/exam-forms/{form['id']}/submit", json={"confirm": True})
    assert r.status_code == 409
    assert "already" in _msg(r).lower()

    # Repeated confirm stays idempotent (same success payment, state untouched).
    r = client_c.post(f"/api/student/exam-forms/{form['id']}/payments/{pay['id']}/confirm")
    assert r.status_code == 200
    assert r.json()["status"] == "success"
    db = SessionLocal()
    try:
        row = db.get(StudentExamForm, uuid.UUID(form["id"]))
        assert row.form_status == "Approved" and row.fee_status == "Paid"
        count = db.query(ExamPayment).filter(
            ExamPayment.form_id == row.id, ExamPayment.status == "success"
        ).count()
        assert count == 1
    finally:
        db.close()


def test_session_payment_history_and_idor():
    sid = _open_session("TESTBCHIST")
    form = _fill_session(sid)
    client_c.post(f"/api/student/exam-forms/{form['id']}/payments/initiate")
    r = client_c.get(f"/api/student/exam-forms/{form['id']}/payments")
    assert r.status_code == 200
    assert len(r.json()["payments"]) == 1

    # Student B (own jar) must be blocked from C's payment endpoints (IDOR).
    r = client_b.post(f"/api/student/exam-forms/{form['id']}/payments/initiate")
    assert r.status_code == 403
    r = client_b.get(f"/api/student/exam-forms/{form['id']}/document")
    assert r.status_code == 403


def test_session_payment_receipt_access_and_idor():
    sid = _open_session("TESTBCRCP")
    form = _fill_session(sid)

    # 1. No payment yet -> receipt is unavailable (409 on both surfaces).
    assert client_c.get(f"/api/student/exam-forms/{form['id']}/receipt").status_code == 409
    r = client_c.post(f"/api/student/exam-forms/{form['id']}/receipt", json={"as_attachment": True})
    assert r.status_code == 409

    # 2. Cross-student access blocked (IDOR).
    assert client_b.get(f"/api/student/exam-forms/{form['id']}/receipt").status_code == 403
    r = client_b.post(f"/api/student/exam-forms/{form['id']}/receipt", json={"as_attachment": True})
    assert r.status_code == 403

    # 3. Pay -> success.
    r = client_c.post(f"/api/student/exam-forms/{form['id']}/payments/initiate")
    assert r.status_code == 200, r.text
    pay = r.json()
    r = client_c.post(f"/api/student/exam-forms/{form['id']}/payments/{pay['id']}/confirm")
    assert r.status_code == 200, r.text
    pay2 = r.json()

    # 4. HTML receipt renders the successful payment (own view only).
    r = client_c.get(f"/api/student/exam-forms/{form['id']}/receipt")
    assert r.status_code == 200, r.text
    html = r.text
    assert "OFFICIAL PAYMENT RECEIPT" in html
    assert "cluster university srinagar" in html.lower()
    assert "PAID" in html
    assert pay2["gateway_ref"] in html
    db = SessionLocal()
    try:
        row = db.get(StudentExamForm, uuid.UUID(form["id"]))
        assert row.form_no in html  # receipt number = form number
    finally:
        db.close()

    # 5. PDF receipt downloads as an attachment.
    r = client_c.post(f"/api/student/exam-forms/{form['id']}/receipt", json={"as_attachment": True})
    assert r.status_code == 200, r.text
    assert r.headers.get("content-type", "").startswith("application/pdf")
    assert r.content[:5] == b"%PDF-"
    assert "attachment" in r.headers.get("content-disposition", "")
    assert "Fee_Receipt_" in r.headers.get("content-disposition", "")

    # 6. The exam-form document reflects the auto-approved state.
    r = client_c.get(f"/api/student/exam-forms/{form['id']}/document")
    assert r.status_code == 200
    assert "Approved" in r.text


def test_session_zero_fee_auto_paid_and_submittable():
    form = _fill_session(_ZERO_SESSION_ID)
    assert form["fee_status"] == "Paid"
    assert form["fee_amount"] == 0
    db = SessionLocal()
    try:
        row = db.get(StudentExamForm, uuid.UUID(form["id"]))
        pay = db.query(ExamPayment).filter(ExamPayment.form_id == row.id).first()
        assert pay is not None and pay.status == "success" and pay.amount == 0
        assert pay.gateway_ref.startswith("ZERO-")
    finally:
        db.close()
    r = client_c.post(f"/api/student/exam-forms/{form['id']}/submit", json={"confirm": True})
    assert r.status_code == 200
    assert r.json()["form_status"] == "Submitted"


def test_session_document_html_and_pdf_print():
    sid = _open_session("TESTBCDOC")
    form = _fill_session(sid)

    r = client_c.get(f"/api/student/exam-forms/{form['id']}/document")
    assert r.status_code == 200
    html = r.text
    assert "EXAMINATION FORM" in html
    assert "Data Structures" in html
    assert "cluster university srinagar" in html.lower()

    db = SessionLocal()
    try:
        row = db.get(StudentExamForm, uuid.UUID(form["id"]))
        form_no = row.form_no
    finally:
        db.close()
    assert form_no and form_no in html

    r2 = client_c.post(f"/api/student/exam-forms/{form['id']}/print", json={"as_attachment": False})
    assert r2.status_code == 200, r2.text
    assert r2.headers.get("content-type", "").startswith("application/pdf")
    assert r2.content[:5] == b"%PDF-"
    assert "inline" in r2.headers.get("content-disposition", "")

    db = SessionLocal()
    try:
        row = db.get(StudentExamForm, uuid.UUID(form["id"]))
        assert row.printed_at is not None  # stamped on download
    finally:
        db.close()


def test_session_admin_crud_status_and_authz():
    assert client.get("/api/admin/exam-sessions").status_code == 401
    assert client.get("/api/admin/exam-sessions", headers=ADMIN).status_code == 403

    r = client.get("/api/admin/exam-sessions?status=Open&page_size=100", headers=SUPER)
    assert r.status_code == 200
    assert any(s["code"] == "TESTBCA3" for s in r.json()["sessions"])

    body = {
        "name": "End Semester Exam Regular",
        "code": "PG26X", "programme": "mca", "semester": 1,
        "base_fee": 2000, "status": "Draft",
        "application_open_at": "2026-09-01T00:00:00+00:00",
        "last_date_normal": "2026-10-01T00:00:00+00:00",
    }
    r = client.post("/api/admin/exam-sessions", json=body, headers=SUPER)
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    _created_session_ids.append(sid)

    r = client.post("/api/admin/exam-sessions", json=body, headers=SUPER)
    assert r.status_code == 409  # duplicate code

    r = client.patch(f"/api/admin/exam-sessions/{sid}", json={"name": "Renamed"}, headers=SUPER)
    assert r.status_code == 200 and r.json()["name"] == "Renamed"

    r = client.post(f"/api/admin/exam-sessions/{sid}/status", json={"status": "Open"}, headers=SUPER)
    assert r.status_code == 200 and r.json()["status"] == "Open"

    r = client.delete(f"/api/admin/exam-sessions/{sid}", headers=SUPER)
    assert r.status_code == 200

    r = client.delete(f"/api/admin/exam-sessions/{sid}", headers=SUPER)
    assert r.status_code == 404

    # A session with exam forms cannot be deleted (archive instead).
    r = client.delete(f"/api/admin/exam-sessions/{_SESSION_ID}", headers=SUPER)
    assert r.status_code == 409


# --------------------------------------------------------------------------- #
# Phase D2 — chat integration (session-driven Fill / Pay / Document events)
# --------------------------------------------------------------------------- #
def _chat_events_with(client_: TestClient, message: str):
    r = client_.post(
        "/api/chat/ask",
        json={"message": message, "chat_id": f"efc_{uuid.uuid4().hex[:8]}", "stream": True},
        headers=STU,
    )
    assert r.status_code == 200
    events = []
    for line in r.text.splitlines():
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[len("data: "):]))
            except Exception:
                events.append({"type": "token", "text": line[len("data: "):]})
    assert events, "chat stream should yield events"
    return events


def _chat_events_c(message: str):
    return _chat_events_with(client_c, message)


def _chat_option_ids(events) -> list[str]:
    return [
        o["id"]
        for e in events if e.get("type") == "options"
        for o in (e.get("options") or [])
    ]


def test_chat_exam_form_hub_landing_always_shows_both_options():
    # The exam form hub ALWAYS shows BOTH Fill and Print, regardless of whether
    # the student already has an application. (fresh profile, no forms)
    e = _create_student(reg="CUS-EF-E0001", current_semester=3, admission_year=2023)
    client_e = TestClient(app)
    r_login = client_e.post("/api/student/verify", json={"reg_no": e["reg_no"], "dob": "2005-06-15"})
    assert r_login.status_code == 200, r_login.text
    _open_session("TESTCHAT1")
    events = _chat_events_with(client_e, "student_exam_form")
    ids = _chat_option_ids(events)
    assert "exam_form_fill" in ids
    assert "exam_form_print" in ids


def test_chat_exam_form_hub_landing_both_options_with_submitted_form():
    # Even with a submitted/approved form the hub STILL shows BOTH options.
    f = _create_student(reg="CUS-EF-A1001", programme="bba", current_semester=3, admission_year=2023)
    client_f = TestClient(app)
    _login(client_f, f["reg_no"])
    sid_f = _open_session("TESTCHATQA", programme="bba")
    dbx = SessionLocal()
    try:
        _seed_form(
            dbx, f["id"], semester=3,
            exam_session_id=uuid.UUID(sid_f),
            form_no="TESTCHATQA-3-10001",
            form_status="Approved",
            submission_date="10-Sep-2026",
            fee_status="Paid",
            fee_amount=1500,
        )
    finally:
        dbx.close()
    events = _chat_events_with(client_f, "student_exam_form")
    ids = _chat_option_ids(events)
    assert "exam_form_print" in ids
    assert "exam_form_fill" in ids


def test_chat_exam_form_fill_shows_existing_application_not_duplicate():
    # When an already-applied student clicks Fill the EXISTING application is
    # displayed (message + detail + View/Download chips) — a new application
    # is never started.
    f = _create_student(reg="CUS-EF-A1002", programme="bba", current_semester=3, admission_year=2023)
    client_f = TestClient(app)
    _login(client_f, f["reg_no"])
    sid_f = _open_session("TESTCHATQB", programme="bba")
    dbx = SessionLocal()
    try:
        _seed_form(
            dbx, f["id"], semester=3,
            exam_session_id=uuid.UUID(sid_f),
            form_no="TESTCHATQB-3-10001",
            form_status="Approved",
            submission_date="10-Sep-2026",
            fee_status="Paid",
            fee_amount=1500,
        )
    finally:
        dbx.close()
    events = _chat_events_with(client_f, "exam_form_fill")
    texts = [e.get("text", "") for e in events if e.get("type") == "token"]
    joined = " ".join(texts).lower()
    assert "already filled" in joined
    ids = _chat_option_ids(events)
    assert not any(i.startswith("exam_form_pick") for i in ids), "no session picker for an already-applied student"
    assert any(i.startswith("exam_form_view") for i in ids), "existing form must offer View"
    assert any(i.startswith("exam_form_dl") for i in ids), "existing form must offer Print / Download"
    assert any(e.get("type") == "detail" for e in events), "existing form detail must be shown"


def test_chat_exam_form_pick_existing_session_shows_existing_application():
    # Picking an already-applied session from the picker must display the
    # existing application instead of creating a duplicate.
    f = _create_student(reg="CUS-EF-A1003", programme="bba", current_semester=3, admission_year=2023)
    client_f = TestClient(app)
    _login(client_f, f["reg_no"])
    sid_f = _open_session("TESTCHATQC", programme="bba")
    dbx = SessionLocal()
    try:
        _seed_form(
            dbx, f["id"], semester=3,
            exam_session_id=uuid.UUID(sid_f),
            form_no="TESTCHATQC-3-10001",
            form_status="Approved",
            submission_date="10-Sep-2026",
            fee_status="Paid",
            fee_amount=1500,
        )
    finally:
        dbx.close()
    events = _chat_events_with(client_f, "exam_form_picktestchatqc")
    texts = " ".join(e.get("text", "") for e in events if e.get("type") == "token").lower()
    assert "already filled" in texts
    ids = _chat_option_ids(events)
    assert any(i.startswith("exam_form_view") for i in ids)
    assert any(i.startswith("exam_form_dl") for i in ids)


def test_chat_exam_form_print_shows_empty_state():
    # A student with no submitted form gets a clear empty state — no dead print.
    h = _create_student(reg="CUS-EF-H0001", programme="bba", current_semester=3, admission_year=2023)
    client_h = TestClient(app)
    _login(client_h, h["reg_no"])
    _open_session("TESTCHATH", programme="bba")
    events = _chat_events_with(client_h, "exam_form_print")
    texts = [e.get("text", "") for e in events if e.get("type") == "token"]
    assert any("No submitted exam form is available" in t for t in texts)
    assert not any(i.startswith("exam_form_view") for i in _chat_option_ids(events))
    assert not any(i.startswith("exam_form_dl") for i in _chat_option_ids(events))


def test_chat_exam_form_fill_pick_and_pay_event():
    # Regression: a student with NO existing application gets the OPEN-session
    # picker, and picking a session leads to the mock payment flow (unchanged).
    client_d = _new_eligible_bca_client("CUS-EF-D0005")
    _open_session("TESTCHAT2")
    events = _chat_events_with(client_d, "exam_form_fill")
    ids = _chat_option_ids(events)
    pick = next(i for i in ids if i.startswith("exam_form_pick") and "testchat2" in i)

    events2 = _chat_events_with(client_d, pick)
    assert any(e.get("type") == "detail" for e in events2)
    pay_chip = next(a for a in _chat_option_ids(events2) if a.startswith("exam_form_pay"))

    events3 = _chat_events_with(client_d, pay_chip)
    pays = [e for e in events3 if e.get("type") == "exam_form_pay"]
    assert pays and pays[0]["amount"] == 1500
    form_id = pay_chip[len("exam_form_pay"):]
    assert form_id == pays[0]["form_id"]
    _created_form_ids.append(form_id)


def test_chat_exam_form_document_event_renders_form_doc():
    client_e = _new_eligible_bca_client("CUS-EF-E0002")
    _open_session("TESTCHAT3")
    events = _chat_events_with(client_e, "exam_form_fill")
    pick = next(i for i in _chat_option_ids(events) if i.startswith("exam_form_pick") and "testchat3" in i)
    events2 = _chat_events_with(client_e, pick)
    view_chip = next(a for a in _chat_option_ids(events2) if a.startswith("exam_form_view"))

    events3 = _chat_events_with(client_e, view_chip)
    docs = [e for e in events3 if e.get("type") == "exam_form_doc"]
    assert docs, f"should emit exam_form_doc event: {[e.get('type') for e in events3]}"
    assert "EXAMINATION FORM" in docs[0].get("document_html", "")
    assert docs[0]["form_no"]
    _created_form_ids.append(view_chip[len("exam_form_view"):])


# --------------------------------------------------------------------------- #
# MASTER: unified entry points — typed/voice NL → SAME canonical hub
# --------------------------------------------------------------------------- #
def _chat_events_cid(client_: TestClient, message: str, chat_id: str):
    """Like `_chat_events_with` but uses the caller-provided chat_id so the
    conversation state (e.g. the Student Services auth gate) persists across
    turns."""
    r = client_.post(
        "/api/chat/ask",
        json={"message": message, "chat_id": chat_id, "stream": True},
        headers=STU,
    )
    assert r.status_code == 200
    events = []
    for line in r.text.splitlines():
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[len("data: "):]))
            except Exception:
                events.append({"type": "token", "text": line[len("data: "):]})
    assert events, "chat stream should yield events"
    return events


def _chat_token_texts(events) -> list[str]:
    return [e.get("text", "") for e in events if e.get("type") == "token"]


def test_chat_nl_exam_form_generic_lands_canonical_hub():
    # Typed "exam form" (generic) must land on the SAME canonical hub the
    # Student Services chip renders — both options, never the legacy
    # provisioning message and never an auto-picked single flow.
    cl = _new_eligible_bca_client("CUS-EF-NL01")
    events = _chat_events_with(cl, "exam form")
    ids = _chat_option_ids(events)
    assert "exam_form_fill" in ids
    assert "exam_form_print" in ids
    texts = " ".join(_chat_token_texts(events)).lower()
    assert "no exam form is available for your profile yet" not in texts


def test_chat_nl_examination_form_generic_lands_canonical_hub():
    cl = _new_eligible_bca_client("CUS-EF-NL02")
    events = _chat_events_with(cl, "examination form")
    ids = _chat_option_ids(events)
    assert "exam_form_fill" in ids
    assert "exam_form_print" in ids


def test_chat_nl_fill_exam_form_goes_directly_to_fill():
    # "fill exam form" (typed / voice) goes DIRECTLY to the Fill flow (open
    # session picker) without showing the hub first.
    cl = _new_eligible_bca_client("CUS-EF-NL03")
    _open_session("TESTCHATNL3")
    events = _chat_events_with(cl, "fill exam form")
    ids = _chat_option_ids(events)
    picks = [i for i in ids if i.startswith("exam_form_pick") and "testchatnl3" in i]
    assert picks, f"expected a session picker chip, got {ids}"


def test_chat_nl_print_exam_form_goes_directly_to_print_empty_state():
    # "print exam form" goes DIRECTLY to the Print flow; a student with no
    # submitted form sees the print empty state — never the hub, never the
    # legacy provisioning message.
    cl = _new_eligible_bca_client("CUS-EF-NL04")
    events = _chat_events_with(cl, "print exam form")
    texts = " ".join(_chat_token_texts(events))
    assert "No submitted exam form is available" in texts
    assert "No Exam Form is available for your profile yet" not in texts
    assert not any(i.startswith("exam_form_view") for i in _chat_option_ids(events))


def test_chat_nl_exam_form_auth_resume_lands_canonical_hub():
    # Unauthenticated "exam form" opens the auth gate; after sign-in the
    # frontend sends "Student Services" and the ORIGINAL intent resumes to the
    # canonical hub (both options) — never the legacy provisioning message.
    st = _create_student(reg="CUS-EF-NL05", current_semester=3, admission_year=2023)
    cl = TestClient(app)
    cid = f"nl_{uuid.uuid4().hex[:8]}"
    ev1 = _chat_events_cid(cl, "exam form", cid)
    assert any(e.get("type") == "auth_form" for e in ev1), ev1
    _login(cl, st["reg_no"])
    ev2 = _chat_events_cid(cl, "Student Services", cid)
    ids = _chat_option_ids(ev2)
    assert "exam_form_fill" in ids
    assert "exam_form_print" in ids
    texts = " ".join(_chat_token_texts(ev2)).lower()
    assert "no exam form is available for your profile yet" not in texts


def test_chat_nl_fill_exam_form_auth_resume_goes_directly_to_fill():
    st = _create_student(reg="CUS-EF-NL06", current_semester=3, admission_year=2023)
    _open_session("TESTCHATNL6")
    cl = TestClient(app)
    cid = f"nl_{uuid.uuid4().hex[:8]}"
    ev1 = _chat_events_cid(cl, "fill exam form", cid)
    assert any(e.get("type") == "auth_form" for e in ev1), ev1
    _login(cl, st["reg_no"])
    ev2 = _chat_events_cid(cl, "Student Services", cid)
    ids = _chat_option_ids(ev2)
    picks = [i for i in ids if i.startswith("exam_form_pick") and "testchatnl6" in i]
    assert picks, f"resumed fill should reach the session picker, got {ids}"
    assert "exam_form_fill" not in ids, "resumed fill should skip the hub"
    assert "exam_form_print" not in ids, "resumed fill should skip the hub"