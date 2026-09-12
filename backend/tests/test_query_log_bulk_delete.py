"""
backend/tests/test_query_log_bulk_delete.py

Focused tests for Admin -> AI Insights -> Query Log bulk delete.

Backend surface under test:

  POST /api/admin/analytics/logs/bulk-delete     (require_admin)
  Body: {"ids": ["id1", "id2", ...]}

Contract (existing, reused):
  - deletes ONLY the supplied InteractionEvent ids (transactional, one commit)
  - returns {"deleted": N, "failed": M} (never fabricates counts)
  - empty body / empty ids  -> 400
  - >500 ids                -> 400
  - malformed (non-UUID) id -> 400
  - unauthenticated 401 / student 403 / authority_admin 403 (server-enforced)

Checks include the mandatory duplicate-query-text independence case.

Run:  python tests/test_query_log_bulk_delete.py   (or pytest tests/test_query_log_bulk_delete.py)
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: F401  (register models before any session)

from fastapi.testclient import TestClient

from app.auth.security import hash_password
from app.database import SessionLocal, create_all
from app.main import app

PASS: list[str] = []
FAIL: list[str] = []

TOKENS: dict[str, dict[str, str]] = {"admin": {}, "student": {}, "authority": {}}
_created_user_ids: list[str] = []
_created_authority_ids: list[str] = []
_created_event_ids: list[str] = []
_PREFIX = f"__qlbulk_{uuid.uuid4().hex[:6]}"


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  PASS  {name}" + (f"  {detail}" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")


def _cleanup() -> None:
    from app.analytics.models import InteractionEvent
    from app.models import Authority, User

    db = SessionLocal()
    try:
        for eid in _created_event_ids:
            db.query(InteractionEvent).filter(InteractionEvent.id == eid).delete()
        for aid in _created_authority_ids:
            db.query(Authority).filter(Authority.id == aid).delete()
        for uid in _created_user_ids:
            db.query(User).filter(User.id == uid).delete()
        db.commit()
    finally:
        db.close()


def _ensure_users() -> None:
    from app.models import Authority, User

    db = SessionLocal()
    admin_username = student_username = auth_admin_username = ""
    try:
        admin = User(
            id=uuid.uuid4(),
            username=f"__qlb_admin_{uuid.uuid4().hex[:6]}",
            email=f"__qlb_admin_{uuid.uuid4().hex[:6]}@test.local",
            hashed_password=hash_password("secret123"),
            role="superadmin",
            is_active=True,
        )
        db.add(admin)
        db.flush()
        admin_username = admin.username
        _created_user_ids.append(str(admin.id))

        student = User(
            id=uuid.uuid4(),
            username=f"__qlb_student_{uuid.uuid4().hex[:6]}",
            email=f"__qlb_student_{uuid.uuid4().hex[:6]}@test.local",
            hashed_password=hash_password("secret123"),
            role="student",
            is_active=True,
        )
        db.add(student)
        db.flush()
        student_username = student.username
        _created_user_ids.append(str(student.id))

        authority = Authority(
            id=str(uuid.uuid4()),
            department_name="Bulk Delete Test Dept",
            authority_name=f"QLB Office {uuid.uuid4().hex[:6]}",
            designation="Head",
            email=f"qlb{uuid.uuid4().hex[:8]}@cus.ac.in",
            phone="0194-2311256",
            office_location="Gogji-Bagh, Srinagar",
            active=True,
            source_kind="manual",
        )
        db.add(authority)
        db.flush()
        authority_id = str(authority.id)
        _created_authority_ids.append(authority_id)

        auth_admin = User(
            id=uuid.uuid4(),
            username=f"__qlb_aa_{uuid.uuid4().hex[:6]}",
            email=f"__qlb_aa_{uuid.uuid4().hex[:6]}@test.local",
            hashed_password=hash_password("secret123"),
            role="authority_admin",
            is_active=True,
            authority_id=authority_id,
        )
        db.add(auth_admin)
        db.flush()
        auth_admin_username = auth_admin.username
        _created_user_ids.append(str(auth_admin.id))
        db.commit()
    finally:
        db.close()

    client = TestClient(app)
    r = client.post("/api/auth/login", data={"username": admin_username, "password": "secret123"})
    TOKENS["admin"]["Authorization"] = f"Bearer {r.json()['access_token']}"
    r = client.post("/api/auth/login", data={"username": student_username, "password": "secret123"})
    TOKENS["student"]["Authorization"] = f"Bearer {r.json()['access_token']}"
    r = client.post("/api/auth/login", data={"username": auth_admin_username, "password": "secret123"})
    TOKENS["authority"]["Authorization"] = f"Bearer {r.json()['access_token']}"


def _make_event(query_text, **kw):
    from app.analytics.models import InteractionEvent

    db = SessionLocal()
    try:
        ev = InteractionEvent(
            anon_session_id=f"qlb-session-{uuid.uuid4().hex[:6]}",
            conversation_id=f"qlb-conv-{uuid.uuid4().hex[:6]}",
            planner_action="answer",
            detected_intent="query",
            detected_programme=kw.pop("programme", "BSC-IT"),
            response_source=kw.pop("source", "rag"),
            conversation_completed=kw.pop("completed", True),
            response_time_ms=kw.pop("response_time_ms", 130),
            query_original=query_text,
            **kw,
        )
        db.add(ev)
        db.commit()
        db.refresh(ev)
        _created_event_ids.append(str(ev.id))
        return str(ev.id)
    finally:
        db.close()


def _event_rows(client, search):
    r = client.get(f"/api/admin/analytics/logs?period=month&page=1&page_size=50&search={search}", headers=TOKENS["admin"])
    if r.status_code != 200:
        return None
    return r.json().get("logs", [])


def _db_alive(eid) -> bool:
    from app.analytics.models import InteractionEvent

    db = SessionLocal()
    try:
        return db.query(InteractionEvent).filter(InteractionEvent.id == eid).first() is not None
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 1. Core bulk delete + duplicate-query independence (mandatory test)
# ---------------------------------------------------------------------------


def test_bulk_delete_exact_ids_duplicate_query():
    print("-- bulk delete: exact ids, duplicate query text ==")
    client = TestClient(app)
    shared = "How many semesters does BCA have?"
    e1 = _make_event(shared)
    e2 = _make_event(shared)
    e3 = _make_event(shared)
    e4 = _make_event(f"{_PREFIX} unrelated")

    rows = _event_rows(client, _PREFIX)
    check("all four fixtures visible", rows is not None and len(rows) == 4, f"n={len(rows) if rows else None}")
    check("list exposes unique ids per identical row",
          len({r["id"] for r in rows}) == 4 and {r["id"] for r in rows} == {e1, e2, e3, e4})

    r = client.post("/api/admin/analytics/logs/bulk-delete", json={"ids": [e1, e3]}, headers=TOKENS["admin"])
    check("bulk delete 200 (2 of 3 identical rows)", r.status_code == 200, f"status={r.status_code} {r.text[:140]}")
    body = r.json()
    check("reports 2 deleted, 0 failed", body.get("deleted") == 2 and body.get("failed") == 0, str(body))

    check("Event 1 deleted", not _db_alive(e1))
    check("Event 3 deleted", not _db_alive(e3))
    check("Event 2 remains (identical text, untouched)", _db_alive(e2))
    check("Event 4 remains (unrelated)", _db_alive(e4))

    rows_after = _event_rows(client, _PREFIX)
    ids_after = {r["id"] for r in rows_after} if rows_after else set()
    check("list now shows only the two survivors", ids_after == {e2, e4}, f"got {ids_after}")


# ---------------------------------------------------------------------------
# 2. One selected record / mixed existing-nonexisting // missing ids
# ---------------------------------------------------------------------------


def test_bulk_delete_single_and_mixed():
    print("-- bulk delete: single + mixed existing/nonexisting ==")
    client = TestClient(app)
    solo = _make_event(f"{_PREFIX} solo")
    twin = _make_event(f"{_PREFIX} solo")

    r = client.post("/api/admin/analytics/logs/bulk-delete", json={"ids": [solo]}, headers=TOKENS["admin"])
    check("single-record bulk delete 200", r.status_code == 200, f"status={r.status_code}")
    check("single delete reports exactly 1", r.json().get("deleted") == 1 and r.json().get("failed") == 0, str(r.json()))
    check("solo removed from DB", not _db_alive(solo))
    check("twin survives", _db_alive(twin))

    ghost = str(uuid.uuid4())
    r = client.post("/api/admin/analytics/logs/bulk-delete", json={"ids": [twin, ghost]}, headers=TOKENS["admin"])
    check("mixed existing/nonexisting 200", r.status_code == 200, f"status={r.status_code}")
    body = r.json()
    check("mixed reports 1 deleted, 1 failed (missing id counted)", body.get("deleted") == 1 and body.get("failed") == 1, str(body))
    check("twin removed, ghost never existed", not _db_alive(twin))

    r = client.post("/api/admin/analytics/logs/bulk-delete", json={"ids": [ghost]}, headers=TOKENS["admin"])
    check("already-missing ids report 0 deleted, 1 failed", r.status_code == 200 and r.json().get("deleted") == 0 and r.json().get("failed") == 1, str(r.json()))


# ---------------------------------------------------------------------------
# 3. Validation
# ---------------------------------------------------------------------------


def test_bulk_delete_validation():
    print("-- bulk delete: validation ==")
    client = TestClient(app)
    e = _make_event(f"{_PREFIX} validation")

    r = client.post("/api/admin/analytics/logs/bulk-delete", json={}, headers=TOKENS["admin"])
    check("missing ids -> 400", r.status_code == 400, f"status={r.status_code} {r.text[:120]}")
    r = client.post("/api/admin/analytics/logs/bulk-delete", json={"ids": []}, headers=TOKENS["admin"])
    check("empty ids array -> 400", r.status_code == 400, f"status={r.status_code} {r.text[:120]}")
    r = client.post("/api/admin/analytics/logs/bulk-delete", json={"ids": ["not-a-uuid"]}, headers=TOKENS["admin"])
    check("malformed uuid -> 400", r.status_code == 400, f"status={r.status_code} {r.text[:120]}")
    r = client.post("/api/admin/analytics/logs/bulk-delete", json={"ids": [e, "junk"]}, headers=TOKENS["admin"])
    check("mixed valid + malformed -> 400 (no partial delete)", r.status_code == 400, f"status={r.status_code} {r.text[:120]}")
    r = client.post("/api/admin/analytics/logs/bulk-delete", json={"ids": [str(uuid.uuid4()) for _ in range(501)]}, headers=TOKENS["admin"])
    check("501 ids -> 400 (max enforced)", r.status_code == 400, f"status={r.status_code} {r.text[:120]}")
    r = client.post("/api/admin/analytics/logs/bulk-delete", json={"ids": "nope"}, headers=TOKENS["admin"])
    check("ids not a list -> 422/400 rejected", r.status_code in (400, 422), f"status={r.status_code}")
    check("validation failures never delete anything", _db_alive(e))


# ---------------------------------------------------------------------------
# 4. Security matrix
# ---------------------------------------------------------------------------


def test_bulk_delete_security():
    print("-- bulk delete: security matrix ==")
    client = TestClient(app)
    victim = _make_event(f"{_PREFIX} secure")

    r = client.post("/api/admin/analytics/logs/bulk-delete", json={"ids": [victim]})
    check("unauthenticated -> 401", r.status_code == 401, f"status={r.status_code}")
    r = client.post("/api/admin/analytics/logs/bulk-delete", json={"ids": [victim]}, headers=TOKENS["student"])
    check("student -> 403", r.status_code == 403, f"status={r.status_code}")
    r = client.post("/api/admin/analytics/logs/bulk-delete", json={"ids": [victim]}, headers=TOKENS["authority"])
    check("authority_admin -> 403", r.status_code == 403, f"status={r.status_code}")
    check("row untouched by rejected calls", _db_alive(victim))

    r = client.get("/api/admin/analytics/logs?period=month", headers=TOKENS["student"])
    check("student cannot read logs (403)", r.status_code == 403, f"status={r.status_code}")


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

import pytest  # noqa: E402


@pytest.fixture(scope="module", autouse=True)
def _prepare():
    create_all()
    _ensure_users()
    yield
    _cleanup()


if __name__ == "__main__":
    create_all()
    _ensure_users()
    tests = [
        test_bulk_delete_exact_ids_duplicate_query,
        test_bulk_delete_single_and_mixed,
        test_bulk_delete_validation,
        test_bulk_delete_security,
    ]
    try:
        for fn in tests:
            try:
                print("-- " + fn.__name__ + " --")
                fn()
            except Exception as exc:  # noqa: BLE001
                FAIL.append(fn.__name__)
                print(f"  ERROR  {fn.__name__}: {exc}")
    finally:
        _cleanup()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)