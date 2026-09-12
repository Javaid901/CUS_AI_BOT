"""
backend/tests/test_grievance_eligible_authorities.py

Grievance authority eligibility rule — an authority is a valid grievance
destination ONLY when it is ACTIVE and at least one ACTIVE `authority_admin`
account is assigned to it (users.authority_id → authorities.id).

Covers all four student surfaces sharing the single definition:
  1. Picker   : GET /api/authority/active          → eligible authorities only
  2. Recommend: POST /api/grievances/recommend     → eligible authorities only
  3. Auto-match: POST /api/authority/match          → never an unstaffed office
  4. Submit   : POST /api/grievances                → 422 for unstaffed offices

Plus the boundary rules:
  * an INACTIVE account does NOT back an authority
  * an INACTIVE authority is never a destination even if it has an admin
  * deactivating the last administrator removes the authority immediately
  * plain contact lookup (GET /api/authority/{id}) stays unrestricted

Runs against the app's real DB (TestClient), cleaning up every created row.

Run:  python tests/test_grievance_eligible_authorities.py   (or pytest tests/)
"""

from __future__ import annotations

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.models  # noqa: F401

from fastapi.testclient import TestClient

from app.auth.security import hash_password
from app.authority.service import authority_service
from app.database import SessionLocal, create_all
from app.grievance.models import Grievance
from app.main import app
from app.models import Authority, User

create_all()

PASS: list[str] = []
FAIL: list[str] = []

_created_authority_ids: list[str] = []
_created_refs: list[str] = []
client = TestClient(app)


def check(name: str, cond: bool, detail: str = ""):
    line = f"  {'PASS' if cond else 'FAIL'}  {name}" + (f"  {detail}" if detail else "")
    try:
        print(line)
    except UnicodeEncodeError:
        print(line.encode("ascii", "replace").decode("ascii"))
    if cond:
        PASS.append(name)
    else:
        FAIL.append(name)


def _fake_ip(suffix: int) -> dict[str, str]:
    return {"X-Forwarded-For": f"203.0.113.{suffix}"}


def _cleanup() -> None:
    db = SessionLocal()
    try:
        db.query(User).filter(
            User.email.like("eli-adm-%@test.local")
        ).delete(synchronize_session=False)
        for ref in _created_refs:
            db.query(Grievance).filter(Grievance.reference == ref).delete(synchronize_session=False)
        for aid in _created_authority_ids:
            db.query(Authority).filter(Authority.id == aid).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
        authority_service.refresh_cache(db)


def _purge_leftovers() -> None:
    db = SessionLocal()
    try:
        db.query(User).filter(
            User.email.like("eli-adm-%@test.local")
        ).delete(synchronize_session=False)
        db.query(Authority).filter(
            Authority.email.like("eli-%@test.local")
        ).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()
        authority_service.refresh_cache(db)


_purge_leftovers()

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _auto_cleanup():  # noqa: ANN001
    yield
    _cleanup()


def _make_authority(name: str, active: bool = True) -> Authority:
    db = SessionLocal()
    try:
        a = Authority(
            id=str(uuid.uuid4()),
            department_name=f"Dept {name} {uuid.uuid4().hex[:4]}",
            authority_name=name,
            designation="Head",
            email=f"eli-{name.lower().replace(' ', '-')[:20]}-{uuid.uuid4().hex[:6]}@test.local",
            phone="0194-2311256",
            keywords='["violet umbrella", "lonely fern", "results"]',
            services_offered='["grievance intake"]',
            active=active,
        )
        db.add(a)
        db.commit()
        db.refresh(a)
        _created_authority_ids.append(str(a.id))
        authority_service.refresh_cache(db)
        return a
    finally:
        db.close()


def _make_admin(authority_id: str, *, active: bool = True) -> str:
    db = SessionLocal()
    try:
        u = User(
            id=uuid.uuid4(),
            username=f"__eli_adm_{uuid.uuid4().hex[:10]}",
            email=f"eli-adm-{uuid.uuid4().hex[:8]}@test.local",
            hashed_password=hash_password("pass1234"),
            role="authority_admin",
            is_active=active,
            authority_id=authority_id,
            full_name="Eligibility Admin",
            designation="Office Head",
        )
        db.add(u)
        db.commit()
        return str(u.id)
    finally:
        db.close()


