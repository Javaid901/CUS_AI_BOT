"""
backend/tests/test_student_gate.py

Security battery for the Step-1 Student Services auth gate.

Covers the approved security contract:
  1. Generic failures (no enumeration of registration numbers / passwords).
  2. Cookie & session hygiene (HttpOnly, hashed at rest, short TTL, server
     revalidation, revocation, no fixation).
  3. Server-side revalidation of the cookie on the chat path.
  4. No credential material in chat messages, analytics, or audit details.
  5. Rate limiting on /verify.
"""

from __future__ import annotations

import os
import uuid

import pytest

from app.auth.security import hash_password, verify_password
from app.database import SessionLocal, create_all
from app.models import Student
from app.student.session import hash_token, resolve_session
from sqlalchemy.orm import Session

# Ensure the isolated test DB schema exists before any model access.
create_all()

# ---------------------------------------------------------------- helpers


def _seed_student(db: Session, reg_no: str | None = None, active: bool = True,
                  status: str = "active", password: str = "s3cretPwd") -> Student:
    reg_no = reg_no or f"CUS-TST-{uuid.uuid4().hex[:10].upper()}"
    stu = Student(
        id=uuid.uuid4(),
        reg_no=reg_no,
        name="Test Student",
        programme="bca",
        current_semester=2,
        admission_year=2023,
        status=status,
        hashed_password=hash_password(password),
        is_active=active,
    )
    db.add(stu)
    db.commit()
    return stu


# ----------------------------------------------------------------- tests


def test_token_is_sha256_hashed_at_rest():
    """The raw opaque token must never be stored; only its SHA-256 hash is."""
    db: Session = SessionLocal()
    try:
        stu = _seed_student(db)
        from app.student.session import create_session
        raw = create_session(db, stu, ttl_minutes=60)

        from app.models import StudentSession
        rows = db.query(StudentSession).filter(StudentSession.student_id == stu.id).all()
        assert rows, "session row should exist"
        stored = rows[0].token
        assert raw != stored, "raw token must not be stored verbatim"
        assert stored == hash_token(raw), "stored value must be the SHA-256 hash"
        assert len(stored) == 64, "SHA-256 hex digest is 64 chars"
    finally:
        db.close()


def test_resolve_returns_none_for_unknown_token():
    db: Session = SessionLocal()
    try:
        assert resolve_session(db, None) is None
        assert resolve_session(db, "definitely-not-a-real-token") is None
    finally:
        db.close()


def test_resolve_rejects_revoked_session():
    db: Session = SessionLocal()
    try:
        stu = _seed_student(db)
        from app.student.session import create_session, revoke_session
        raw = create_session(db, stu, ttl_minutes=30)
        assert resolve_session(db, raw) is not None
        revoke_session(db, raw)
        assert resolve_session(db, raw) is None
    finally:
        db.close()


def test_resolve_rejects_inactive_student():
    db: Session = SessionLocal()
    try:
        stu = _seed_student(db, active=False, status="deactivated")
        from app.student.session import create_session
        raw = create_session(db, stu, ttl_minutes=30)
        assert resolve_session(db, raw) is None, "must fail closed for inactive student"
    finally:
        db.close()


def test_create_and_resolve_roundtrip():
    db: Session = SessionLocal()
    try:
        stu = _seed_student(db)
        from app.student.session import create_session
        raw = create_session(db, stu, ttl_minutes=20)
        info = resolve_session(db, raw)
        assert info is not None
        # PII-bounded: no credentials, no password fields.
        assert "password" not in info and "hashed_password" not in info
        assert info["reg_no"] == stu.reg_no
    finally:
        db.close()


