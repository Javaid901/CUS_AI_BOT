"""
backend/app/student/session.py

Opaque server-side student sessions for the Student Services auth gate.

Design (mirrors the approved Step-1 spec):
  - A login issues a random unguessable opaque token (secrets.token_urlsafe).
  - Only its SHA-256 hash is stored (StudentSession.token), so a DB leak does
    not leak usable session tokens.
  - The raw token travels exclusively inside the HttpOnly cookie
    (cus_student_sid); it never appears in chat messages, analytics, audit
    detail fields, URLs or storage.
  - Every /api/chat/ask re-resolves the cookie against the DB: revoked or
    expired sessions are rejected server-side, the browser is never trusted.
"""

from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session as OrmSession

from app.config import settings
from app.models import Student, StudentSession


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def hash_token(raw: str) -> str:
    """SHA-256 hex digest of a raw session token (never store the raw token)."""
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def create_session(db: OrmSession, student: Student, ttl_minutes: int | None = None) -> str:
    """Issue a fresh session for `student`, returning the raw cookie token."""
    raw = secrets.token_urlsafe(32)
    ttl = ttl_minutes if ttl_minutes else settings.STUDENT_SESSION_TTL_MINUTES
    db.add(
        StudentSession(
            student_id=student.id,
            token=hash_token(raw),
            expires_at=_utcnow() + timedelta(minutes=ttl),
            revoked=False,
        )
    )
    db.commit()
    return raw


def resolve_session(db: OrmSession, raw_token: str | None):
    """Revalidate the raw cookie token against the DB.

    Returns a minimal, PII-bounded dict (used only to personalise the
    authenticated hub) or None when the session is missing, revoked, expired,
    or the owning student is not active/valid. Never returns credentials.
    """
    if not raw_token:
        return None
    try:
        row = (
            db.query(StudentSession)
            .filter(StudentSession.token == hash_token(raw_token))
            .first()
        )
    except Exception:
        return None
    if row is None or row.revoked:
        return None
    if row.expires_at is None:
        return None
    expires = row.expires_at
    now = _utcnow()
    # SQLite drops tz info on read; normalise both sides to a common basis.
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    if expires <= now:
        return None
    student = db.get(Student, row.student_id)
    if student is None or not student.is_active or student.status != "active":
        return None
    return {
        "student_id": str(student.id),
        "name": student.name,
        "reg_no": student.reg_no,
        "programme": student.programme,
        "semester": student.current_semester,
    }


def revoke_session(db: OrmSession, raw_token: str | None) -> int:
    """Revoke the session matching the raw cookie token (idempotent)."""
    if not raw_token:
        return 0
    row = (
        db.query(StudentSession)
        .filter(StudentSession.token == hash_token(raw_token))
        .first()
    )
    if row is None:
        return 0
    row.revoked = True
    db.commit()
    return 1


def classify_stale_session(db: OrmSession, raw_token: str | None) -> str:
    """Classify an unresolvable session cookie for USER-FACING WORDING ONLY.

    Called only after resolve_session() has already returned None. Picks the
    friendliest gate message without exposing internals:

      - "revoked"  -> the session row exists and was explicitly logged out
                      (gate says the session "ended", never "expired").
      - "expired"  -> time-expired / unknown / deactivated student token
                      (gate says the session "expired"). Used as the safe
                      default for anything else so a wrong guess is never
                      made for the revoked case.

    This is purely a wording hint. The access decision is ALWAYS made by
    resolve_session(); the browser is never trusted.
    """
    if not raw_token:
        return "expired"
    try:
        row = (
            db.query(StudentSession)
            .filter(StudentSession.token == hash_token(raw_token))
            .first()
        )
    except Exception:
        return "expired"
    if row is None:
        return "expired"
    if row.revoked:
        return "revoked"
    return "expired"