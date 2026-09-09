"""
backend/tests/test_student_session.py

Student Services fixed-timeout session + deterministic chat-logout battery.

Covers the approved security contract for session lifetime & logout:

  1. Fixed 10-minute TTL: login issues a ~600s session; the TTL is NOT
     sliding (activity through the chat path and /api/student/session never
     extends expires_at).
  2. Server-side expiry enforcement: an expired session row is rejected even
     when the browser still holds the cookie, and the expiry gate message is
     shown ("Your Student Services session has expired. ...").
  3. /api/student/logout revokes the session and clears the HttpOnly cookie.
  4. Deterministic chat logout (pre-LLM): "logout" / "log out" / "sign out" /
     "sign me out" / "log me out" (+ please / "from student services"
     wrappers) must revoke the session, clear the cookie, emit a confirmation
     and a `logout` event. None of these are substring-matched, so open-ended
     questions never log anyone out.
  5. Isolation: chat logout terminates ONLY the Student Services session; the
     chat JWT / guest identity keeps working. Normal (non-logout) chat never
     revokes the session.
  6. No credential material in streams, audit, or events.

Tests use one throwaway client jar per scenario (fresh cookie per test).
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.auth.security import hash_password
from app.database import SessionLocal, create_all
from app.main import app
from app.models import (
    AuditLog,
    Student,
    StudentAdmitCard,
    StudentExamForm,
    StudentResult,
    StudentSession,
    User,
)
from app.student.logout import detect_logout_command
from app.student.session import create_session, resolve_session
from app.utils import rate_limit as _rl

create_all()

client = TestClient(app)

SUPER: dict[str, str] = {}
STU: dict[str, str] = {}

_locals: dict[str, str] = {}

_created_student_ids: list[str] = []
_created_user_ids: list[str] = []
_created_admit_card_ids: list[str] = []
_created_exam_form_ids: list[str] = []

_LOGGED_OUT_TEXT = "Logged out successfully."
_NOT_SIGNED_IN_TEXT = "You are not signed in to Student Services."
_EXPIRED_MARKER = "Your Student Services session has expired."
_ENDED_MARKER = "Your Student Services session has ended."

_BANNED_STREAM = ("password", "hashed_password", "dob", "cus_student_sid")


@pytest.fixture(autouse=True)
def _reset_rate_limit_bucket():
    yield
    _rl._HITS.clear()


@pytest.fixture(scope="module", autouse=True)
def _bootstrap():
    db = SessionLocal()
    try:
        for key, role in (("super", "superadmin"), ("user", "student")):
            username = f"__ss_{key}_{uuid.uuid4().hex[:6]}"
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
            _locals[key] = username
        db.commit()
    finally:
        db.close()

    r = client.post("/api/auth/login", data={"username": _locals["super"], "password": "secret123"})
    assert r.status_code == 200, r.text
    SUPER["Authorization"] = f"Bearer {r.json()['access_token']}"
    r = client.post("/api/auth/login", data={"username": _locals["user"], "password": "secret123"})
    assert r.status_code == 200, r.text
    STU["Authorization"] = f"Bearer {r.json()['access_token']}"

    yield

    db = SessionLocal()
    try:
        for uid in _created_student_ids:
            db.query(StudentResult).filter(StudentResult.student_id == str(uid)).delete()
            db.query(StudentSession).filter(StudentSession.student_id == str(uid)).delete()
            db.query(Student).filter(Student.id == str(uid)).delete()
        for cid in _created_admit_card_ids:
            db.query(StudentAdmitCard).filter(StudentAdmitCard.id == str(cid)).delete()
        for fid in _created_exam_form_ids:
            db.query(StudentExamForm).filter(StudentExamForm.id == str(fid)).delete()
        for uid in _created_user_ids:
            db.query(AuditLog).filter(AuditLog.actor_id == uid).delete()
            db.query(User).filter(User.id == uid).delete()
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------- helpers


def _create_student(reg: str | None = None, dob: str = "2005-06-15") -> dict:
    body = {
        "reg_no": reg or f"CUS-SS-{uuid.uuid4().hex[:6].upper()}",
        "name": "Session Student",
        "dob": dob,
        "programme": "bca",
        "current_semester": 2,
        "admission_year": 2023,
        "is_active": True,
    }
    r = client.post("/api/admin/students", json=body, headers=SUPER)
    assert r.status_code == 201, r.text
    dto = r.json()
    _created_student_ids.append(dto["id"])
    return dto


def _login(client_: TestClient, reg: str, dob: str = "2005-06-15"):
    r = client_.post("/api/student/verify", json={"reg_no": reg, "dob": dob})
    assert r.status_code == 200, r.text
    return r


def _as_aware(dt: datetime) -> datetime:
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _session_rows(db, student_id: str) -> list:
    return (
        db.query(StudentSession)
        .filter(StudentSession.student_id == str(student_id))
        .order_by(StudentSession.expires_at.desc())
        .all()
    )


def _sse_events(text: str) -> list[tuple[str, dict]]:
    events = []
    for block in text.split("\n\n"):
        ev = ""
        data_lines = []
        for line in block.splitlines():
            if line.startswith("event:"):
                ev = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].lstrip())
        if not data_lines:
            continue
        payload = "\n".join(data_lines)
        try:
            events.append((ev, json.loads(payload)))
        except Exception:
            events.append((ev, {"type": "token", "text": payload}))
    return events


def _chat(client_: TestClient, message: str) -> tuple[int, str, list[tuple[str, dict]]]:
    r = client_.post(
        "/api/chat/ask",
        json={"message": message, "chat_id": f"ss_chat_{uuid.uuid4().hex[:8]}", "stream": True},
        headers=STU,
    )
    assert r.status_code == 200, r.text
    return r.status_code, r.text, _sse_events(r.text)


def _token_text(events: list[tuple[str, dict]]) -> str:
    return " ".join(
        e[1].get("text", "")
        for e in events if e[1].get("type") == "token"
    )


def _has_logout_event(events: list[tuple[str, dict]]) -> bool:
    return any(ev_name == "logout" for ev_name, _ in events)


def _set_cookie_headers(r) -> str:
    headers = r.headers
    if hasattr(headers, "get_list"):
        return "; ".join(headers.get_list("set-cookie")).lower()
    return (headers.get("set-cookie") or "").lower()


def _post_chat(client_: TestClient, message: str):
    """POST a chat turn and return the raw response (headers matter for SSO)."""
    r = client_.post(
        "/api/chat/ask",
        json={"message": message, "chat_id": f"ss_ux_{uuid.uuid4().hex[:8]}", "stream": True},
        headers=STU,
    )
    assert r.status_code == 200, r.text
    return r


def _chat_cid(client_: TestClient, message: str, chat_id: str) -> tuple[str, list[tuple[str, dict]]]:
    """Chat turn on an EXPLICIT conversation (needed for resume-after-re-login)."""
    r = client_.post(
        "/api/chat/ask",
        json={"message": message, "chat_id": chat_id, "stream": True},
        headers=STU,
    )
    assert r.status_code == 200, r.text
    return r.text, _sse_events(r.text)


def _seed_results(student_id: str) -> None:
    db = SessionLocal()
    try:
        for sem, code, name in (
            (1, "CUS101", "Mathematics"),
            (2, "CUS102", "Computer Science"),
        ):
            db.add(StudentResult(
                id=uuid.uuid4(),
                student_id=str(student_id),
                semester=sem,
                exam_type="Regular",
                subject_name=name,
                subject_code=code,
                internal_marks=25,
                external_marks=50,
                total_marks=75,
                max_marks=100,
                grade="B",
                sgpa="7.50",
                cgpa="7.40",
                status="pass",
                academic_year="2023-2024",
            ))
        db.commit()
    finally:
        db.close()


def _seed_admit_card(student_id: str, semester: int = 1) -> None:
    db = SessionLocal()
    try:
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
            subjects=json.dumps(["Mathematics", "Computer Science"]),
            instructions=json.dumps([
                "Bring this admit card to the examination hall",
                "Carry a valid photo ID (Aadhaar/College ID)",
            ]),
            issued_date="01-May-2024",
            academic_year="2023-2024",
        )
        db.add(r)
        db.commit()
        _created_admit_card_ids.append(str(r.id))
    finally:
        db.close()


def _seed_exam_form(student_id: str, semester: int = 1) -> None:
    db = SessionLocal()
    try:
        r = StudentExamForm(
            id=uuid.uuid4(),
            student_id=uuid.UUID(student_id),
            semester=semester,
            exam_type="Regular",
            form_status="Pending",
            subjects=json.dumps(["Mathematics", "Computer Science"]),
            academic_year="2023-2024",
        )
        db.add(r)
        db.commit()
        _created_exam_form_ids.append(str(r.id))
    finally:
        db.close()


def _event_names(events: list[tuple[str, dict]]) -> list[str]:
    return [e[0] for e in events]


# ------------------------------------------------------------------ config


def test_ttl_config_is_fixed_ten_minutes():
    from app.config import settings
    from app.student.routes import _cookie_max_age
    assert settings.STUDENT_SESSION_TTL_MINUTES == 10
    assert _cookie_max_age() == 600


def test_verify_sets_600s_cookie_with_httponly_and_api_path():
    dto = _create_student()
    c = TestClient(app)
    r = _login(c, dto["reg_no"])
    sc = _set_cookie_headers(r)
    assert "cus_student_sid=" in sc
    assert "max-age=600" in sc
    assert "httponly" in sc
    assert "path=/api/" in sc


# ---------------------------------------------------------- TTL semantics


def test_login_creates_session_with_ten_minute_expiry_window():
    dto = _create_student()
    c = TestClient(app)
    _login(c, dto["reg_no"])
    db = SessionLocal()
    try:
        rows = _session_rows(db, dto["id"])
        assert len(rows) == 1
        exp = _as_aware(rows[0].expires_at)
        now = datetime.now(timezone.utc)
        assert exp > now
        assert exp - now <= timedelta(seconds=610)
        assert exp - now >= timedelta(seconds=590)
    finally:
        db.close()


def test_ttl_is_fixed_not_extended_by_resolve_activity():
    """Repeated resolves (chat + session probes) never slide expires_at."""
    db = SessionLocal()
    dto = _create_student()
    try:
        stu = db.query(Student).filter(Student.id == dto["id"]).first()
        raw = create_session(db, stu)
        row = db.query(StudentSession).filter(StudentSession.student_id == str(stu.id)).first()
        expires_at = row.expires_at

        for _ in range(5):
            assert resolve_session(db, raw) is not None

        row2 = db.query(StudentSession).filter(StudentSession.student_id == str(stu.id)).first()
        assert row2.expires_at == expires_at, "fixed TTL: activity must not extend expiry"
    finally:
        db.close()


def test_ttl_is_fixed_not_extended_by_chat_and_session_probes():
    dto = _create_student()
    c = TestClient(app)
    _login(c, dto["reg_no"])
    db = SessionLocal()
    try:
        row = _session_rows(db, dto["id"])[0]
        expires_at = row.expires_at
    finally:
        db.close()

    # Activity through the real paths the browser uses: chat asks (which
    # server-side resolve the cookie) and the PII-free status probe.
    for _ in range(3):
        _chat(c, "hello")
        r = c.get("/api/student/session")
        assert r.status_code == 200 and r.json()["authenticated"] is True

    db = SessionLocal()
    try:
        row = _session_rows(db, dto["id"])[0]
        assert row.expires_at == expires_at, "fixed TTL: chat activity must not extend expiry"
    finally:
        db.close()


def test_expired_session_rejected_server_side_until_relogin():
    """A stale cookie whose DB row expired yields 401 and an expired gate."""
    dto = _create_student()
    c = TestClient(app)
    _login(c, dto["reg_no"])
    db = SessionLocal()
    try:
        row = _session_rows(db, dto["id"])[0]
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
    finally:
        db.close()

    assert c.get("/api/student/results").status_code == 401
    assert c.get("/api/student/session").json()["authenticated"] is False

    _chat(c, "show my results")
    _, _, events = _chat(c, "show my results")
    tokens = _token_text(events)
    assert "You must first verify" not in tokens
    assert _EXPIRED_MARKER in tokens
    assert any(ev == "auth_form" for ev, _ in events)

    # Re-login issues a fresh session and restores access.
    _login(c, dto["reg_no"])
    ok, _, events2 = _chat(c, "show my results")
    assert ok == 200
    assert any(ev == "auth_form" for ev, _ in events2) is False
    assert _EXPIRED_MARKER not in _token_text(events2)
    assert c.get("/api/student/session").json()["authenticated"] is True


# ------------------------------------------------------- logout endpoint


def test_logout_endpoint_revokes_row_and_clears_cookie():
    dto = _create_student()
    c = TestClient(app)
    _login(c, dto["reg_no"])
    assert c.get("/api/student/session").json()["authenticated"] is True

    r = c.post("/api/student/logout")
    assert r.status_code == 200
    sc = _set_cookie_headers(r)
    assert "cus_student_sid=" in sc and ("max-age=0" in sc or "expires=thu, 01 jan 1970" in sc)

    db = SessionLocal()
    try:
        rows = _session_rows(db, dto["id"])
        assert rows and rows[0].revoked is True
    finally:
        db.close()

    assert c.get("/api/student/session").json()["authenticated"] is False
    assert c.get("/api/student/results").status_code == 401


# ------------------------------------------------------- chat logout


def _assert_chat_logout_revocations(dto: dict, message: str):
    c = TestClient(app)
    _login(c, dto["reg_no"])
    db = SessionLocal()
    try:
        before = _session_rows(db, dto["id"])
        assert before and before[0].revoked is False
    finally:
        db.close()

    r = c.post(
        "/api/chat/ask",
        json={"message": message, "chat_id": f"ss_logout_{uuid.uuid4().hex[:8]}", "stream": True},
        headers=STU,
    )
    assert r.status_code == 200
    events = _sse_events(r.text)
    assert _LOGGED_OUT_TEXT in _token_text(events)
    assert _has_logout_event(events)
    assert any(ev == "done" for ev, _ in events)

    sc = _set_cookie_headers(r)
    assert "cus_student_sid=" in sc and ("max-age=0" in sc or "expires=thu, 01 jan 1970" in sc)

    db = SessionLocal()
    try:
        rows = _session_rows(db, dto["id"])
        assert rows and rows[0].revoked is True, f"chat '{message}' must revoke the session"
    finally:
        db.close()

    assert c.get("/api/student/session").json()["authenticated"] is False
    return events


def test_chat_logout_command_flow():
    dto = _create_student()
    events = _assert_chat_logout_revocations(dto, "logout")
    banned_blob = " ".join(repr(e) for _, e in events).lower()
    for banned in _BANNED_STREAM:
        assert banned not in banned_blob


@pytest.mark.parametrize("message", [
    "log out",
    "sign out",
    "sign me out",
    "log me out",
    "please log out",
    "please sign me out",
    "log me out from student services",
    "logout from student services",
    "Kindly log me out of student services",
    "LOG OUT!",
    "Logout.",
])
def test_chat_logout_command_variants_revoke(message: str):
    dto = _create_student()
    _assert_chat_logout_revocations(dto, message)


def test_chat_logout_when_not_signed_in_is_idempotent():
    c = TestClient(app)
    _, text, events = _chat(c, "logout")
    assert _NOT_SIGNED_IN_TEXT in _token_text(events)
    assert _has_logout_event(events)
    assert any(ev == "done" for ev, _ in events)

    # The chat identity itself is untouched: a follow-up message still works.
    ok, _, events2 = _chat(c, "hello")
    assert ok == 200
    assert not _has_logout_event(events2)


def test_chat_logout_does_not_affect_chat_authentication():
    dto = _create_student()
    c = TestClient(app)
    _login(c, dto["reg_no"])
    _, _, events = _chat(c, "logout")
    assert _LOGGED_OUT_TEXT in _token_text(events)

    # Same JWT still serves the chat; the guest conversation is intact.
    ok, _, events2 = _chat(c, "hello")
    assert ok == 200
    assert any(ev == "done" for ev, _ in events2)


def test_normal_chat_does_not_revoke_session():
    dto = _create_student()
    c = TestClient(app)
    _login(c, dto["reg_no"])

    _, _, events = _chat(c, "show my results")
    assert not _has_logout_event(events)
    assert any(ev == "auth_form" for ev, _ in events) is False

    db = SessionLocal()
    try:
        rows = _session_rows(db, dto["id"])
        assert rows and rows[0].revoked is False
    finally:
        db.close()
    assert c.get("/api/student/session").json()["authenticated"] is True


# ------------------------------------------------------ detector (pure)


@pytest.mark.parametrize("message", [
    "logout", "LOGOUT", "Logout", "logout!", "Log OUT.",
    "log out", "log   out",
    "sign out", "sign me out",
    "log me out",
    "please logout", "please log out", "please sign me out", "kindly log me out",
    "log me out from student services",
    "logout from student services",
    "sign me out of student services",
    "  LOGOUT  ",
])
def test_detector_accepts_command_set(message: str):
    assert detect_logout_command(message) is True


@pytest.mark.parametrize("message", [
    "", None, " ", "hello", "logout now", "log out now",
    "what does logout mean", "how do i log out", "tell me about signing out",
    "i want to log out", "show me how to log out", "do not log me out",
    "log me out please now", "how to sign out of my account",
    "my results", "back", "logout today", "sign out tomorrow",
])
def test_detector_rejects_non_commands(message: str):
    assert detect_logout_command(message) is False


# ------------------------------------------------------------- audit


def test_logout_audit_records_outcome_only():
    dto = _create_student()
    c = TestClient(app)
    _login(c, dto["reg_no"])
    _chat(c, "logout")

    db = SessionLocal()
    try:
        rows = (
            db.query(AuditLog)
            .filter(AuditLog.action == "student_logout")
            .order_by(AuditLog.id.desc())
            .all()
        )
        assert rows, "a student_logout audit row must exist"
        for row in rows:
            detail = (row.detail or "").lower()
            for banned in ("password", "hashed_password", "dob", "cus_student_sid", dto["reg_no"].lower()):
                assert banned not in detail
    finally:
        db.close()


# -------------------------------------------------------- gate wording


def test_expired_gate_message_wording():
    from app.student.gate import expired_gate_message, logged_out_gate_message, auth_gate_message
    assert expired_gate_message(None) == "Your Student Services session has expired. Please log in again."
    assert expired_gate_message("results") == "Your Student Services session has expired. Please log in again."
    assert logged_out_gate_message(None) == "Your Student Services session has ended. Please log in again."
    # The distinct messages: expired vs ended vs first-login.
    assert expired_gate_message(None) != logged_out_gate_message(None)
    assert "has ended" not in expired_gate_message(None)
    assert "has expired" not in logged_out_gate_message(None)
    assert "expired" not in auth_gate_message(None)
    assert "ended" not in auth_gate_message(None)


# ------------------------------------------------------- UX: manual logout


def test_manual_logout_success_message_event_revocation_and_cookie_clear():
    dto = _create_student()
    c = TestClient(app)
    _login(c, dto["reg_no"])

    r = _post_chat(c, "logout")
    events = _sse_events(r.text)
    tokens = _token_text(events)

    assert tokens == "Logged out successfully."
    assert _has_logout_event(events)

    # The SAME response contains no auth_form: logout must never pop the
    # sign-in window immediately (the window only ever appears on the NEXT
    # protected request).
    assert "auth_form" not in _event_names(events)
    assert "Your Student Services session has expired" not in tokens
    assert "Your Student Services session has ended" not in tokens

    sc = _set_cookie_headers(r)
    assert "cus_student_sid=" in sc and "max-age=0" in sc
    assert "cus_student_out=1" in sc and "max-age=3600" in sc

    db = SessionLocal()
    try:
        rows = _session_rows(db, dto["id"])
        assert rows and rows[0].revoked is True
    finally:
        db.close()


def test_logout_when_not_signed_in_sets_no_marker():
    c = TestClient(app)
    r = _post_chat(c, "logout")
    sc = _set_cookie_headers(r)
    assert "cus_student_out=" not in sc
    events = _sse_events(r.text)
    assert _NOT_SIGNED_IN_TEXT in _token_text(events)
    assert "Logged out successfully." not in _token_text(events)


@pytest.mark.parametrize("phrase,family", [
    ("show my results", "results"),
    ("show my admit card", "admit_card"),
    ("show my exam form", "exam_form"),
])
def test_access_after_manual_logout_shows_ended_message_then_login_window(phrase, family):
    dto = _create_student()
    c = TestClient(app)
    _login(c, dto["reg_no"])
    _post_chat(c, "logout")

    _, _, events = _chat(c, phrase)
    tokens = _token_text(events)

    assert _ENDED_MARKER in tokens
    assert "Please log in again." in tokens
    assert "expired" not in tokens.lower()            # never a false "expired"
    assert "You must first verify" not in tokens      # never internal HTTP wording

    forms = [e for e in events if e[0] == "auth_form"]
    assert forms, "the login window must open after logout on protected access"
    assert forms[0][1]["payload"]["family"] == family
    assert any(e == "logout" for e in _event_names(events)) is False
    assert "done" in _event_names(events)


# --------------------------------------------------- UX: actual expiry


def test_actual_expiry_shows_exact_expired_message_and_login_window():
    dto = _create_student()
    c = TestClient(app)
    _login(c, dto["reg_no"])

    db = SessionLocal()
    try:
        row = _session_rows(db, dto["id"])[0]
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
    finally:
        db.close()

    _, _, events = _chat(c, "show my results")
    tokens = _token_text(events)

    assert tokens == "Your Student Services session has expired. Please log in again."
    assert "ended" not in tokens

    forms = [e for e in events if e[0] == "auth_form"]
    assert forms and forms[0][1]["payload"]["family"] == "results"
    assert "done" in _event_names(events)


@pytest.mark.parametrize("phrase,family", [
    ("show my results", "results"),
    ("show my admit card", "admit_card"),
    ("show my exam form", "exam_form"),
])
def test_fresh_browser_with_unresolved_cookie_never_claims_expired(phrase, family):
    """A no-cookie browser always gets the generic first-login ask, not an
    expiry claim — expiry wording requires an actual (lapsed) session row."""
    dto = _create_student()
    c = TestClient(app)
    _login(c, dto["reg_no"])

    db = SessionLocal()
    try:
        row = _session_rows(db, dto["id"])[0]
        row.revoked = True
        db.commit()
    finally:
        db.close()
    c.cookies.clear()  # simulate a fresh browser: no cookie at all

    _, _, events = _chat(c, phrase)
    tokens = _token_text(events)
    assert "expired" not in tokens.lower()
    assert "ended" not in tokens.lower()
    assert "sign in" in tokens
    forms = [e for e in events if e[0] == "auth_form"]
    assert forms and forms[0][1]["payload"]["family"] == family
    assert "done" in _event_names(events)


# --------------------------------------- UX: re-login creates new session


def test_relogin_after_expiry_creates_fresh_session_and_resumes_request():
    dto = _create_student()
    _seed_results(dto["id"])
    c = TestClient(app)
    _login(c, dto["reg_no"])

    db = SessionLocal()
    try:
        row = _session_rows(db, dto["id"])[0]
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
        before = len(_session_rows(db, dto["id"]))
    finally:
        db.close()

    chat_id = f"ss_resume_{uuid.uuid4().hex[:8]}"
    _, events = _chat_cid(c, "show my results", chat_id)
    assert "Your Student Services session has expired. Please log in again." in _token_text(events)
    assert any(e[0] == "auth_form" for e in events)

    # The frontend never re-verifies silently here: the student signs in again
    # through the SAME login form, which issues a brand-new session row.
    _login(c, dto["reg_no"])
    db = SessionLocal()
    try:
        rows = _session_rows(db, dto["id"])
        assert len(rows) == before + 1, "re-login must mint a NEW session"
        assert rows[0].revoked is False
    finally:
        db.close()

    # "Student Services" (sent by ssfSuccess after a successful sign-in) on the
    # SAME conversation resumes the ORIGINAL Results request, not the bare hub.
    _, events2 = _chat_cid(c, "Student Services", chat_id)
    resize = [e for e in events2 if e[0] == "results_form"]
    assert resize, "resumed Results request must render the semester+roll form"
    sems = [s["semester"] for s in (resize[0][1].get("semesters") or [])]
    assert 1 in sems and 2 in sems


def test_relogin_after_logout_clears_ended_marker():
    dto = _create_student()
    c = TestClient(app)
    _login(c, dto["reg_no"])

    r_logout = _post_chat(c, "logout")
    assert "cus_student_out=1" in _set_cookie_headers(r_logout)

    r_verify = _login(c, dto["reg_no"])
    assert "cus_student_out=" in _set_cookie_headers(r_verify)
    assert "max-age=0" in _set_cookie_headers(r_verify)

    # With a valid fresh session the gate never falls back to "ended".
    _, _, events = _chat(c, "show my results")
    tokens = _token_text(events)
    assert _ENDED_MARKER not in tokens
    assert "expired" not in tokens.lower()
    assert any(e[0] == "auth_form" for e in events) is False


def test_relogin_endpoint_after_expiry_continues_without_marker():
    """After a pure expiry (no manual logout) the marker must never appear."""
    dto = _create_student()
    c = TestClient(app)
    _login(c, dto["reg_no"])

    db = SessionLocal()
    try:
        row = _session_rows(db, dto["id"])[0]
        row.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        db.commit()
    finally:
        db.close()

    r_verify = _login(c, dto["reg_no"])
    sc = _set_cookie_headers(r_verify)
    assert "cus_student_sid=" in sc and "max-age=600" in sc
    # No logout ever happened: the marker may only appear as a no-op clearing,
    # never as a fresh "ended" signal for the future.
    if "cus_student_out=" in sc:
        assert "max-age=0" in sc


# ----------------------------------------------------- UX: security


def test_gated_request_after_logout_reveals_no_data_and_no_token():
    dto = _create_student()
    _seed_results(dto["id"])
    c = TestClient(app)
    _login(c, dto["reg_no"])
    _post_chat(c, "logout")

    _, _, events = _chat(c, "show my results")
    blob = " ".join(repr(e) for e in events).lower()

    assert "results_form" not in blob             # no result form / picker leaked
    assert "fields" not in blob                    # no grade detail leaked
    assert _BANNED_STREAM[3] not in blob.lower()   # cus_student_sid never echoed
    assert any(e[0] == "auth_form" for e in events)
    assert any(e[0] == "done" for e in events)


def test_revoked_but_cookie_still_present_shows_ended_not_expired():
    """A stale browser that still holds a revoked cookie (e.g. logout from a
    different device) is classified "revoked" => "has ended", never "expired",
    because the row exists and is explicitly revoked."""
    dto = _create_student()
    c = TestClient(app)
    _login(c, dto["reg_no"])

    db = SessionLocal()
    try:
        row = _session_rows(db, dto["id"])[0]
        row.revoked = True
        db.commit()
    finally:
        db.close()

    _, _, events = _chat(c, "show my admit card")
    tokens = _token_text(events)
    assert _ENDED_MARKER in tokens
    assert "expired" not in tokens.lower()
    assert any(e[0] == "auth_form" for e in events)


# -------------------------------------------------- duplicate-hub regression


def _hub_options(events: list[tuple[str, dict]]) -> list[tuple[str, dict]]:
    """Options events that ARE the Student Services hub (unique by title)."""
    return [e for e in events if e[0] == "options" and e[1].get("title") == "Student Services"]


def _option_ids(events: list[tuple[str, dict]]) -> list[str]:
    return [
        str(o.get("id"))
        for e in events if e[0] == "options"
        for o in (e[1].get("options") or [])
    ]


def test_hub_emitted_once_then_results_without_second_hub():
    """Requirement: initial hub emitted exactly once; selecting Results must
    NOT generate another Student Services hub after the results form."""
    dto = _create_student()
    _seed_results(dto["id"])
    c = TestClient(app)
    _login(c, dto["reg_no"])
    cid = f"ss_dup_r_{uuid.uuid4().hex[:8]}"

    _, hub_events = _chat_cid(c, "Student Services", cid)
    hub = _hub_options(hub_events)
    assert len(hub) == 1, f"initial hub emitted {len(hub)}x, want exactly 1"
    assert "identity is verified" in (hub[0][1].get("message") or "")
    assert set(_option_ids(hub_events)) == {"student_results", "student_admit_card", "student_exam_form"}

    _, results_events = _chat_cid(c, "student_results", cid)
    resize = [e for e in results_events if e[0] == "results_form"]
    assert resize, "Results must render the semester+roll form (not a hub)"
    sems = [s["semester"] for s in (resize[0][1].get("semesters") or [])]
    assert 1 in sems and 2 in sems
    assert _option_ids(results_events) == [], "no chips may replace the results form"
    assert _hub_options(results_events) == [], "a second Student Services hub appeared after Results"
    assert len(_hub_options(hub_events + results_events)) == 1


def test_hub_emitted_once_then_admit_card_without_second_hub():
    dto = _create_student()
    _seed_admit_card(dto["id"], 1)
    _seed_admit_card(dto["id"], 2)
    c = TestClient(app)
    _login(c, dto["reg_no"])
    cid = f"ss_dup_a_{uuid.uuid4().hex[:8]}"

    _, hub_events = _chat_cid(c, "Student Services", cid)
    assert len(_hub_options(hub_events)) == 1

    _, card_events = _chat_cid(c, "student_admit_card", cid)
    ids = _option_ids(card_events)
    assert "admit_card_sem-1" in ids and "admit_card_sem-2" in ids
    assert _hub_options(card_events) == [], "a second Student Services hub appeared after Admit Card"
    assert len(_hub_options(hub_events + card_events)) == 1


def test_hub_emitted_once_then_exam_form_without_second_hub():
    dto = _create_student()
    _seed_exam_form(dto["id"], 1)
    _seed_exam_form(dto["id"], 2)
    c = TestClient(app)
    _login(c, dto["reg_no"])
    cid = f"ss_dup_f_{uuid.uuid4().hex[:8]}"

    _, hub_events = _chat_cid(c, "Student Services", cid)
    assert len(_hub_options(hub_events)) == 1

    _, form_events = _chat_cid(c, "student_exam_form", cid)
    ids = _option_ids(form_events)
    assert "exam_formregular1" in ids and "exam_formregular2" in ids
    assert _hub_options(form_events) == [], "a second Student Services hub appeared after Exam Form"
    assert len(_hub_options(hub_events + form_events)) == 1


def test_typed_service_switching_never_emits_hub_between_services():
    """All four required direct-switch pairs, sequenced through one live
    conversation: Results → "admit card" → Admit Card, Admit Card →
    "results" → Results, Results → "exam form" → Exam Form, Exam Form →
    "results" → Results. No hub is emitted between any of the turns."""
    dto = _create_student()
    _seed_results(dto["id"])
    _seed_admit_card(dto["id"], 1)
    _seed_admit_card(dto["id"], 2)
    _seed_exam_form(dto["id"], 1)
    _seed_exam_form(dto["id"], 2)
    c = TestClient(app)
    _login(c, dto["reg_no"])
    cid = f"ss_dup_sw_{uuid.uuid4().hex[:8]}"

    all_hub_events = []
    _, hub_events = _chat_cid(c, "Student Services", cid)
    all_hub_events += hub_events
    assert len(_hub_options(hub_events)) == 1

    def _direct(message: str, marker: str, label: str):
        _, evs = _chat_cid(c, message, cid)
        if marker == "results_form":
            assert any(e[0] == "results_form" for e in evs), f"{label}: results form not opened"
        else:
            assert marker in _option_ids(evs), f"{label}: picker not opened"
        assert _hub_options(evs) == [], f"{label}: Student Services hub appeared between services"
        all_hub_events.extend(evs)

    _direct("student_results", "results_form", "entry → Results")
    _direct("show my admit card", "admit_card_sem-1", "Results → Admit Card")
    _direct("show my results", "results_form", "Admit Card → Results")
    _direct("show my exam form", "exam_formregular1", "Results → Exam Form")
    _direct("show my results", "results_form", "Exam Form → Results")

    assert len(_hub_options(all_hub_events)) == 1


def test_explicit_back_returns_navigation_without_reemitting_hub():
    """The hub must only ever appear on explicit entry; a "back" turn resolves
    through the normal navigation response and must never resurrect a Student
    Services hub on its own."""
    dto = _create_student()
    _seed_results(dto["id"])
    c = TestClient(app)
    _login(c, dto["reg_no"])
    cid = f"ss_back_{uuid.uuid4().hex[:8]}"

    _, hub_events = _chat_cid(c, "Student Services", cid)
    assert len(_hub_options(hub_events)) == 1

    _, results_events = _chat_cid(c, "student_results", cid)
    assert any(e[0] == "results_form" for e in results_events)
    assert _hub_options(results_events) == []

    _, back_events = _chat_cid(c, "back", cid)
    assert any(e[0] == "options" for e in back_events), "explicit Back must resolve to navigation options"
    assert _hub_options(back_events) == [], "explicit Back must not emit the Student Services hub either"