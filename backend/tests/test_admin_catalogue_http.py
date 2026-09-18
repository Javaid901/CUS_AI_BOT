"""
backend/tests/test_admin_catalogue_http.py

HTTP-level regression tests for the admin academic-catalogue API (live, real
routes via FastAPI TestClient against the isolated throwaway test database).

Covers the defects fixed in the working tree:

  1. The new-programme-create flow (POST) MUST return a usable id (regression
     for the old frontend bug that sent new-programme saves to
     PUT /programmes/undefined, which raised inside the service layer).
  2. The update flow MUST use the real id returned by POST (PUT round-trip).
  3. A malformed programme id in the uri ("undefined", "not-a-uuid") MUST
     return 404 ("Programme not found") — exactly like an absent-but-valid
     UUID — instead of leaking an unhandled ``ValueError`` as HTTP 500.
  4. Duplicate create -> 409 (IntegrityError mapping) at the HTTP boundary.

Run:  python -m pytest tests/test_admin_catalogue_http.py -q
"""

from __future__ import annotations

import uuid

import pytest
from fastapi.testclient import TestClient

from app.auth.security import hash_password
from app.database import SessionLocal, create_all
from app.main import app
from app.models import User

create_all()

# Programmes this module creates, removed at module teardown so the shared
# throwaway database returns to its baseline for count-based suites.
_CREATED: list[str] = []


@pytest.fixture(scope="module")
def client():
    c = TestClient(app)
    yield c
    c.close()


@pytest.fixture(scope="module")
def admin_headers(client):
    db = SessionLocal()
    try:
        admin = User(
            id=uuid.uuid4(),
            username=f"__httpcat_{uuid.uuid4().hex[:8]}",
            email=f"__httpcat_{uuid.uuid4().hex[:8]}@test.local",
            hashed_password=hash_password("secret123"),
            role="superadmin",
            is_active=True,
        )
        db.add(admin)
        db.commit()
        username = admin.username
    finally:
        db.close()

    login = client.post(
        "/api/auth/login",
        data={"username": username, "password": "secret123"},
    )
    assert login.status_code == 200, login.text
    return {"Authorization": f"Bearer {login.json()['access_token']}"}


@pytest.fixture(scope="module", autouse=True)
def _cleanup_created(admin_headers, client):
    yield
    for pid in _CREATED:
        try:
            client.delete(f"/api/admin/catalogue/programmes/{pid}", headers=admin_headers)
        except Exception:  # noqa: BLE001
            pass


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def test_create_new_programme_returns_usable_id(client, admin_headers):
    r = client.post(
        "/api/admin/catalogue/programmes",
        json={
            "name": _uniq("PG HTTP Create"),
            "code": _uniq("httpc"),
            "degree_level": "Postgraduate",
        },
        headers=admin_headers,
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("id"), f"no id returned: {body}"
    uuid.UUID(str(body["id"]))  # must be a real uuid the UI can PUT with
    _CREATED.append(str(body["id"]))


def test_update_roundtrip_with_returned_uuid(client, admin_headers):
    created = client.post(
        "/api/admin/catalogue/programmes",
        json={
            "name": _uniq("PG HTTP Update"),
            "code": _uniq("httpu"),
            "degree_level": "Postgraduate",
        },
        headers=admin_headers,
    )
    assert created.status_code == 200, created.text
    pid = created.json()["id"]
    _CREATED.append(str(pid))

    got = client.get(f"/api/admin/catalogue/programmes/{pid}", headers=admin_headers)
    assert got.status_code == 200, got.text

    updated_name = _uniq("PG HTTP Updated")
    upd = client.put(
        f"/api/admin/catalogue/programmes/{pid}",
        json={"name": updated_name},
        headers=admin_headers,
    )
    assert upd.status_code == 200, upd.text

    after = client.get(f"/api/admin/catalogue/programmes/{pid}", headers=admin_headers)
    assert after.status_code == 200 and after.json()["name"] == updated_name


def test_malformed_programme_uuid_is_404_not_500(client, admin_headers):
    for bad in ("undefined", "not-a-uuid"):
        for method in ("PUT", "DELETE"):
            status = _status(client, admin_headers, method, bad)
            assert status == 404, f"{method} /programmes/{bad!r} -> {status}, want 404"


def test_absent_but_valid_uuid_is_404(client, admin_headers):
    ghost = str(uuid.uuid4())
    for method in ("GET", "PUT", "DELETE"):
        status = _status(client, admin_headers, method, ghost)
        assert status == 404, f"{method} /programmes/{ghost} -> {status}, want 404"


def test_duplicate_programme_create_returns_409(client, admin_headers):
    payload = {
        "name": _uniq("PG HTTP Duplicate"),
        "code": _uniq("httpd"),
        "degree_level": "Postgraduate",
    }
    first = client.post(
        "/api/admin/catalogue/programmes", json=payload, headers=admin_headers
    )
    assert first.status_code == 200, first.text
    _CREATED.append(str(first.json()["id"]))
    dup = client.post(
        "/api/admin/catalogue/programmes", json=payload, headers=admin_headers
    )
    assert dup.status_code == 409, dup.text


# NOTE: over-length fields (DataError -> 422) cannot be exercised here — SQLite
# does not enforce String(150); that path is PostgreSQL-only (verified live).


def _status(client, headers, method, pid) -> int:
    url = f"/api/admin/catalogue/programmes/{pid}"
    if method.upper() == "GET":
        resp = client.get(url, headers=headers)
    elif method.upper() == "PUT":
        resp = client.put(url, json={}, headers=headers)
    else:
        resp = client.delete(url, headers=headers)
    return resp.status_code