def _submit_payload(authority_id: str) -> dict:
    return {
        "student": {
            "name": "Elig Interview",
            "email": "eli.student@example.com",
            "roll_number": "ELI001",
            "semester": "4",
            "college": "Amar Singh College",
        },
        "original_input": "my results are wrong and nobody is helping",
        "final_text": "My results are wrong and nobody in the office is helping resolve this matter.",
        "category": "Examination & Results",
        "authority_id": authority_id,
        "idempotency_key": "eli-" + uuid.uuid4().hex[:24],
    }


# ---------------------------------------------------------------------------
# 1. Picker (GET /api/authority/active)
# ---------------------------------------------------------------------------


def test_picker_serves_only_admin_backed_authorities():
    print("-- picker: only active authorities backed by an ACTIVE authority admin --")
    staffed = _make_authority("Violet Umbrella Office")
    _make_admin(staffed.id)
    unstaffed = _make_authority("Violet Umbrella Office (No Admin)")
    admin_off = _make_authority("Violet Umbrella Office (Inactive Admin)")
    _make_admin(admin_off.id, active=False)
    inactive = _make_authority("Violet Umbrella Office (Inactive)", active=False)
    _make_admin(inactive.id)

    r = client.get("/api/authority/active")
    check("picker endpoint 200", r.status_code == 200, str(r.status_code))
    ids = {a["authority_id"] for a in r.json().get("authorities", [])}
    check("staffed active authority offered", staffed.id in ids, str(ids))
    check("active-but-unstaffed authority NOT offered", unstaffed.id not in ids)
    check("admin-deactivated authority NOT offered", admin_off.id not in ids)
    check("inactive authority NOT offered even with admin", inactive.id not in ids)


def test_picker_drops_authority_when_last_admin_deactivated():
    print("-- picker: deactivating the last administrator removes the authority --")
    staffed = _make_authority("Violet Umbrella Office (Last Admin)")
    uid = _make_admin(staffed.id)

    r = client.get("/api/authority/active")
    ids = {a["authority_id"] for a in r.json().get("authorities", [])}
    check("offered before deactivation", staffed.id in ids, str(ids))

    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == uid).first()
        user.is_active = False
        db.commit()
    finally:
        db.close()

    r2 = client.get("/api/authority/active")
    ids2 = {a["authority_id"] for a in r2.json().get("authorities", [])}
    check("dropped immediately (DB-driven)", staffed.id not in ids2, str(ids2))

    r3 = client.post("/api/grievances", json=_submit_payload(staffed.id), headers=_fake_ip(80))
    check("submission blocked after last admin deactivated", r3.status_code == 422, r3.text[:200])


# ---------------------------------------------------------------------------
# 2. Recommend (POST /api/grievances/recommend)
# ---------------------------------------------------------------------------


def test_recommend_only_returns_admin_backed_authorities():
    print("-- recommend: admin-backed authorities only --")
    staffed = _make_authority("Violet Umbrella Office")
    _make_admin(staffed.id)
    unstaffed = _make_authority("Violet Umbrella Office (No Admin)")

    r = client.post(
        "/api/grievances/recommend",
        json={"input": "violet umbrella results"},
        headers=_fake_ip(30),
    )
    check("recommend endpoint 200", r.status_code == 200, r.text[:200])
    body = r.json()
    top = body.get("authority") or {}
    alternatives = body.get("alternatives", [])
    ids = [x["authority_id"] for x in [top] + alternatives if x.get("authority_id")]
    check("staffed authority recommended", staffed.id in ids, str(ids))
    check("unstaffed active authority never recommended", unstaffed.id not in ids, str(ids))
    check("all recommendations have non-empty IDs", all(bool(x) for x in ids))


# ---------------------------------------------------------------------------
# 3. Auto-match (POST /api/authority/match)
# ---------------------------------------------------------------------------


