"""
backend/tests/test_admin_session_refresh.py

Regression test for the admin idle-session fix.

The frontend stores both the access token AND the refresh token issued by
POST /api/auth/login, and proactively refreshes the access token (via
POST /api/auth/refresh) before its 60-minute expiry. This lets the Super
Admin remain logged in while the server keeps running (Phase 3 stability).

This suite verifies the backend contract the frontend now relies on:

  1. Login returns BOTH an access_token and a refresh_token.
  2. The refresh_token exchanges for a NEW access_token that authenticates
     on a protected admin endpoint (round-trip proof).
  3. The previously-issued access_token still works until its own expiry
     (refresh rotates the access token, not the session).
  4. A fake/forged refresh_token is rejected with 401.
  5. A revoked refresh_token is rejected with 401.

Run:  python -m pytest tests/test_admin_session_refresh.py -q
"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from app.auth.security import hash_password
from app.database import SessionLocal, create_all
from app.main import app
from app.models import RefreshToken, User

create_all()

_CLIENT = TestClient(app)

# Users this module creates, removed at module teardown so the shared
# throwaway database returns to its baseline for count-based suites.
_CREATED_USER_IDS: list[str] = []
_CREATED_REFRESH_IDS: list[str] = []


def _cleanup() -> None:
    from app.models import AuditLog

    db = SessionLocal()
    try:
        for rid in _CREATED_REFRESH_IDS:
            db.query(RefreshToken).filter(RefreshToken.id == rid).delete()
        for uid in _CREATED_USER_IDS:
            db.query(AuditLog).filter(AuditLog.actor_id == uid).delete()
            db.query(User).filter(User.id == uid).delete()
        db.commit()
    finally:
        db.close()


def _create_admin() -> "tuple[User, str]":
    username = f"__sess_{uuid.uuid4().hex[:8]}"
    password = "secret123"
    db = SessionLocal()
    try:
        user = User(
            id=uuid.uuid4(),
            username=username,
            email=f"{username}@test.local",
            hashed_password=hash_password(password),
            role="superadmin",
            is_active=True,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        _CREATED_USER_IDS.append(str(user.id))
    finally:
        db.close()
    return user, password


def _login(username: str, password: str) -> "dict":
    return _CLIENT.post(
        "/api/auth/login", data={"username": username, "password": password}
    )


def test_login_returns_access_and_refresh_tokens() -> None:
    user, password = _create_admin()
    r = _login(user.username, password)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["access_token"], "access_token must be present"
    assert body["refresh_token"], "refresh_token must be present (frontend uses it)"
    assert body["token_type"] == "bearer"


def test_refresh_token_exchanges_for_new_usable_access_token() -> None:
    user, password = _create_admin()
    login = _login(user.username, password)
    refresh_token = login.json()["refresh_token"]

    rr = _CLIENT.post("/api/auth/refresh", json={"refresh_token": refresh_token})
    assert rr.status_code == 200, rr.text
    new_access = rr.json()["access_token"]
    assert new_access, "refresh must issue a new access token"

    # New access token authenticates on a protected admin endpoint.
    me = _CLIENT.get("/api/admin/profile", headers={"Authorization": f"Bearer {new_access}"})
    assert me.status_code == 200, me.text
    assert me.json()["username"] == user.username


def test_previous_access_token_still_valid_shortly_after_refresh() -> None:
    user, password = _create_admin()
    login = _login(user.username, password)
    old_access = login.json()["access_token"]
    refresh_token = login.json()["refresh_token"]

    # Rotate the access token.
    rr = _CLIENT.post("/api/auth/refresh", json={"refresh_token": refresh_token})
    assert rr.status_code == 200

    # The old access token is still valid (it expires on its own 60-min clock).
    me = _CLIENT.get("/api/admin/profile", headers={"Authorization": f"Bearer {old_access}"})
    assert me.status_code == 200, me.text


def test_forged_refresh_token_rejected() -> None:
    r = _CLIENT.post("/api/auth/refresh", json={"refresh_token": "not-a-real-token"})
    assert r.status_code == 401, r.text


def test_revoked_refresh_token_rejected() -> None:
    user, password = _create_admin()
    login = _login(user.username, password)
    refresh_token = login.json()["refresh_token"]

    db = SessionLocal()
    try:
        row = db.query(RefreshToken).filter(RefreshToken.token == refresh_token).first()
        assert row is not None, "login must persist the refresh token in the DB"
        row.revoked = True
        db.commit()
        _CREATED_REFRESH_IDS.append(str(row.id))
    finally:
        db.close()

    rr = _CLIENT.post("/api/auth/refresh", json={"refresh_token": refresh_token})
    assert rr.status_code == 401, rr.text
    msg = (rr.json().get("error") or {}).get("message") or rr.json().get("detail")
    assert msg == "Invalid refresh token"