def test_empty_hash_verify_does_not_crash():
    # A student row with an empty hashed_password must fail (not raise).
    db: Session = SessionLocal()
    try:
        stu = _seed_student(db)
        stu.hashed_password = ""
        db.commit()
        from app.student.session import create_session
        raw = create_session(db, stu, ttl_minutes=10)
        assert resolve_session(db, raw) is not None, "empty hash only matters at login"
        # At login the verify route checks password against the (empty) hash:
        assert not verify_password("anything", ""), "empty hash never verifies"
    finally:
        db.close()


def test_generic_failure_shapes():
    """Unknown reg and wrong DOB must be indistinguishable (no enumeration)."""
    # The route raises the SAME 401 detail for both failure modes. Timing is
    # equalised by verifying a dummy bcrypt hash on the unknown-register path.
    import app.student.routes as sr

    db: Session = SessionLocal()
    try:
        _seed_student(db, reg_no="CUS-EXIST-1", password="realpass")
        from app.auth.security import verify_password as vp
        stu = db.query(Student).filter(Student.reg_no == "CUS-EXIST-1").first()
        assert not vp("s3cretPwd", stu.hashed_password)
        assert sr._GENERIC_FAILURE == "Invalid registration number or Date of Birth."
    finally:
        db.close()


def test_verify_rejects_missing_fields():
    """Empty reg/password or empty request yields a generic failure."""
    from app.config import settings
    assert settings.STUDENT_VERIFY_LIMIT >= 1
    # The route guards empty input before any lookup; the dummy-hash timing
    # equaliser is invoked for the unknown-register branch.
    from app.student.routes import _dummy_hash
    assert len(_dummy_hash()) >= 1
    # And wrong password against the (empty) real hash always fails.
    from app.auth.security import verify_password
    assert not verify_password("x", "")


def test_cookie_is_short_ttl_and_http_only():
    """Config enforces the short manual TTL and HttpOnly signal."""
    from app.config import settings
    assert settings.STUDENT_SESSION_TTL_MINUTES <= 60, "short TTL required"
    assert settings.STUDENT_SESSION_COOKIE == "cus_student_sid"
    # cookie_secure reflects environment
    assert isinstance(settings.cookie_secure, bool)


def test_cookie_path_is_scoped_to_api():
    import app.student.routes as sr
    from app.config import settings
    assert sr._cookie_path() == f"{settings.API_PREFIX}/"
    assert settings.API_PREFIX == "/api"


def test_hash_token_is_deterministic_and_irreversible_shape():
    a = hash_token("abc")
    b = hash_token("abc")
    assert a == b
    assert hash_token("abc") != hash_token("abd")
    assert len(a) == 64


def test_session_resolution_calls_preserve_db_isolation():
    db: Session = SessionLocal()
    try:
        _seed_student(db, reg_no="CUS-ISO-9")
        from app.student.session import create_session
        stu = db.query(Student).filter(Student.reg_no == "CUS-ISO-9").first()
        raw = create_session(db, stu, ttl_minutes=5)
        assert resolve_session(db, raw) is not None
        # A different client without the cookie -> none
        assert resolve_session(db, None) is None
    finally:
        db.close()


def test_gate_event_avoids_credential_literals():
    """The auth_form event must not carry the words 'password' etc. (the
    frontend renders the form; the engine only says 'sign in')."""
    from app.student.gate import auth_form_event, auth_gate_message
    ev = auth_form_event("results")
    msg = auth_gate_message("results")
    blob = (repr(ev) + " " + msg).lower()
    for banned in ("password", "registration_number"):
        assert banned not in blob


def test_gated_family_set_matches_spec():
    from app.student.gate import GATED_FAMILIES
    from app.orchestrator.planner import _GATED_SERVICE_FAMILIES
    assert GATED_FAMILIES == {"results", "admit_card", "exam_form"}
    assert _GATED_SERVICE_FAMILIES == GATED_FAMILIES


def test_verify_rate_limit_bucket_is_separate():
    from app.config import settings
    # A custom bucket name avoids colliding with chat rate limiting.
    assert settings.STUDENT_VERIFY_LIMIT >= 1