def test_match_autoselects_admin_backed_authority():
    print("-- match: named admin-backed office is auto-selected --")
    staffed = _make_authority("Violet Umbrella Office")
    _make_admin(staffed.id)
    _make_authority("Violet Umbrella Office (No Admin)")

    r = client.post(
        "/api/authority/match",
        json={"text": "I want to complain to the Violet Umbrella Office"},
        headers=_fake_ip(40),
    )
    check("match endpoint 200", r.status_code == 200, r.text[:200])
    d = r.json()
    check("status matched", d.get("status") == "matched", str(d))
    check("matched authority is the admin-backed office", (d.get("authority") or {}).get("authority_id") == staffed.id, str(d.get("authority")))


def test_match_never_autoselects_unstaffed_authority():
    print("-- match: active-but-unstaffed office is unavailable, never matched --")
    lonely = _make_authority("Lonely Fern Office")

    r = client.post(
        "/api/authority/match",
        json={"text": "I want to complain to the Lonely Fern Office"},
        headers=_fake_ip(41),
    )
    d = r.json()
    check("status unavailable", d.get("status") == "unavailable", str(d))
    check("unstaffed office named", "Lonely Fern Office" in d.get("names", []), str(d))


# ---------------------------------------------------------------------------
# 4. Submit (POST /api/grievances)
# ---------------------------------------------------------------------------


def test_submit_accepts_admin_backed_authority():
    print("-- submit: admin-backed active office accepted --")
    staffed = _make_authority("Violet Umbrella Office")
    _make_admin(staffed.id)

    r = client.post("/api/grievances", json=_submit_payload(staffed.id), headers=_fake_ip(50))
    check("submit 201", r.status_code == 201, r.text[:300])
    ref = (r.json() or {}).get("reference", "")
    if ref:
        _created_refs.append(ref)


def test_submit_rejects_unstaffed_and_deadministered_authorities():
    print("-- submit: 422 for offices with no ACTIVE administrator --")
    unstaffed = _make_authority("Violet Umbrella Office (No Admin)")
    r1 = client.post("/api/grievances", json=_submit_payload(unstaffed.id), headers=_fake_ip(51))
    check("active-but-unstaffed rejected (422)", r1.status_code == 422, r1.text[:200])
    check("unstaffed rejection is explicit", "administrator" in (r1.text or "").lower(), r1.text[:200])

    staffed = _make_authority("Violet Umbrella Office (Dead Admin)")
    _make_admin(staffed.id, active=False)
    r2 = client.post("/api/grievances", json=_submit_payload(staffed.id), headers=_fake_ip(52))
    check("only-inactive-admin rejected (422)", r2.status_code == 422, r2.text[:200])

    inactive = _make_authority("Violet Umbrella Office (Inactive)", active=False)
    _make_admin(inactive.id)
    r3 = client.post("/api/grievances", json=_submit_payload(inactive.id), headers=_fake_ip(53))
    check("inactive-with-admin rejected (422)", r3.status_code == 422, r3.text[:200])


# ---------------------------------------------------------------------------
# 5. Contact lookup stays unrestricted
# ---------------------------------------------------------------------------


def test_contact_card_remains_unrestricted():
    print("-- contact lookup: plain office information stays available --")
    unstaffed = _make_authority("Lonely Fern Office")
    r = client.get(f"/api/authority/{unstaffed.id}")
    check("unstaffed active authority contact card still served", r.status_code == 200, str(r.status_code))

    from app.authority.matcher import find_authority
    matches = find_authority("lonely fern office")
    check("find_authority (contact path) unaffected", any(m.get("id") == unstaffed.id for m in matches), str([m.get("authority_name") for m in matches]))


def _main():
    tests = [
        test_picker_serves_only_admin_backed_authorities,
        test_picker_drops_authority_when_last_admin_deactivated,
        test_recommend_only_returns_admin_backed_authorities,
        test_match_autoselects_admin_backed_authority,
        test_match_never_autoselects_unstaffed_authority,
        test_submit_accepts_admin_backed_authority,
        test_submit_rejects_unstaffed_and_deadministered_authorities,
        test_contact_card_remains_unrestricted,
    ]
    try:
        for fn in tests:
            try:
                print(f"-- {fn.__name__} --")
                fn()
            except Exception as exc:  # noqa: BLE001
                FAIL.append(fn.__name__)
                print(f"  ERROR  {fn.__name__}: {exc}")
            _cleanup()
    finally:
        _cleanup()
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    _main()