"""
backend/tests/test_query_log_delete.py

Focused tests for the Admin -> AI Insights -> Query Log delete (trash icon) fix.

The frontend bug: fetchJSON(url) dropped the options object, so the trash-icon
DELETE was silently issued as GET /logs/{id} (the detail route) - the row was
never deleted. This suite verifies the backend delete surface the UI now hits:

  DELETE /api/admin/analytics/logs/{log_id}   (require_admin)

Checks:
  - admin delete succeeds and persists in DB + logs list + detail
  - exact-event-only / identical-query independence (same text, separate rows)
  - filtered-list + pagination consistency after a delete
  - repeated delete of the same id -> 404
  - nonexistent id / malformed id -> 404
  - security: unauthenticated 401, student 403, authority_admin 403

Run:  python tests/test_query_log_delete.py   (or pytest tests/test_query_log_delete.py)
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
_PREFIX = f"__qldelete_{uuid.uuid4().hex[:6]}"


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
            username=f"__ql_admin_{uuid.uuid4().hex[:6]}",
            email=f"__ql_admin_{uuid.uuid4().hex[:6]}@test.local",
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
            username=f"__ql_student_{uuid.uuid4().hex[:6]}",
            email=f"__ql_student_{uuid.uuid4().hex[:6]}@test.local",
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
            department_name="Query Log Test Dept",
            authority_name=f"QL Office {uuid.uuid4().hex[:6]}",
            designation="Head",
            email=f"ql{uuid.uuid4().hex[:8]}@cus.ac.in",
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
            username=f"__ql_aa_{uuid.uuid4().hex[:6]}",
            email=f"__ql_aa_{uuid.uuid4().hex[:6]}@test.local",
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


def _make_event(query_text, source="rag", **kw):
    from app.analytics.models import InteractionEvent

    db = SessionLocal()
    try:
        ev = InteractionEvent(
            anon_session_id=f"ql-session-{uuid.uuid4().hex[:6]}",
            conversation_id=f"ql-conv-{uuid.uuid4().hex[:6]}",
            planner_action="answer",
            detected_intent="query",
            detected_programme=kw.pop("programme", "BSC-IT"),
            response_source=source,
            conversation_completed=kw.pop("completed", True),
            response_time_ms=kw.pop("response_time_ms", 120),
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


def _log_ids(client, search):
    r = client.get(f"/api/admin/analytics/logs?period=month&page=1&page_size=50&search={search}", headers=TOKENS["admin"])
    if r.status_code != 200:
        return r.status_code, None
    body = r.json()
    return body.get("total", 0), body.get("logs", [])


# ---------------------------------------------------------------------------
# 1. Single delete via the backend route the (fixed) UI now calls
# ---------------------------------------------------------------------------


def test_single_delete_success():
    print("-- delete: success + persistence ==")
    client = TestClient(app)
    e_alpha = _make_event(f"{_PREFIX} alpha")
    r = client.delete(f"/api/admin/analytics/logs/{e_alpha}", headers=TOKENS["admin"])
    check("admin DELETE succeeds (200)", r.status_code == 200, f"status={r.status_code} {r.text[:120]}")
    check("response body is status=deleted", r.status_code == 200 and r.json().get("status") == "deleted", r.text[:120])

    from app.analytics.models import InteractionEvent

    db = SessionLocal()
    try:
        still = db.query(InteractionEvent).filter(InteractionEvent.id == e_alpha).first()
        check("row removed from DB", still is None)
    finally:
        db.close()

    r = client.get(f"/api/admin/analytics/logs/{e_alpha}", headers=TOKENS["admin"])
    check("detail route now 404 for deleted id", r.status_code == 404, f"status={r.status_code}")


def test_invalid_and_missing_ids():
    print("-- delete: missing / malformed id ==")
    client = TestClient(app)
    r = client.delete(f"/api/admin/analytics/logs/{uuid.uuid4()}", headers=TOKENS["admin"])
    check("nonexistent uuid -> 404", r.status_code == 404, f"status={r.status_code}")
    r = client.delete("/api/admin/analytics/logs/not-a-uuid", headers=TOKENS["admin"])
    check("malformed id -> 404 (not 500)", r.status_code == 404, f"status={r.status_code}")


def test_repeated_delete():
    print("-- delete: repeat delete same id ==")
    client = TestClient(app)
    e_dup = _make_event(f"{_PREFIX} repeat")
    first = client.delete(f"/api/admin/analytics/logs/{e_dup}", headers=TOKENS["admin"])
    second = client.delete(f"/api/admin/analytics/logs/{e_dup}", headers=TOKENS["admin"])
    check("first delete 200", first.status_code == 200, f"status={first.status_code}")
    check("second delete 404 (already gone)", second.status_code == 404, f"status={second.status_code}")


# ---------------------------------------------------------------------------
# 2. Precision: exact event only, identical queries are independent
# ---------------------------------------------------------------------------


def test_exact_event_only_and_identical_query_independence():
    print("-- delete: targets only the exact event ==")
    client = TestClient(app)
    shared = f"{_PREFIX} same question"
    e1 = _make_event(shared)
    e2 = _make_event(shared)
    # catalogue-flavoured sibling that must survive
    sibling = _make_event(f"{_PREFIX} other", source="structured", completed=False)

    total_before, logs_before = _log_ids(client, _PREFIX)
    check("list exposes id for every row (frontend targets real ids)",
          all("id" in (lg or {}) for lg in logs_before) and len(logs_before) >= 3,
          f"logs={len(logs_before)}")
    visible = {lg["id"] for lg in logs_before}
    check("fixture ids present in logs list", {e1, e2, sibling}.issubset(visible), f"missing={visible - {e1, e2, sibling}}")

    r = client.delete(f"/api/admin/analytics/logs/{e1}", headers=TOKENS["admin"])
    check("delete of first identical-query event 200", r.status_code == 200, f"status={r.status_code}")

    from app.analytics.models import InteractionEvent

    db = SessionLocal()
    try:
        e1_alive = db.query(InteractionEvent).filter(InteractionEvent.id == e1).first()
        e2_alive = db.query(InteractionEvent).filter(InteractionEvent.id == e2).first()
        sibling_alive = db.query(InteractionEvent).filter(InteractionEvent.id == sibling).first()
        check("deleted event gone from DB", e1_alive is None)
        check("identical-query twin survives (independent row)", e2_alive is not None)
        check("sibling survives", sibling_alive is not None)
    finally:
        db.close()

    total_after, logs_after = _log_ids(client, _PREFIX)
    check("log list total drops by exactly 1", total_after == total_before - 1, f"before={total_before} after={total_after}")
    after_texts = {lg["query_text"] for lg in logs_after}
    check("deleted variant still visible via its twin in logs",
          f"{shared}" in after_texts and f"{_PREFIX} other" in after_texts)
    check("deleted row absent while twin present",
          not any(lg["id"] == e1 for lg in logs_after) and any(lg["id"] == e2 for lg in logs_after))


def test_filtered_and_paginated_consistency():
    print("-- delete: filtered + pagination totals stay consistent ==")
    client = TestClient(app)
    keep = _make_event(f"{_PREFIX} filterme", response_time_ms=500)
    victim = _make_event(f"{_PREFIX} filterme", response_time_ms=700)

    search_q = f"{_PREFIX} filterme"
    param_q = client.get(f"/api/admin/analytics/logs?period=month&page=1&page_size=50&search={search_q}", headers=TOKENS["admin"])
    before = param_q.json()
    check("filtered list returns all matching rows", before["total"] == 2, f"total={before['total']}")
    check("pagination page_size honoured", before["page_size"] == 50 and before["page"] == 1)

    r = client.delete(f"/api/admin/analytics/logs/{victim}", headers=TOKENS["admin"])
    check("filtered delete 200", r.status_code == 200, f"status={r.status_code}")

    after = client.get(f"/api/admin/analytics/logs?period=month&page=1&page_size=50&search={search_q}", headers=TOKENS["admin"]).json()
    check("filtered total decremented", after["total"] == 1, f"total={after['total']}")
    check("surviving row still returned", after["logs"] and after["logs"][0]["id"] == keep)
    check("page_size mirror unchanged", before["page_size"] == after["page_size"])
    check("surviving row still addressable by detail GET",
          client.get(f"/api/admin/analytics/logs/{keep}", headers=TOKENS["admin"]).status_code == 200)


# ---------------------------------------------------------------------------
# 3. Security matrix
# ---------------------------------------------------------------------------


def test_security_matrix():
    print("-- delete: security matrix ==")
    client = TestClient(app)
    victim = _make_event(f"{_PREFIX} secure")

    r = client.delete(f"/api/admin/analytics/logs/{victim}")
    check("unauthenticated -> 401", r.status_code == 401, f"status={r.status_code}")
    r = client.delete(f"/api/admin/analytics/logs/{victim}", headers=TOKENS["student"])
    check("student -> 403", r.status_code == 403, f"status={r.status_code}")
    r = client.delete(f"/api/admin/analytics/logs/{victim}", headers=TOKENS["authority"])
    check("authority_admin -> 403", r.status_code == 403, f"status={r.status_code}")

    from app.analytics.models import InteractionEvent

    db = SessionLocal()
    try:
        row = db.query(InteractionEvent).filter(InteractionEvent.id == victim).first()
        check("row untouched by rejected requests", row is not None)
    finally:
        db.close()

    r = client.get("/api/admin/analytics/logs?period=month", headers=TOKENS["student"])
    check("student cannot read logs (403)", r.status_code == 403, f"status={r.status_code}")
    r = client.get("/api/admin/analytics/logs?period=month", headers=TOKENS["authority"])
    check("authority_admin cannot read logs (403)", r.status_code == 403, f"status={r.status_code}")
    r = client.get("/api/admin/analytics/logs?period=month")
    check("unauthenticated cannot read logs (401)", r.status_code == 401, f"status={r.status_code}")


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
        test_single_delete_success,
        test_invalid_and_missing_ids,
        test_repeated_delete,
        test_exact_event_only_and_identical_query_independence,
        test_filtered_and_paginated_consistency,
        test_security_matrix,
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