"""
backend/tests/test_knowledge_gap_resolve.py

Focused tests for Admin -> AI Insights -> Gaps -> Resolve workflow.

Backend surface under test:

  GET  /api/admin/analytics/knowledge-gaps?limit=N[&include_resolved=true]  (require_admin)
  POST /api/admin/analytics/knowledge-gaps/{gap_id}/resolve                  (require_admin)

New Resolve contract:
  - REQUIRES {"resolution_text": "..."} (trimmed, >= 20 chars)
  - stores the admin-provided verified resolution (never auto-generated)
  - records resolved_by using the established actor-label pattern
  - sets resolved=True and resolved_at via datetime.now(timezone.utc)
    (SQLite stores datetimes without tzinfo, so reads are naive)
  - idempotent on repeat with identical text; 409 on conflicting re-resolve
  - unresolved-gap query semantics unchanged; include_resolved still works

Run:  python tests/test_knowledge_gap_resolve.py   (or pytest tests/test_knowledge_gap_resolve.py)
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
_created_gap_ids: list[str] = []
_created_event_ids: list[str] = []
_MARKER = f"__qlgap_{uuid.uuid4().hex[:6]}"
_RESOLUTION = "The official MCA 3rd semester examination fee is Rs 20,000 as per the Finance Office notification dated 12/09/2026."


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  PASS  {name}" + (f"  {detail}" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")


def _cleanup() -> None:
    from app.analytics.models import InteractionEvent, KnowledgeGap
    from app.models import Authority, User

    db = SessionLocal()
    try:
        for eid in _created_event_ids:
            db.query(InteractionEvent).filter(InteractionEvent.id == eid).delete()
        for gid in _created_gap_ids:
            db.query(KnowledgeGap).filter(KnowledgeGap.id == gid).delete()
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
            username=f"__qg_admin_{uuid.uuid4().hex[:6]}",
            email=f"__qg_admin_{uuid.uuid4().hex[:6]}@test.local",
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
            username=f"__qg_student_{uuid.uuid4().hex[:6]}",
            email=f"__qg_student_{uuid.uuid4().hex[:6]}@test.local",
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
            department_name="Gap Resolve Test Dept",
            authority_name=f"QGR Office {uuid.uuid4().hex[:6]}",
            designation="Head",
            email=f"qgr{uuid.uuid4().hex[:8]}@cus.ac.in",
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
            username=f"__qg_aa_{uuid.uuid4().hex[:6]}",
            email=f"__qg_aa_{uuid.uuid4().hex[:6]}@test.local",
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
    TOKENS["admin"]["_user"] = admin_username
    r = client.post("/api/auth/login", data={"username": student_username, "password": "secret123"})
    TOKENS["student"]["Authorization"] = f"Bearer {r.json()['access_token']}"
    r = client.post("/api/auth/login", data={"username": auth_admin_username, "password": "secret123"})
    TOKENS["authority"]["Authorization"] = f"Bearer {r.json()['access_token']}"


def _make_gap(**kw) -> str:
    from app.analytics.models import KnowledgeGap

    db = SessionLocal()
    try:
        g = KnowledgeGap(
            gap_type=kw.pop("gap_type", "unanswered_question"),
            query_text=kw.pop("query_text", f"{_MARKER} what is the fee for MCA 3rd semester?"),
            confidence_score=kw.pop("confidence_score", 0.32),
            frequency=kw.pop("frequency", 4),
            suggestion=kw.pop("suggestion", "Add the verified MCA fee entry from the Finance Office notification."),
            resolved=kw.pop("resolved", False),
            **kw,
        )
        db.add(g)
        db.commit()
        db.refresh(g)
        _created_gap_ids.append(str(g.id))
        return str(g.id)
    finally:
        db.close()


def _make_event() -> str:
    from app.analytics.models import InteractionEvent

    db = SessionLocal()
    try:
        ev = InteractionEvent(
            anon_session_id=f"qg-session-{uuid.uuid4().hex[:6]}",
            conversation_id=f"qg-conv-{uuid.uuid4().hex[:6]}",
            planner_action="answer",
            detected_intent="query",
            detected_programme="MCA",
            response_source="rag",
            conversation_completed=True,
            response_time_ms=130,
            query_original=f"{_MARKER} event {uuid.uuid4().hex[:6]}",
        )
        db.add(ev)
        db.commit()
        db.refresh(ev)
        _created_event_ids.append(str(ev.id))
        return str(ev.id)
    finally:
        db.close()


def _gap_row(gid: str) -> dict | None:
    from app.analytics.models import KnowledgeGap

    db = SessionLocal()
    try:
        g = db.query(KnowledgeGap).filter(KnowledgeGap.id == gid).first()
        if not g:
            return None
        return {
            "resolved": bool(g.resolved),
            "resolved_at": g.resolved_at,
            "resolution_text": g.resolution_text,
            "resolved_by": g.resolved_by,
        }
    finally:
        db.close()


def _list_gaps(client, **params):
    q = "&".join(f"{k}={v}" for k, v in params.items())
    return client.get(f"/api/admin/analytics/knowledge-gaps?{q}", headers=TOKENS["admin"])


# ---------------------------------------------------------------------------
# A. Happy path: resolve with a valid verified resolution
# ---------------------------------------------------------------------------


def test_resolve_happy_path():
    print("-- resolve: valid admin-provided resolution ==")
    client = TestClient(app)
    gid = _make_gap()

    r = client.post(f"/api/admin/analytics/knowledge-gaps/{gid}/resolve",
                    json={"resolution_text": _RESOLUTION}, headers=TOKENS["admin"])
    check("resolve 200", r.status_code == 200, f"status={r.status_code} {r.text[:140]}")
    check("response status resolved", r.json().get("status") == "resolved", str(r.json()))

    row = _gap_row(gid)
    check("resolved == True in DB", row is not None and row["resolved"] is True, str(row))
    check("resolved_at populated",
          row["resolved_at"] is not None,
          str(row["resolved_at"]))
    check("resolution_text stores EXACT admin text",
          row["resolution_text"] == _RESOLUTION, str(row["resolution_text"]))
    check("resolved_by records acting admin (username)",
          row["resolved_by"] == TOKENS["admin"].get("_user", "admin-fallback"), str(row["resolved_by"]))


# ---------------------------------------------------------------------------
# B/C. Missing / blank resolution
# ---------------------------------------------------------------------------


def test_resolve_missing_blank_rejected():
    print("-- resolve: missing & blank text rejected ==")
    client = TestClient(app)
    gid = _make_gap()

    r = client.post(f"/api/admin/analytics/knowledge-gaps/{gid}/resolve", json={}, headers=TOKENS["admin"])
    check("missing resolution_text -> 400", r.status_code == 400, f"status={r.status_code} {r.text[:120]}")

    r = client.post(f"/api/admin/analytics/knowledge-gaps/{gid}/resolve",
                    json={"resolution_text": ""}, headers=TOKENS["admin"])
    check("empty resolution_text -> 400", r.status_code == 400, f"status={r.status_code} {r.text[:120]}")

    r = client.post(f"/api/admin/analytics/knowledge-gaps/{gid}/resolve",
                    json={"resolution_text": "   \t  \n  "}, headers=TOKENS["admin"])
    check("whitespace-only -> 400", r.status_code == 400, f"status={r.status_code} {r.text[:120]}")

    r = client.post(f"/api/admin/analytics/knowledge-gaps/{gid}/resolve",
                    json={"resolution_text": "ok"}, headers=TOKENS["admin"])
    check("junk short text -> 400 (min length)", r.status_code == 400, f"status={r.status_code} {r.text[:120]}")

    row = _gap_row(gid)
    check("gap still unresolved", row is not None and row["resolved"] is False, str(row))
    check("no resolution recorded", row["resolution_text"] is None, str(row))
    check("no resolve timestamp", row["resolved_at"] is None, str(row))


# ---------------------------------------------------------------------------
# D/E. Invalid / nonexistent gap
# ---------------------------------------------------------------------------


def test_resolve_invalid_and_missing_gap():
    print("-- resolve: invalid id and missing gap ==")
    client = TestClient(app)

    r = client.post("/api/admin/analytics/knowledge-gaps/not-a-uuid/resolve",
                    json={"resolution_text": _RESOLUTION}, headers=TOKENS["admin"])
    check("invalid gap id -> 400", r.status_code == 400, f"status={r.status_code} {r.text[:120]}")

    r = client.post(f"/api/admin/analytics/knowledge-gaps/{uuid.uuid4()}/resolve",
                    json={"resolution_text": _RESOLUTION}, headers=TOKENS["admin"])
    check("nonexistent gap -> 404", r.status_code == 404, f"status={r.status_code} {r.text[:120]}")


# ---------------------------------------------------------------------------
# F. Authorization
# ---------------------------------------------------------------------------


def test_resolve_security():
    print("-- resolve: authorization matrix ==")
    client = TestClient(app)
    victim = _make_gap()

    r = client.post(f"/api/admin/analytics/knowledge-gaps/{victim}/resolve",
                    json={"resolution_text": _RESOLUTION})
    check("unauthenticated -> 401", r.status_code == 401, f"status={r.status_code}")

    r = client.post(f"/api/admin/analytics/knowledge-gaps/{victim}/resolve",
                    json={"resolution_text": _RESOLUTION}, headers=TOKENS["student"])
    check("student -> 403", r.status_code == 403, f"status={r.status_code}")

    r = client.post(f"/api/admin/analytics/knowledge-gaps/{victim}/resolve",
                    json={"resolution_text": _RESOLUTION}, headers=TOKENS["authority"])
    check("authority_admin -> 403", r.status_code == 403, f"status={r.status_code}")

    row = _gap_row(victim)
    check("gap untouched by unauthorized calls", row is not None and row["resolved"] is False, str(row))


# ---------------------------------------------------------------------------
# Safety: idempotent repeat / conflict-protected overwrite
# ---------------------------------------------------------------------------


def test_resolve_already_resolved_safety():
    print("-- resolve: already-resolved safety ==")
    client = TestClient(app)
    gid = _make_gap()
    t1 = _RESOLUTION
    t2 = "A completely different verified resolution text that also exists here for testing purposes."

    r = client.post(f"/api/admin/analytics/knowledge-gaps/{gid}/resolve",
                    json={"resolution_text": t1}, headers=TOKENS["admin"])
    check("first resolve 200", r.status_code == 200, f"status={r.status_code}")

    r = client.post(f"/api/admin/analytics/knowledge-gaps/{gid}/resolve",
                    json={"resolution_text": t1}, headers=TOKENS["admin"])
    body = r.json()
    check("repeat with identical text idempotent 200", r.status_code == 200, f"status={r.status_code} {r.text[:140]}")
    check("repeat flagged already_resolved", body.get("already_resolved") is True, str(body))

    r = client.post(f"/api/admin/analytics/knowledge-gaps/{gid}/resolve",
                    json={"resolution_text": t2}, headers=TOKENS["admin"])
    check("conflicting re-resolve -> 409", r.status_code == 409, f"status={r.status_code} {r.text[:120]}")

    row = _gap_row(gid)
    check("original resolution preserved", row["resolution_text"] == t1, str(row["resolution_text"]))


# ---------------------------------------------------------------------------
# G. Regression: list semantics + include_resolved
# ---------------------------------------------------------------------------


def test_regression_list_semantics():
    print("-- regression: default list excludes resolved, include_resolved exposes ==")
    client = TestClient(app)
    unresolved = _make_gap(query_text=f"{_MARKER} unresolved pending")
    resolved = _make_gap(query_text=f"{_MARKER} already handled")
    r = client.post(f"/api/admin/analytics/knowledge-gaps/{resolved}/resolve",
                    json={"resolution_text": _RESOLUTION}, headers=TOKENS["admin"])
    check("seed resolve 200", r.status_code == 200, f"status={r.status_code}")

    r = _list_gaps(client, limit="200")
    check("default list 200", r.status_code == 200, f"status={r.status_code}")
    ids = [g["id"] for g in r.json()]
    check("default list contains unresolved gap", unresolved in ids)
    check("default list EXCLUDES resolved gap", resolved not in ids, f"ids={ids}")

    r = _list_gaps(client, limit="200", include_resolved="true")
    ids = [g["id"] for g in r.json()]
    check("include_resolved list contains both", resolved in ids and unresolved in ids, f"ids={ids}")

    resolved_json = next((g for g in r.json() if g["id"] == resolved), None)
    check("include_resolved exposes resolution_text", resolved_json and resolved_json.get("resolution_text") == _RESOLUTION, str(resolved_json))
    check("include_resolved exposes resolved_by", resolved_json and bool(resolved_json.get("resolved_by")), str(resolved_json))


# ---------------------------------------------------------------------------
# J. Data integrity: interactions + other gaps untouched
# ---------------------------------------------------------------------------


def test_data_integrity():
    print("-- data integrity: events and unrelated gaps untouched ==")
    client = TestClient(app)
    ev = _make_event()
    other = _make_gap(query_text=f"{_MARKER} unrelated gap that must survive")
    target = _make_gap()

    r = client.post(f"/api/admin/analytics/knowledge-gaps/{target}/resolve",
                    json={"resolution_text": _RESOLUTION}, headers=TOKENS["admin"])
    check("resolve 200", r.status_code == 200, f"status={r.status_code}")

    from app.analytics.models import InteractionEvent

    db = SessionLocal()
    try:
        ev_row = db.query(InteractionEvent).filter(InteractionEvent.id == ev).first()
    finally:
        db.close()
    check("InteractionEvent row still exists and untouched", ev_row is not None, str(ev))
    check("InteractionEvent query_original unchanged", ev_row.query_original is not None and ev_row.query_original.startswith(_MARKER), str(ev_row.query_original))

    other_row = _gap_row(other)
    check("unrelated gap still unresolved", other_row is not None and other_row["resolved"] is False, str(other_row))
    check("unrelated gap has no resolution", other_row["resolution_text"] is None, str(other_row))


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
        test_resolve_happy_path,
        test_resolve_missing_blank_rejected,
        test_resolve_invalid_and_missing_gap,
        test_resolve_security,
        test_resolve_already_resolved_safety,
        test_regression_list_semantics,
        test_data_integrity,
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