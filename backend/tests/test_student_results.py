"""
backend/tests/test_student_results.py

Phase B battery — Student Results (authenticated "my results" + Super Admin
CSV/XLSX import with preview → confirm → single-transaction apply).

  1. Student side (server-validated session cookie):
       - unauthenticated / expired / revoked / inactive sessions → 401
       - own results only; semester allowlist; safe "no results" message
       - per-attempt selection: semester + examination roll number (POST
         body, never a URL); a wrong/foreign roll → safe "no result"
       - IDOR: cross-student access blocked even with tampered params
       - response payload is an explicit allowlist (no ids / credentials)
  2. Super Admin side (require_superadmin everywhere):
       - list; import preview (writes nothing); import confirm (atomic)
       - optional Examination Roll Number column imported + validated
       - invalid rows / in-file duplicates / DB duplicates → rejected with
         nothing written
       - audit records only filename + count (never credentials/marks)
  3. Chat flow: authenticated "show my results" → semester+roll form → POST
       /view detail; print/download HTML is markup-safe; no credential
       material in any stream, chat text or print document.
"""

from __future__ import annotations

import csv
import io
import uuid
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from app.auth.security import hash_password
from app.database import SessionLocal, create_all
from app.main import app
from app.models import AuditLog, Student, StudentResult, StudentSession, User
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
_created_result_ids: list[str] = []

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
            username = f"__pr_{key}_{uuid.uuid4().hex[:6]}"
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
        for rid in _created_result_ids:
            db.query(StudentResult).filter(StudentResult.id == str(rid)).delete()
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
        "reg_no": reg or f"CUS-PR-{uuid.uuid4().hex[:6].upper()}",
        "name": "Results Student",
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


def _seed_result(db, student_id: str, semester: int = 1, code: str = "CUS101",
                 name: str = "Mathematics", **over) -> str:
    r = StudentResult(
        id=uuid.uuid4(),
        student_id=uuid.UUID(student_id),
        semester=semester,
        exam_type="Regular",
        subject_name=name[:200],
        subject_code=code[:20],
        internal_marks=25,
        external_marks=50,
        total_marks=75,
        max_marks=100,
        grade="B",
        sgpa="7.50",
        cgpa="7.40",
        status="pass",
        academic_year="2023-2024",
    )
    for k, v in over.items():
        setattr(r, k, v)
    db.add(r)
    db.commit()
    _created_result_ids.append(str(r.id))
    return str(r.id)


def _login(client_: TestClient, reg: str, dob: str = "2005-06-15") -> None:
    r = client_.post("/api/student/verify", json={"reg_no": reg, "dob": dob})
    assert r.status_code == 200, r.text


def _msg(r):
    return r.json()["error"]["message"]


