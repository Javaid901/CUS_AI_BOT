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


# --------------------------------------------------------------------------- #
# Schema-repair regression (the login outage this guards against)
# --------------------------------------------------------------------------- #
def test_legacy_refresh_tokens_schema_is_repaired_without_data_loss(
    monkeypatch,
) -> None:
    """A database created by the earlier `token_hash` revision must gain the
    `token` column the RefreshToken ORM expects — otherwise every login dies
    with UndefinedColumn: column "token" of relation "refresh_tokens" does not
    exist. The repair must keep every pre-existing refresh token, must be
    idempotent, and must leave an already-correct schema untouched.
    """
    import os
    import tempfile

    import sqlalchemy as sa
    from app import database as database_module

    legacy_db = os.path.join(tempfile.mkdtemp(prefix="cus_legacy_rt_"), "legacy.db")
    legacy_engine = sa.create_engine(f"sqlite:///{legacy_db}", future=True)
    # Exactly the shape the live PostgreSQL database had: `token_hash` instead
    # of `token`, and already populated with refresh tokens.
    with legacy_engine.begin() as conn:
        conn.execute(
            sa.text(
                "CREATE TABLE refresh_tokens ("
                " id CHAR(32) NOT NULL PRIMARY KEY,"
                " user_id CHAR(32) NOT NULL,"
                " token_hash VARCHAR(64),"
                " expires_at DATETIME NOT NULL,"
                " created_at DATETIME NOT NULL,"
                " revoked BOOLEAN NOT NULL)"
            )
        )
        for i in range(3):
            conn.execute(
                sa.text(
                    "INSERT INTO refresh_tokens"
                    " (id, user_id, token_hash, expires_at, created_at, revoked)"
                    " VALUES (:id, :uid, :h, '2030-01-01 00:00:00',"
                    " '2026-01-01 00:00:00', 0)"
                ),
                {"id": uuid.uuid4().hex, "uid": uuid.uuid4().hex, "h": f"hash{i}"},
            )

    monkeypatch.setattr(database_module, "engine", legacy_engine)
    database_module._upgrade_schema()
    database_module._upgrade_schema()  # second run must be a no-op

    with legacy_engine.begin() as conn:
        cols = {c["name"] for c in sa.inspect(legacy_engine).get_columns("refresh_tokens")}
        rows = conn.execute(
            sa.text("SELECT id, token, token_hash FROM refresh_tokens")
        ).fetchall()

    assert "token" in cols, "refresh_tokens.token must exist after the upgrade"
    assert len(rows) == 3, "no pre-existing refresh token may be dropped"
    assert all(r[1] for r in rows), "every legacy row needs a non-NULL token"
    assert len({r[1] for r in rows}) == 3, "backfilled tokens must be unique"
    assert {r[2] for r in rows} == {"hash0", "hash1", "hash2"}, "token_hash preserved"
    legacy_engine.dispose()


def test_authority_admin_login_persists_refresh_token() -> None:
    """The Authority Admin portal uses the same endpoint; it must get a usable
    access token and a persisted refresh token exactly like the Super Admin."""
    from app.models import Authority

    username = f"__sess_aa_{uuid.uuid4().hex[:8]}"
    password = "secret123"
    db = SessionLocal()
    try:
        authority = Authority(
            department_name="Examination Cell",
            authority_name=f"Test Authority {uuid.uuid4().hex[:6]}",
            email=f"aa_{uuid.uuid4().hex[:6]}@test.local",
            phone="0000000000",
        )
        db.add(authority)
        db.commit()
        db.refresh(authority)
        user = User(
            id=uuid.uuid4(),
            username=username,
            email=f"{username}@test.local",
            hashed_password=hash_password(password),
            role="authority_admin",
            is_active=True,
            authority_id=authority.id,
        )
        db.add(user)
        db.commit()
        db.refresh(user)
        _CREATED_USER_IDS.append(str(user.id))
        authority_id = str(authority.id)
    finally:
        db.close()

    r = _login(username, password)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["user"]["role"] == "authority_admin"
    assert body["user"]["authority_id"] == authority_id
    assert body["access_token"] and body["refresh_token"]

    db = SessionLocal()
    try:
        row = (
            db.query(RefreshToken)
            .filter(RefreshToken.token == body["refresh_token"])
            .first()
        )
        assert row is not None, "authority_admin login must persist its refresh token"
        assert str(row.user_id) == str(user.id)
        _CREATED_REFRESH_IDS.append(str(row.id))
    finally:
        db.close()

    rr = _CLIENT.post("/api/auth/refresh", json={"refresh_token": body["refresh_token"]})
    assert rr.status_code == 200, rr.text
    profile = _CLIENT.get(
        "/api/authority-admin/profile",
        headers={"Authorization": f"Bearer {rr.json()['access_token']}"},
    )
    assert profile.status_code == 200, profile.text
    assert profile.json()["username"] == username


def test_invalid_credentials_still_rejected() -> None:
    user, password = _create_admin()
    r = _CLIENT.post(
        "/api/auth/login",
        data={"username": user.username, "password": password + "-wrong"},
    )
    assert r.status_code == 401, r.text
    assert not r.json().get("access_token")
