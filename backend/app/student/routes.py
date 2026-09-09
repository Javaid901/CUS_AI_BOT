"""
backend/app/student/routes.py

Student Services authentication endpoints.

  POST /api/student/verify   Login: validates reg_no + DOB (the student's
                             password), issues an opaque session token in an
                             HttpOnly cookie.
  POST /api/student/logout   Revokes the current session and clears the cookie.
  GET  /api/student/session  PII-free auth-state check (for the frontend hub).

Security contract (approved Step-1 spec + Phase A):
  - Verification happens ONLY here — never inside /api/chat/ask.
  - A student's password IS their Date of Birth. The raw DOB is normalised to
    the canonical YYYY-MM-DD form (app/student/dob.py) and bcrypt-verified
    against Student.hashed_password.
  - Unknown registration number and wrong DOB return the SAME generic 401
    (timing equalised with a dummy bcrypt hash on both branches). An
    unparseable DOB also fails generically.
  - Fixed 5 req/min/IP rate limit on /verify (separate bucket from chat).
  - Sessions are opaque, short-lived, hashed at rest (see session.py) and
    revalidated server-side on every chat request; the browser is never
    trusted with the decision.
  - Audit rows record only the outcome — never the reg number, DOB or token.
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.auth.security import hash_password, verify_password
from app.config import settings
from app.database import get_db
from app.models import Student
from app.student.dob import normalize_dob
from app.student.session import create_session, resolve_session, revoke_session
from app.utils.logging import audit
from app.utils.rate_limit import endpoint_rate_limit
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import func

router = APIRouter(prefix=f"{settings.API_PREFIX}/student", tags=["student"])

_GENERIC_FAILURE = "Invalid registration number or Date of Birth."

# Timing equaliser: a real bcrypt hash of a dummy value, verified whenever the
# registration number is unknown so both failure paths take ~the same time.
_DUMMY_HASH = ""


def _dummy_hash() -> str:
    global _DUMMY_HASH
    if not _DUMMY_HASH:
        _DUMMY_HASH = hash_password("timing-equalizer-dummy")
    return _DUMMY_HASH


def _client_ip(request: Request) -> str | None:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else None


def _cookie_path() -> str:
    return f"{settings.API_PREFIX}/"


def _cookie_max_age() -> int:
    return settings.STUDENT_SESSION_TTL_MINUTES * 60


class VerifyRequest(BaseModel):
    reg_no: str = Field(default="", max_length=100)
    dob: str = Field(default="", max_length=100)


@router.post("/verify")
async def verify(
    body: VerifyRequest,
    request: Request,
    response: Response,
    db=Depends(get_db),
    _rl: None = Depends(
        endpoint_rate_limit(settings.STUDENT_VERIFY_LIMIT, "student_verify")
    ),
):
    reg = (body.reg_no or "").strip().upper()
    dob = body.dob or ""
    ip = _client_ip(request)

    if not reg or not dob:
        audit(db, "student_verify", actor_role="student", detail="student_verify fail", ip=ip)
        raise HTTPException(status_code=422, detail=_GENERIC_FAILURE)

    # Normalise the DOB to the canonical YYYY-MM-DD form used at hashing time.
    # An unparseable DOB is a generic failure (never a format-specific hint).
    try:
        canonical = normalize_dob(dob)
    except ValueError:
        verify_password(dob, _dummy_hash())
        audit(db, "student_verify", actor_role="student", detail="student_verify fail", ip=ip)
        raise HTTPException(status_code=401, detail=_GENERIC_FAILURE)

    # CASE-INSENSITIVE lookup — the demo seeds store uppercase registration
    # numbers, students should not need to match the exact case.
    student = (
        db.query(Student)
        .filter(func.upper(Student.reg_no) == reg)
        .first()
    )

    # Unknown registration number: equalise timing with a dummy verify so the
    # response time cannot reveal whether a registration number exists.
    if student is None:
        verify_password(canonical, _dummy_hash())
        audit(db, "student_verify", actor_role="student", detail="student_verify fail", ip=ip)
        raise HTTPException(status_code=401, detail=_GENERIC_FAILURE)

    # Wrong DOB: generic failure, no enumeration hint.
    if not verify_password(canonical, student.hashed_password or ""):
        audit(db, "student_verify", actor_role="student", detail="student_verify fail", ip=ip)
        raise HTTPException(status_code=401, detail=_GENERIC_FAILURE)

    # Inactive / de-listed students fail closed with the same generic message.
    if not student.is_active or student.status != "active":
        verify_password(canonical, student.hashed_password)
        audit(db, "student_verify", actor_role="student", detail="student_verify fail", ip=ip)
        raise HTTPException(status_code=401, detail=_GENERIC_FAILURE)

    token = create_session(db, student)
    response.set_cookie(
        key=settings.STUDENT_SESSION_COOKIE,
        value=token,
        max_age=_cookie_max_age(),
        expires=datetime.now(timezone.utc).timestamp() + _cookie_max_age(),
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path=_cookie_path(),
    )
    # A fresh sign-in clears the logout marker so later gates never mislabel
    # this new session as having "ended".
    response.delete_cookie(settings.STUDENT_LOGOUT_MARKER_COOKIE, path=_cookie_path())
    audit(db, "student_verify", actor_role="student", detail="student_verify ok", ip=ip)
    return {"verified": True, "name": student.name}


@router.post("/logout")
async def logout(request: Request, response: Response, db=Depends(get_db)):
    token = request.cookies.get(settings.STUDENT_SESSION_COOKIE)
    revoke_session(db, token)
    response.delete_cookie(settings.STUDENT_SESSION_COOKIE, path=_cookie_path())
    # Non-credential UX marker: the next chat gate says "session has ended"
    # instead of "expired" for this browser. Grants nothing by itself.
    response.set_cookie(
        settings.STUDENT_LOGOUT_MARKER_COOKIE, "1",
        max_age=3600,
        httponly=True,
        secure=settings.cookie_secure,
        samesite="lax",
        path=_cookie_path(),
    )
    return {"logged_out": True}


@router.get("/session")
async def session_status(request: Request, db=Depends(get_db)):
    token = request.cookies.get(settings.STUDENT_SESSION_COOKIE)
    resolved = resolve_session(db, token)
    return {"authenticated": bool(resolved)}