_DEFAULT_HEADERS = [
    "registration_number", "semester", "subject_code", "subject_name",
    "internal_marks", "external_marks", "total_marks", "max_marks",
    "grade", "sgpa", "cgpa", "status",
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


def _preview(rows: list[list], filename: str = "r.csv", headers=None) -> object:
    ext = filename.rsplit(".", 1)[-1]
    content = _csv_bytes(rows, headers) if ext == "csv" else _xlsx_bytes(rows, headers)
    mime = "text/csv" if ext == "csv" else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return client.post(
        "/api/admin/results/preview",
        files={"file": (filename, content, mime)},
        headers=SUPER,
    )


def _confirm(raw_rows, filename: str = "r.csv"):
    return client.post(
        "/api/admin/results/confirm",
        json={"filename": filename, "rows": raw_rows},
        headers=SUPER,
    )


def _raw_dicts(row_lists):
    return [dict(zip(_DEFAULT_HEADERS, row)) for row in row_lists]


def _count_results():
    db = SessionLocal()
    try:
        return db.query(StudentResult).count()
    finally:
        db.close()


# --------------------------------------------------------------------------- #
# Setup used across tests
# --------------------------------------------------------------------------- #
@pytest.fixture(scope="module", autouse=True)
def _seed_people():
    global A, B
    A.update(_create_student(reg="CUS-PR-A0001"))
    B.update(_create_student(reg="CUS-PR-B0001"))
    db = SessionLocal()
    try:
        # A: semesters 1 & 2 with ONE STABLE examination roll (assigned in
        # semester 1 and reused for semester 2 — never a per-attempt roll).
        _seed_result(db, A["id"], semester=1, code="CUS101", name="Mathematics", exam_roll_no="23001")
        _seed_result(db, A["id"], semester=1, code="CUS102", name="Physics", exam_roll_no="23001")
        _seed_result(db, A["id"], semester=2, code="CUS201", name="Computer Science", exam_roll_no="23001")
        # B: no results at all
    finally:
        db.close()
    _login(client_a, A["reg_no"])
    _login(client_b, B["reg_no"])


# --------------------------------------------------------------------------- #
# Student side — authn / authz
# --------------------------------------------------------------------------- #
def test_results_requires_valid_session():
    r = client.get("/api/student/results")
    assert r.status_code == 401
    assert "session" in _msg(r).lower()


def test_authed_student_lists_own_semesters():
    r = client_a.get("/api/student/results")
    assert r.status_code == 200
    sems = {s["semester"] for s in r.json()["semesters"]}
    assert sems == {1, 2}
    assert all(s["subject_count"] > 0 for s in r.json()["semesters"])


def test_authed_payload_is_safe_allowlist():
    r = client_a.post("/api/student/results/view", json={"semester": 1, "exam_roll_no": "23001"})
    assert r.status_code == 200
    data = r.json()
    assert data["found"] is True
    res = data["result"]
    assert res["student_name"] == "Results Student"
    assert res["reg_no"] == "CUS-PR-A0001"
    assert res["exam_roll_no"] == "23001"
    assert res["semester"] == 1
    assert len(res["subjects"]) == 2
    assert res["semester_summary"]["subject_count"] == 2
    allowed = {
        "semester", "exam_type", "academic_year", "subject_code", "subject_name",
        "internal_marks", "external_marks", "total_marks", "max_marks",
        "grade", "status", "sgpa", "cgpa",
    }
    subj = res["subjects"][0]
    assert set(subj.keys()) == allowed
    blob = r.text
    for banned in ("student_id", "student.session", "hashed_password", "password", "token", "cus_student_sid"):
        assert banned not in blob


def test_view_wrong_roll_is_safe_not_found():
    r = client_a.post("/api/student/results/view", json={"semester": 1, "exam_roll_no": "99999999"})
    assert r.status_code == 200
    data = r.json()
    assert data["found"] is False
    assert "No result was found" in data["message"]


def test_view_no_semester_published_is_safe_not_found():
    # Semester 4 is on the allowlist but A has nothing published for it.
    r = client_a.post("/api/student/results/view", json={"semester": 4, "exam_roll_no": "23004"})
    assert r.status_code == 200
    assert r.json()["found"] is False
    # B has no results at all — even a matching-shaped roll must not leak A's.
    rb = client_b.post("/api/student/results/view", json={"semester": 1, "exam_roll_no": "23001"})
    assert rb.status_code == 200
    assert rb.json()["found"] is False


def test_view_malformed_roll_rejected():
    for bad in ("", "ab cd", "!!!", "super-long-roll-number-exceeding-fifty-characters-longer-more"):
        r = client_a.post("/api/student/results/view", json={"semester": 1, "exam_roll_no": bad})
        assert r.status_code == 422, bad
    r = client_a.post("/api/student/results/view", json={"semester": 1, "exam_roll_no": "AB-cd12"})
    assert r.status_code == 200  # hyphens and case are allowed


def test_view_semester_outside_allowlist_rejected():
    r = client_a.post("/api/student/results/view", json={"semester": 9, "exam_roll_no": "23009"})
    assert r.status_code == 422
    # the legacy whole-semester GET no longer exists (roll-less lookup is gone)
    r = client_a.get("/api/student/results?semester=1")
    assert r.status_code == 422


def test_view_unauthenticated_rejected():
    r = client.post("/api/student/results/view", json={"semester": 1, "exam_roll_no": "23001"})
    assert r.status_code == 401


def test_expired_session_rejected():
    dto = _create_student(reg="CUS-PR-EXP901")
    db = SessionLocal()
    try:
        stu = db.query(Student).filter(Student.id == uuid.UUID(dto["id"])).first()
        raw = create_session(db, stu, ttl_minutes=1)
        row = db.query(StudentSession).filter(StudentSession.student_id == stu.id).first()
        row.expires_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()
    r = client.get("/api/student/results", cookies={"cus_student_sid": raw})
    assert r.status_code == 401
    db = SessionLocal()
    try:
        stu = db.query(Student).filter(Student.id == uuid.UUID(dto["id"])).first()
        db.delete(stu)
        db.commit()
    finally:
        db.close()


def test_revoked_session_rejected():
    dto = _create_student(reg="CUS-PR-REV902")
    db = SessionLocal()
    try:
        stu = db.query(Student).filter(Student.id == uuid.UUID(dto["id"])).first()
        raw = create_session(db, stu, ttl_minutes=10)
        revoke_session(db, raw)
    finally:
        db.close()
    r = client.get("/api/student/results", cookies={"cus_student_sid": raw})
    assert r.status_code == 401
    db = SessionLocal()
    try:
        stu = db.query(Student).filter(Student.id == uuid.UUID(dto["id"])).first()
        db.delete(stu)
        db.commit()
    finally:
        db.close()


def test_inactive_student_session_rejected():
    dto = _create_student(reg="CUS-PR-IA900")
    db = SessionLocal()
    try:
        stu = db.query(Student).filter(Student.id == uuid.UUID(dto["id"])).first()
        raw = create_session(db, stu, ttl_minutes=10)
        stu.is_active = False
        stu.status = "deactivated"
        db.commit()
    finally:
        db.close()
    r = client.get("/api/student/results", cookies={"cus_student_sid": raw})
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
    # Tampering with identity params changes nothing: identity comes from the
    # HttpOnly session cookie only, and the roll is scoped to the OWNER.
    r = client_a.post(
        "/api/student/results/view",
        json={"semester": 1, "exam_roll_no": "23001",
              "student_id": B["id"], "reg_no": B["reg_no"], "student_id2": B["id"]},
    )
    assert r.status_code == 200
    assert r.json()["found"] is True  # A's OWN attempt, with A's own roll


def test_idor_foreign_roll_never_resolves():
    # A fresh third student owns its own roll; neither student can read the
    # other's attempt even though both rolls are structurally identical.
    c = _create_student(reg=f"CUS-PR-C-{uuid.uuid4().hex[:4].upper()}")
    db = SessionLocal()
    try:
        _seed_result(db, c["id"], semester=1, code="CUS901", name="C-Physics", exam_roll_no="29999")
    finally:
        db.close()
    client_c = TestClient(app)
    _login(client_c, c["reg_no"])
    try:
        r_b_into_a = client_b.post("/api/student/results/view", json={"semester": 1, "exam_roll_no": "23001"})
        assert r_b_into_a.status_code == 200 and r_b_into_a.json()["found"] is False
        r_a_into_c = client_a.post("/api/student/results/view", json={"semester": 1, "exam_roll_no": "29999"})
        assert r_a_into_c.status_code == 200 and r_a_into_c.json()["found"] is False
        r_c_own = client_c.post("/api/student/results/view", json={"semester": 1, "exam_roll_no": "29999"})
        assert r_c_own.status_code == 200 and r_c_own.json()["found"] is True
        assert "C-Physics" in r_c_own.text
    finally:
        db = SessionLocal()
        try:
            for rid in _created_result_ids:
                pass  # cleanup handled in _bootstrap teardown
            db.query(Student).filter(Student.id == uuid.UUID(c["id"])).delete()
            db.commit()
        finally:
            db.close()
    # A's earlier tampered request never switched identity
    r = client_a.post("/api/student/results/view", json={"semester": 1, "exam_roll_no": "23001"})
    assert r.json()["found"] is True
    assert "Mathematics" in r.text


# --------------------------------------------------------------------------- #
# Super-Admin management + import
# --------------------------------------------------------------------------- #
def test_admin_results_authz():
    assert client.get("/api/admin/results", headers=ADMIN).status_code == 403
    assert client.get("/api/admin/results", headers=STU).status_code == 403
    assert client.get("/api/admin/results").status_code == 401
    r = client.get("/api/admin/results", headers=SUPER)
    assert r.status_code == 200
    assert any(x["reg_no"] == "CUS-PR-A0001" for x in r.json()["results"])

    payload = {"filename": "x.csv", "rows": []}
    assert client.post("/api/admin/results/preview", files={"file": ("x.csv", b"a,b", "text/csv")}, headers=ADMIN).status_code == 403
    assert client.post("/api/admin/results/confirm", json=payload, headers=ADMIN).status_code == 403
    assert client.post("/api/admin/results/confirm", json=payload).status_code == 401


def test_preview_valid_csv_resolves_students():
    rows = [
        ["CUS-PR-A0001", 7, "HIS701", "History", 30, 60, 90, 100, "A", "8.00", "7.60", "pass"],
        ["CUS-PR-B0001", 7, "HIS702", "Political Science", 28, 55, 83, 100, "B+", "8.00", "7.60", "pass"],
    ]
    r = _preview(rows, "a.csv")
    assert r.status_code == 200
    data = r.json()
    assert data["total_rows"] == 2
    assert data["valid_count"] == 2
    assert data["error_count"] == 0
    assert data["rows"][0]["registration_number"] == "CUS-PR-A0001"


def test_preview_rejects_unknown_student_deeply():
    rows = [["CUS-PR-NOPE1", 7, "X", "Unknown Reg Student", 30, 60, 90, 100, "A", "", "", "pass"]]
    r = _preview(rows, "a.csv")
    assert r.status_code == 200
    data = r.json()
    assert data["error_count"] == 1
    assert "Unknown registration number" in data["errors"][0]["message"]


def test_preview_reports_row_validation_errors():
    rows = [
        ["", 3, "M3", "", 10, 20, 30, 100, "C", "", "", "pass"],          # no reg + no subject
        ["CUS-PR-A0001", "abc", "M4", "Bad Sem", 10, 20, 30, 100, "C", "", "", "pass"],  # bad semester
        ["CUS-PR-A0001", 3, "M5", "Ok", 10, 20, 30, 0, "C", "", "", "pass"],             # max_marks 0
        ["CUS-PR-A0001", 3, "M6", "Bad Year", 10, 20, 30, 100, "C", "", "", "maybe"],    # bad status
    ]
    r = _preview(rows, "a.csv")
    assert r.status_code == 200
    data = r.json()
    assert data["error_count"] == 4
    msgs = " ".join(e["message"] for e in data["errors"])
    for frag in ("Registration number is required", "Semester must be a whole number",
                 "greater than zero", "Status must be 'pass' or 'fail'"):
        assert frag in msgs


def test_preview_xlsx_supported():
    rows = [
        ["CUS-PR-A0001", 8, "ENG801", "English", 32, 58, 90, 100, "A", "8.40", "7.80", "pass"],
    ]
    r = _preview(rows, "b.xlsx")
    assert r.status_code == 200
    assert r.json()["valid_count"] == 1


def test_preview_rejects_unsupported_format():
    r = client.post(
        "/api/admin/results/preview",
        files={"file": ("r.pdf", b"%PDF-fake", "application/pdf")},
        headers=SUPER,
    )
    assert r.status_code == 400


def test_preview_missing_required_column_reports_header_error():
    rows = [["CUS-PR-A0001", 3]]
    headers = ["registration_number", "semester"]
    r = _preview(rows, "a.csv", headers=headers)
    assert r.status_code == 200
    data = r.json()
    assert data["error_count"] == 1
    assert "Missing required column" in data["errors"][0]["message"]
    assert data["valid_count"] == 0


def test_preview_in_file_duplicate_blocked():
    rows = [["CUS-PR-A0001", 6, "PHY601", "Physics", 1, 1, 2, 100, "C", "", "", "pass"]] * 2
    r = _preview(rows, "dup.csv")
    assert r.status_code == 200
    data = r.json()
    assert data["error_count"] == 1
    assert "Duplicate row in the file" in data["errors"][0]["message"]


def test_confirm_applies_in_single_transaction():
    before = _count_results()
    rows = [
        ["CUS-PR-A0001", 3, "ECO201", "Micro Economics", 30, 60, 90, 100, "A", "8.00", "7.60", "pass"],
        ["CUS-PR-A0001", 3, "ECO202", "Macro Economics", 28, 55, 83, 100, "B+", "8.00", "7.60", "pass"],
    ]
    p = _preview(rows, "c.csv")
    assert p.status_code == 200 and p.json()["error_count"] == 0

    r = _confirm(p.json()["raw_rows"], "c.csv")
    assert r.status_code == 200
    assert r.json()["imported"] == 2
    assert _count_results() == before + 2

    db = SessionLocal()
    try:
        aud = (
            db.query(AuditLog)
            .filter(AuditLog.action == "student_result.import")
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
    before = _count_results()
    rows = [
        ["CUS-PR-A0001", 5, "SCI501", "Botany", 10, 20, 30, 100, "C", "", "", "pass"],
        ["CUS-PR-NOPE9", 5, "SCI502", "Zoology", 10, 20, 30, 100, "C", "", "", "pass"],
    ]
    r = _confirm(_raw_dicts(rows), "bad.csv")
    assert r.status_code == 422
    assert _count_results() == before  # nothing written


def test_confirm_db_duplicate_returns_409_and_writes_nothing():
    db = SessionLocal()
    try:
        _seed_result(db, A["id"], semester=5, code="SCI501", name="Botany", academic_year=None)
    finally:
        db.close()
    before = _count_results()
    rows = [
        ["CUS-PR-A0001", 5, "SCI501", "Botany", 10, 20, 30, 100, "C", "", "", "pass"],
    ]
    r = _confirm(_raw_dicts(rows), "dupdb.csv")
    assert r.status_code == 409
    assert "duplicate" in _msg(r).lower()
    assert _count_results() == before


def test_admin_list_filters_and_paginates():
    r = client.get("/api/admin/results?q=CUS-PR-A0001&semester=1&page=1&page_size=5", headers=SUPER)
    assert r.status_code == 200
    data = r.json()
    assert data["total"] >= 2
    assert all(x["semester"] == 1 for x in data["results"])
    assert all(x["reg_no"] == "CUS-PR-A0001" for x in data["results"])

    r = client.get("/api/admin/results?semester=9&page_size=5", headers=SUPER)
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Print / download — server-rendered, markup-safe HTML document
# --------------------------------------------------------------------------- #
def test_print_returns_escaped_html_document():
    r = client_a.post("/api/student/results/view/print", json={"semester": 1, "exam_roll_no": "23001"})
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("text/html")
    assert "Statement of Marks" in r.text
    assert "23001" in r.text
    assert "Mathematics" in r.text
    assert r.headers.get("x-robots-tag") == "noindex, nofollow"
    for banned in ("student_id", "cus_student_sid", "hashed_password", "password"):
        assert banned not in r.text


def test_print_attachment_headers():
    r = client_a.post("/api/student/results/view/print", json={"semester": 2, "exam_roll_no": "23001", "as_attachment": True})
    assert r.status_code == 200
    assert "Result_Semester_2.html" in (r.headers.get("content-disposition") or "")


def test_print_not_found_and_malformed_rejected():
    r = client_a.post("/api/student/results/view/print", json={"semester": 1, "exam_roll_no": "99999999"})
    assert r.status_code == 404
    r = client_a.post("/api/student/results/view/print", json={"semester": 1, "exam_roll_no": "!"})
    assert r.status_code == 422
    r = client.post("/api/student/results/view/print", json={"semester": 1, "exam_roll_no": "23001"})
    assert r.status_code == 401


# --------------------------------------------------------------------------- #
# Current-semester boundary — the authoritative filter applies server-side
# --------------------------------------------------------------------------- #
def test_current_semester_filters_available_list():
    # A is a current_semester=2 student; later tests import rows for semester 3
    # (and 5) via the admin flow, but the STUDENT list must never offer the
    # client a semester beyond the stored current semester.
    r = client_a.get("/api/student/results")
    assert r.status_code == 200
    sems = [s["semester"] for s in r.json()["semesters"]]
    assert 1 in sems and 2 in sems
    assert 3 not in sems and 4 not in sems and 5 not in sems


def test_view_rejects_semester_beyond_current_even_if_published():
    # A DOES have a published semester-3 attempt by now (imported by an earlier
    # test) but is only a semester-2 student. POSTing semester 3 with a
    # matching-shaped roll is treated exactly like "no result" — no data.
    r = client_a.post("/api/student/results/view", json={"semester": 3, "exam_roll_no": "23003"})
    assert r.status_code == 200
    assert r.json()["found"] is False
    assert "No result was found" in r.json()["message"]
    rp = client_a.post("/api/student/results/view/print", json={"semester": 3, "exam_roll_no": "23003"})
    assert rp.status_code == 404


def test_current_semester_one_offers_only_semester_one():
    # Boundary: a first-semester student sees ONLY semester 1 even when admin
    # data exists for semester 2; the semester-2 attempt is unreachable.
    d = _create_student(reg=f"CUS-PR-G1-{uuid.uuid4().hex[:4].upper()}", current_semester=1)
    db = SessionLocal()
    try:
        _seed_result(db, d["id"], semester=1, code="G1101", name="G1-Math", exam_roll_no="230101")
        _seed_result(db, d["id"], semester=2, code="G2201", name="G1-Physics", exam_roll_no="230101")
    finally:
        db.close()
    client_g = TestClient(app)
    _login(client_g, d["reg_no"])
    r = client_g.get("/api/student/results")
    assert r.status_code == 200
    assert [s["semester"] for s in r.json()["semesters"]] == [1]
    rv = client_g.post("/api/student/results/view", json={"semester": 2, "exam_roll_no": "230101"})
    assert rv.status_code == 200 and rv.json()["found"] is False


# --------------------------------------------------------------------------- #
# Import: optional Examination Roll Number column
# --------------------------------------------------------------------------- #
def test_import_with_exam_roll_column_applied_and_listed():
    # B is a current-semester-2 student, so the imported attempt is for a
    # semester B may legitimately consult; the roll is B's own stable roll.
    headers = ["registration_number", "exam_roll_no", "semester", "subject_code", "subject_name",
               "internal_marks", "external_marks", "total_marks", "max_marks", "grade", "sgpa", "cgpa", "status"]
    rows = [["CUS-PR-B0001", "27001", 2, "ECO701", "Econometrics", 30, 60, 90, 100, "A", "8.00", "7.60", "pass"]]
    p = _preview(rows, "roll.csv", headers=headers)
    assert p.status_code == 200
    assert p.json()["valid_count"] == 1
    assert p.json()["rows"][0]["exam_roll_no"] == "27001"

    before = _count_results()
    r = _confirm(p.json()["raw_rows"], "roll.csv")
    assert r.status_code == 200
    assert r.json()["imported"] == 1
    assert _count_results() == before + 1

    lst = client.get("/api/admin/results?q=CUS-PR-B0001&semester=2&page_size=50", headers=SUPER)
    assert lst.status_code == 200
    assert any(x["exam_roll_no"] == "27001" for x in lst.json()["results"])

    # B now sees its own attempt with the imported roll via the student flow.
    rb = client_b.post("/api/student/results/view", json={"semester": 2, "exam_roll_no": "27001"})
    assert rb.status_code == 200 and rb.json()["found"] is True


def test_import_rejects_malformed_exam_roll():
    headers = ["registration_number", "exam_roll_no", "semester", "subject_name"]
    rows = [["CUS-PR-B0001", "bad roll @", 3, "Physics"]]
    p = _preview(rows, "badroll.csv", headers=headers)
    assert p.status_code == 200
    assert p.json()["error_count"] == 1
    assert "Examination roll number is invalid" in p.json()["errors"][0]["message"]


# --------------------------------------------------------------------------- #
# Chat integration — authenticated results flow
# --------------------------------------------------------------------------- #
def _chat_events(message: str):
    r = client_a.post(
        "/api/chat/ask",
        json={"message": message, "chat_id": f"pr_chat_{uuid.uuid4().hex[:8]}", "stream": True},
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


def test_chat_results_form_and_view_flow():
    text, events = _chat_events("show my results")
    forms = [e for e in events if e.get("type") == "results_form"]
    assert forms, "results must render the semester + roll form"
    payload = forms[0]
    sems = [s["semester"] for s in (payload.get("semesters") or [])]
    assert 1 in sems and 2 in sems  # later tests import extra semesters; must be a superset
    assert payload.get("semester") is None
    assert payload.get("roll") == ""
    assert payload.get("placeholder", "").strip()
    # The form is the ONLY interactive element — no roll-less candidate chips,
    # and no marks detail can be reached from a chat message alone.
    assert not any(e.get("type") == "options" for e in events)
    assert not any(e.get("type") == "detail" for e in events)
    for banned in ("password", "hashed_password", "dob", "cus_student_sid"):
        assert banned not in text


def test_chat_typed_semester_preselects_form():
    text, events = _chat_events("show my semester 2 results")
    forms = [e for e in events if e.get("type") == "results_form"]
    assert forms, "semester-scoped results request must render the form"
    assert forms[0].get("semester") == 2
    assert forms[0].get("roll") == ""
    for banned in ("password", "hashed_password"):
        assert banned not in text


def test_chat_typed_roll_prefills_form():
    text, events = _chat_events("my exam roll number is 23000101")
    forms = [e for e in events if e.get("type") == "results_form"]
    assert forms, "roll-number phrase must render the results form"
    assert forms[0].get("roll") == "23000101"
    # the roll lives in the form payload only — never in the raw stream twice.
    assert text.count("23000101") <= 1
    for banned in ("password", "hashed_password"):
        assert banned not in text


def test_engine_typed_semester_renders_form_deterministically():
    from app.orchestrator.engine import _results_events as results_events
    from app.orchestrator.extractor import extract_entities

    db = SessionLocal()
    try:
        entities = extract_entities("show my 2nd semester result")
        assert entities.semester == 2
        evs = list(results_events(db, {"student_id": A["id"], "semester": 2}, "ignored", entities))
        forms = [e for e in evs if e.get("type") == "results_form"]
        assert forms and forms[0].get("semester") == 2
        assert "detail" not in ", ".join(e.get("type", "") for e in evs)
    finally:
        db.close()


def test_engine_legacy_chip_preselects_form():
    from app.orchestrator.engine import _results_events as results_events_from_engine

    db = SessionLocal()
    try:
        evs = list(results_events_from_engine(db, {"student_id": A["id"], "semester": 2}, "results-sem-2", None))
        forms = [e for e in evs if e.get("type") == "results_form"]
        assert forms and forms[0].get("semester") == 2
        # legacy chip never renders marks by itself
        assert "detail" not in ", ".join(e.get("type", "") for e in evs)
    finally:
        db.close()


def test_engine_no_results_semester_renders_safe_token():
    from app.orchestrator.engine import _results_events as results_events
    from app.orchestrator.extractor import extract_entities

    db = SessionLocal()
    try:
        entities = extract_entities("show my semester 6 result")
        assert entities.semester == 6
        evs = list(results_events(db, {"student_id": A["id"], "semester": 2}, "ignored", entities))
        tokens = "".join(e.get("text", "") for e in evs if e.get("type") == "token")
        assert "No result is published for Semester 6" in tokens
    finally:
        db.close()


def test_chat_unauthenticated_still_asks_to_sign_in():
    r = client.post(
        "/api/chat/ask",
        json={"message": "show my results", "chat_id": f"anon_{uuid.uuid4().hex[:8]}", "stream": True},
        headers=STU,
    )  # note: no student cookie on `client`
    assert r.status_code == 200
    assert "auth_form" in r.text or "sign in" in r.text.lower()