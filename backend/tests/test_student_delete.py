"""
backend/tests/test_student_delete.py

Phase H battery — PERMANENT DELETE FEATURE (Student + Result), everything else frozen.

  1. DELETE /api/admin/students/{id} is superadmin-only (admin/authority/student 403,
     anonymous 401) and permanently removes the student + every linked record
     (sessions, results, admit cards, exam forms, fee receipts, attendance,
     transcripts, migration certs, revaluations, xerox requests, backlogs,
     course registrations, helpdesk tickets) atomically.
  2. Grievance history SURVIVES a student delete with its FK nulled (ON DELETE
     SET NULL semantics — same as PostgreSQL would apply).
  3. A deleted student cannot sign in again and any existing cookie becomes
     immediately invalid (resolve_session -> None) — no ghost sessions.
  4. DELETE /api/admin/results/{id} is superadmin-only and removes EXACTLY ONE
     result row; sibling results and the student are untouched.
  5. Both deletes are audited (action, actor, student_id/result_id, outcome)
     and the audit detail never contains credential material (DOB/hash/token).
  6. Unknown/malformed ids => 404 (no enumeration).

These tests share the same SQLite live DB as the server (sqlite:///./cus_ai.db),
so every created row is explicitly cleaned up in the module teardown (the local
engine does not enforce ON DELETE CASCADE).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.auth.security import hash_password
from app.config import settings
from app.database import SessionLocal, create_all
from app.main import app
from app.models import (
    AuditLog,
    BacklogStatus,
    CourseRegistration,
    FeeReceipt,
    Grievance,
    HelpdeskTicket,
    MigrationCertificate,
    RefreshToken,
    Revaluation,
    Student,
    StudentAdmitCard,
    StudentAttendance,
    StudentExamForm,
    StudentResult,
    StudentSession,
    StudentTranscript,
    User,
    XeroxRequest,
)
from app.student.session import resolve_session

create_all()

client = TestClient(app)

SUPER: dict[str, str] = {}
ADMIN: dict[str, str] = {}
AUTH: dict[str, str] = {}
STU: dict[str, str] = {}

_created_student_ids: list[str] = []
_created_result_ids: list[str] = []
_created_grievance_ids: list[str] = []
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
            username = f"__pd_{key}_{uuid.uuid4().hex[:6]}"
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
        created_student_ids = tuple(_created_student_ids)
        if created_student_ids:
            for model in (
                StudentResult,
                StudentAdmitCard,
                StudentExamForm,
                FeeReceipt,
                StudentAttendance,
                StudentTranscript,
                MigrationCertificate,
                Revaluation,
                XeroxRequest,
                BacklogStatus,
                CourseRegistration,
                HelpdeskTicket,
            ):
                db.query(model).filter(model.student_id.in_(created_student_ids)).delete(
                    synchronize_session=False
                )
            db.query(StudentSession).filter(
                StudentSession.student_id.in_(created_student_ids)
            ).delete(synchronize_session=False)
        if _created_grievance_ids:
            db.query(Grievance).filter(Grievance.id.in_(_created_grievance_ids)).delete(
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


def _create_student(**over) -> tuple[dict, dict]:
    body = {
        "reg_no": f"CUS-PD-{uuid.uuid4().hex[:6].upper()}",
        "name": "Delete Test Student",
        "dob": "2005-06-15",
        "programme": "bca",
        "current_semester": 2,
        "admission_year": 2024,
        "is_active": True,
    }
    body.update(over)
    r = client.post("/api/admin/students", json=body, headers=SUPER)
    assert r.status_code == 201, r.text
    dto = r.json()
    _created_student_ids.append(dto["id"])
    return dto, body


def _seed_children(db, student_id: str) -> dict:
    """Seed one of every linked record + two results + one session + one grievance."""
    ids: dict = {}
    result_a = StudentResult(
        student_id=uuid.UUID(student_id), semester=1, subject_name="Mathematics",
        subject_code="MAT101", internal_marks=35, external_marks=55, total_marks=90,
        max_marks=100, grade="A", status="pass",
    )
    result_b = StudentResult(
        student_id=uuid.UUID(student_id), semester=3, subject_name="Programming",
        subject_code="PRG301", internal_marks=30, external_marks=60, total_marks=90,
        max_marks=100, grade="A", status="pass",
    )
    db.add_all([result_a, result_b])
    db.flush()
    ids["result_a"] = str(result_a.id)
    ids["result_b"] = str(result_b.id)
    _created_result_ids.extend([ids["result_a"], ids["result_b"]])

    admit = StudentAdmitCard(student_id=uuid.UUID(student_id), semester=1)
    exam_form = StudentExamForm(student_id=uuid.UUID(student_id), semester=1)
    fee = FeeReceipt(student_id=uuid.UUID(student_id), semester=1, paid_amount=45000)
    attendance = StudentAttendance(student_id=uuid.UUID(student_id), semester=1, subject_name="Mathematics")
    transcript = StudentTranscript(student_id=uuid.UUID(student_id), semester=1)
    migration = MigrationCertificate(student_id=uuid.UUID(student_id), certificate_no="M123")
    reval = Revaluation(student_id=uuid.UUID(student_id), semester=1, subject_name="Mathematics")
    xerox = XeroxRequest(student_id=uuid.UUID(student_id), semester=1, paper_name="Mathematics")
    backlog = BacklogStatus(student_id=uuid.UUID(student_id), semester=1, subject_name="Mathematics")
    course_reg = CourseRegistration(student_id=uuid.UUID(student_id), semester=1, status="Registered")
    helpdesk = HelpdeskTicket(student_id=uuid.UUID(student_id), ticket_id="HD-1")
    db.add_all(
        [admit, exam_form, fee, attendance, transcript, migration, reval, xerox, backlog, course_reg, helpdesk]
    )

    sess = StudentSession(
        student_id=uuid.UUID(student_id),
        token=uuid.uuid4().hex,
        expires_at=datetime.now(timezone.utc),
        revoked=False,
    )
    db.add(sess)

    grievance = Grievance(
        id=uuid.uuid4(),
        source_kind="student",
        student_id=uuid.UUID(student_id),
        student_name="Delete Test Student",
        roll_number="CUS-PD-TEST",
        final_grievance_text="test grievance",
    )
    db.add(grievance)
    db.commit()
    ids["session_token"] = sess.token
    _created_grievance_ids.append(str(grievance.id))
    return ids


def _get_student(student_id: str) -> Student | None:
    db = SessionLocal()
    try:
        return db.get(Student, uuid.UUID(student_id))
    finally:
        db.close()


def _count(model, student_id: str) -> int:
    db = SessionLocal()
    try:
        return db.query(model).filter(model.student_id == str(student_id)).count()
    finally:
        db.close()


def _super_db_id() -> str:
    db = SessionLocal()
    try:
        return str(db.query(User).filter(User.username == _LOCAL["super"]).one().id)
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Student permanent delete
# --------------------------------------------------------------------------- #
def test_permanent_delete_removes_student_and_all_linked_rows():
    dto, body = _create_student(reg="CUS-PD-CASCADE")
    db = SessionLocal()
    try:
        ids = _seed_children(db, dto["id"])
    finally:
        db.close()

    r = client.delete(f"/api/admin/students/{dto['id']}", headers=SUPER)
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": True, "reg_no": body["reg_no"].upper()}

    assert _get_student(dto["id"]) is None
    for model in (
        StudentResult, StudentAdmitCard, StudentExamForm, FeeReceipt,
        StudentAttendance, StudentTranscript, MigrationCertificate, Revaluation,
        XeroxRequest, BacklogStatus, CourseRegistration, HelpdeskTicket,
    ):
        assert _count(model, dto["id"]) == 0, f"{model.__tablename__} orphaned"
    assert _count(StudentSession, dto["id"]) == 0, "sessions orphaned"

    db = SessionLocal()
    try:
        grievance = db.get(Grievance, uuid.UUID(_created_grievance_ids[-1]))
        assert grievance is not None, "grievance history must survive"
        assert grievance.student_id is None, "grievance FK must be nulled"
        assert grievance.student_name == "Delete Test Student"
    finally:
        db.close()
    _created_grievance_ids.pop()


def test_delete_student_role_guards_are_server_side():
    dto, _ = _create_student(reg="CUS-PD-GUARD")
    url = f"/api/admin/students/{dto['id']}"
    assert client.delete(url, headers=ADMIN).status_code == 403
    assert client.delete(url, headers=AUTH).status_code == 403
    assert client.delete(url, headers=STU).status_code == 403
    assert client.delete(url).status_code == 401

    assert _get_student(dto["id"]) is not None, "guards must not delete anything"

    r = client.delete(url, headers=SUPER)
    assert r.status_code == 200


def test_delete_unknown_student_404():
    r = client.delete(f"/api/admin/students/{uuid.uuid4()}", headers=SUPER)
    assert r.status_code == 404
    r = client.delete("/api/admin/students/NOT-A-REG-NO", headers=SUPER)
    assert r.status_code == 404


def test_deleted_student_cannot_login_and_cookie_is_invalid():
    dto, body = _create_student(reg="CUS-PD-TOMBSTONE")
    r = client.post("/api/student/verify", json={"reg_no": body["reg_no"], "dob": "2005-06-15"})
    assert r.status_code == 200, r.text
    token = client.cookies.get(settings.STUDENT_SESSION_COOKIE)
    assert token

    db = SessionLocal()
    try:
        assert resolve_session(db, token) is not None, "session must resolve pre-delete"
    finally:
        db.close()

    r = client.delete(f"/api/admin/students/{dto['id']}", headers=SUPER)
    assert r.status_code == 200

    db = SessionLocal()
    try:
        assert resolve_session(db, token) is None, "cookie must fail immediately after delete"
    finally:
        db.close()

    r = client.post("/api/student/verify", json={"reg_no": body["reg_no"], "dob": "2005-06-15"})
    assert r.status_code == 401  # deleted student cannot sign in again


# --------------------------------------------------------------------------- #
# Result delete (single row)
# --------------------------------------------------------------------------- #
def test_result_delete_removes_only_target_row():
    dto, _ = _create_student(reg="CUS-PD-ONEROW")
    db = SessionLocal()
    try:
        ids = _seed_children(db, dto["id"])
    finally:
        db.close()

    r = client.delete(f"/api/admin/results/{ids['result_b']}", headers=SUPER)
    assert r.status_code == 200, r.text
    assert r.json() == {"deleted": True, "result_id": ids["result_b"]}

    db = SessionLocal()
    try:
        assert db.get(StudentResult, uuid.UUID(ids["result_b"])) is None
        assert db.get(StudentResult, uuid.UUID(ids["result_a"])) is not None
        assert db.get(Student, uuid.UUID(dto["id"])) is not None
    finally:
        db.close()
    _created_result_ids.remove(ids["result_b"])


def test_result_delete_role_guards_are_server_side():
    dto, _ = _create_student(reg="CUS-PD-RGUARD")
    db = SessionLocal()
    try:
        ids = _seed_children(db, dto["id"])
    finally:
        db.close()

    url = f"/api/admin/results/{ids['result_a']}"
    assert client.delete(url, headers=ADMIN).status_code == 403
    assert client.delete(url, headers=AUTH).status_code == 403
    assert client.delete(url, headers=STU).status_code == 403
    assert client.delete(url).status_code == 401

    db = SessionLocal()
    try:
        assert db.get(StudentResult, uuid.UUID(ids["result_a"])) is not None
    finally:
        db.close()


def test_delete_unknown_result_404():
    r = client.delete(f"/api/admin/results/{uuid.uuid4()}", headers=SUPER)
    assert r.status_code == 404
    r = client.delete("/api/admin/results/NOT-A-UUID", headers=SUPER)
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #
def test_deletes_are_audited_without_credentials():
    dto, body = _create_student(reg="CUS-PD-AUDIT")
    db = SessionLocal()
    try:
        ids = _seed_children(db, dto["id"])
    finally:
        db.close()
    super_id = _super_db_id()

    r = client.delete(f"/api/admin/results/{ids['result_a']}", headers=SUPER)
    assert r.status_code == 200
    r = client.delete(f"/api/admin/students/{dto['id']}", headers=SUPER)
    assert r.status_code == 200

    db = SessionLocal()
    try:
        student_log = (
            db.query(AuditLog)
            .filter(AuditLog.action == "student.delete", AuditLog.actor_id == super_id)
            .all()
        )
        assert student_log, "student.delete must be audited"
        hit = next((x for x in student_log if x.target == body["reg_no"].upper()), None)
        assert hit is not None, "audit target must be the reg_no"
        assert "outcome=success" in (hit.detail or "")
        assert str(dto["id"]) in (hit.detail or "")
        assert hit.actor_role == "superadmin"

        result_log = (
            db.query(AuditLog)
            .filter(AuditLog.action == "student_result.delete", AuditLog.actor_id == super_id)
            .all()
        )
        assert result_log, "student_result.delete must be audited"
        hit_r = next((x for x in result_log if x.target == ids["result_a"]), None)
        assert hit_r is not None, "result audit target must be the result id"
        assert str(dto["id"]) in (hit_r.detail or "")

        for row in [hit, hit_r]:
            blob = f"{row.detail or ''}|{row.target or ''}"
            for banned in ("2005-06-15", "2004", "hashed_password", "token=", "$2b$"):
                assert banned not in blob, f"credential material leaked in audit: {blob}"
    finally:
        db.close()