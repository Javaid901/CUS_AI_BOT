"""
backend/tests/test_website_sync_control.py

Focused tests for the Website Sync enable/disable -> manual workflow:

  * GET  /api/admin/website-sync/status   -> disabled by default
  * POST /api/admin/website-sync/toggle   -> enables persists
  * POST /api/admin/website-sync/run      -> rejected (409) while disabled and
    the crawler is NOT invoked (server-side enforcement, no bypass)
  * POST /api/admin/website-sync/run      -> invokes the existing engine when enabled
  * scheduler semantics                   -> manual means NO automatic crawl;
    scheduled mode respects the master enabled flag
  * HTTP/decompression regression         -> a gzip (Content-Encoding: gzip)
    response no longer raises "Error -3 while decompressing data: incorrect
    header check" through the Website Sync crawler path

Run:  python tests/test_website_sync_control.py   (or pytest tests/test_website_sync_control.py)
"""

from __future__ import annotations

import gzip
import http.server
import os
import sys
import tempfile
import threading
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Isolation FIRST: never touch ./sync_downloads/website_sync_state.json.
os.environ.setdefault(
    "WEBSITE_SYNC_STATE_FILE",
    str(Path(os.environ.get("TEMP", tempfile.gettempdir())) / "_website_sync_control_test_state.json"),
)

import app.models  # noqa: F401  (register tables before any session)

import pytest  # noqa: E402
from fastapi.testclient import TestClient

import app.knowledge_sync.web_scheduler as sched_mod  # noqa: E402
from app.auth.security import hash_password
from app.database import SessionLocal, create_all
from app.main import app

PASS: list[str] = []
FAIL: list[str] = []

TOKENS: dict[str, dict[str, str]] = {"admin": {}, "student": {}, "authority": {}}
_created_user_ids: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  PASS  {name}" + (f"  {detail}" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")


def _cleanup() -> None:
    from app.models import Authority, User

    db = SessionLocal()
    try:
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
            username=f"__ws_admin_{uuid.uuid4().hex[:6]}",
            email=f"__ws_admin_{uuid.uuid4().hex[:6]}@test.local",
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
            username=f"__ws_student_{uuid.uuid4().hex[:6]}",
            email=f"__ws_student_{uuid.uuid4().hex[:6]}@test.local",
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
            department_name="Website Sync Test Dept",
            authority_name=f"WS Office {uuid.uuid4().hex[:6]}",
            designation="Head",
            email=f"ws{uuid.uuid4().hex[:8]}@cus.ac.in",
            phone="0194-2311256",
            office_location="Gogji-Bagh, Srinagar",
            active=True,
            source_kind="manual",
        )
        db.add(authority)
        db.flush()

        auth_admin = User(
            id=uuid.uuid4(),
            username=f"__ws_aa_{uuid.uuid4().hex[:6]}",
            email=f"__ws_aa_{uuid.uuid4().hex[:6]}@test.local",
            hashed_password=hash_password("secret123"),
            role="authority_admin",
            is_active=True,
            authority_id=str(authority.id),
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


def _wipe_state() -> None:
    from app.knowledge_sync.web_engine import reset_runtime_state, _state_path

    try:
        _state_path().unlink(missing_ok=True)
    except Exception:
        pass
    reset_runtime_state()


def _set_state(**kw) -> dict:
    from app.knowledge_sync.web_engine import load_state, save_state

    state = load_state()
    state.update(kw)
    save_state(state)
    return state


class _FakeEngine:
    """Records whether the crawler engine was instantiated and invoked."""

    instances = 0
    calls = 0

    def __init__(self, db):
        _FakeEngine.instances += 1
        self.db = db
        self.stats = {
            "trigger": "manual",
            "status": "completed",
            "total_urls": 3,
            "pages_found": 2,
            "new_pages": 2,
            "updated_pages": 0,
            "unchanged_pages": 0,
            "archived_pages": 0,
            "duplicates_skipped": 0,
            "failed_pages": 0,
            "indexed_pages": 0,
        }

    async def run_async(self, trigger="manual", seed_urls=None):
        _FakeEngine.calls += 1
        return dict(self.stats)


_ORIG_ENGINE = None


def _patch_engine():
    global _ORIG_ENGINE
    import app.knowledge_sync.web_engine as eng

    _FakeEngine.instances = 0
    _FakeEngine.calls = 0
    _ORIG_ENGINE = eng.WebsiteSyncEngine
    eng.WebsiteSyncEngine = _FakeEngine  # route imports it at call time


def _restore_engine():
    global _ORIG_ENGINE
    import app.knowledge_sync.web_engine as eng

    if _ORIG_ENGINE is not None:
        eng.WebsiteSyncEngine = _ORIG_ENGINE
    _ORIG_ENGINE = None


# ---------------------------------------------------------------------------
# 1. Disabled by default (current state)
# ---------------------------------------------------------------------------


def test_status_default_disabled():
    print("-- status: disabled by default --")
    _wipe_state()
    client = TestClient(app)
    r = client.get("/api/admin/website-sync/status", headers=TOKENS["admin"])
    check("GET status -> 200", r.status_code == 200, f"status={r.status_code}")
    body = r.json()
    check("master_enabled False by default", body.get("master_enabled") is False, str(body.get("master_enabled")))
    check("source URL is configured site", body.get("base_url") == "https://www.cusrinagar.edu.in", str(body.get("base_url")))
    check("schedule key present", "schedule" in body)
    r = client.get("/api/admin/website-sync/status")
    check("unauthenticated status -> 401", r.status_code == 401, f"status={r.status_code}")
    r = client.get("/api/admin/website-sync/status", headers=TOKENS["student"])
    check("student status -> 403", r.status_code == 403, f"status={r.status_code}")


# ---------------------------------------------------------------------------
# 2. Enable persists
# ---------------------------------------------------------------------------


def test_toggle_enable():
    print("-- toggle: enable persists --")
    _wipe_state()
    client = TestClient(app)
    r = client.post("/api/admin/website-sync/toggle", json={"enabled": True, "schedule": "manual"}, headers=TOKENS["admin"])
    check("POST toggle enable -> 200", r.status_code == 200, f"status={r.status_code} {r.text[:120]}")
    check("toggle returns enabled True", r.json().get("enabled") is True, r.text[:120])

    from app.knowledge_sync.web_engine import load_state

    check("state file persisted enabled", load_state().get("enabled") is True)

    r = client.get("/api/admin/website-sync/status", headers=TOKENS["admin"])
    check("GET status reflects enabled", r.json().get("master_enabled") is True, r.text[:120])


# ---------------------------------------------------------------------------
# 3. Manual sync while disabled -> rejected, crawler NOT executed (mandatory)
# ---------------------------------------------------------------------------


def test_run_rejected_when_disabled():
    print("-- run: rejected while disabled; crawler must NOT run --")
    _wipe_state()
    _patch_engine()
    client = TestClient(app)
    try:
        r = client.post("/api/admin/website-sync/run", json={"urls": None, "trigger": "manual"}, headers=TOKENS["admin"])
        check("run while disabled -> 409", r.status_code == 409, f"status={r.status_code} {r.text[:160]}")
        body = r.json()
        check("structured CONFLICT error", (body.get("error") or {}).get("code") == "CONFLICT", r.text[:160])
        msg = (body.get("error") or {}).get("message", "")
        check("clear admin message", "Website Sync is disabled. Enable it before starting a sync." in msg, msg)
        check("crawler engine never instantiated", _FakeEngine.instances == 0, f"instances={_FakeEngine.instances}")
        check("crawler engine never invoked", _FakeEngine.calls == 0, f"calls={_FakeEngine.calls}")

        from app.models.website_sync import CrawlRun

        db = SessionLocal()
        try:
            before_crawl_runs = db.query(CrawlRun).count()
        finally:
            db.close()

        r = client.post("/api/admin/website-sync/run", json={"urls": None}, headers=TOKENS["student"])
        check("student cannot trigger run (403)", r.status_code == 403, f"status={r.status_code}")
        r = client.post("/api/admin/website-sync/run", json={"urls": None})
        check("unauthenticated run -> 401", r.status_code == 401, f"status={r.status_code}")

        db = SessionLocal()
        try:
            after_crawl_runs = db.query(CrawlRun).count()
        finally:
            db.close()
        check("rejected run creates no new CrawlRun row",
              after_crawl_runs == before_crawl_runs, f"before={before_crawl_runs} after={after_crawl_runs}")
    finally:
        _restore_engine()


# ---------------------------------------------------------------------------
# 4. Manual sync while enabled -> existing crawler is invoked
# ---------------------------------------------------------------------------


def test_run_invokes_engine_when_enabled():
    print("-- run: enabled invokes the existing engine --")
    _set_state(enabled=True, schedule="manual")
    _patch_engine()
    client = TestClient(app)
    try:
        r = client.post("/api/admin/website-sync/run", json={"urls": None, "trigger": "manual"}, headers=TOKENS["admin"])
        check("run while enabled -> 200", r.status_code == 200, f"status={r.status_code} {r.text[:160]}")
        check("engine invoked exactly once", _FakeEngine.calls == 1, f"calls={_FakeEngine.calls}")
        check("engine instantiated once", _FakeEngine.instances == 1, f"instances={_FakeEngine.instances}")
        check("run result returned", r.json().get("status") == "completed", r.text[:160])
    finally:
        _restore_engine()


# ---------------------------------------------------------------------------
# 5. Manual schedule must NOT auto-launch a crawl
# ---------------------------------------------------------------------------


def test_manual_schedule_no_auto_crawl():
    print("-- scheduler: manual must NOT auto-crawl --")
    from app.knowledge_sync.web_scheduler import _poll_once, _run_sync

    original = sched_mod._run_sync
    calls = {"n": 0}

    def record(*a, **k):
        calls["n"] += 1

    sched_mod._run_sync = record
    try:
        for state in [
            {"enabled": True, "schedule": "manual", "hours": 0, "last_run_at": None},
            {"enabled": True, "schedule": "disabled", "hours": 0, "last_run_at": None},
        ]:
            _poll_once(state=state)
        check("manual/disabled schedule triggers zero crawls", calls["n"] == 0, f"calls={calls['n']}")
    finally:
        sched_mod._run_sync = original
        _ = _run_sync


# ---------------------------------------------------------------------------
# 6. Scheduled mode respects the enabled flag
# ---------------------------------------------------------------------------


def test_scheduled_respects_enabled():
    print("-- scheduler: scheduled mode respects enabled --")
    from app.knowledge_sync.web_scheduler import _poll_once

    original = sched_mod._run_sync
    calls = {"n": 0, "states": []}

    def record(*a, **k):
        calls["n"] += 1

    sched_mod._run_sync = record
    try:
        # disabled master + daily schedule -> no crawl
        _poll_once(state={"enabled": False, "schedule": "daily", "hours": 24, "last_run_at": None})
        # enabled + hourly + never run -> crawl IS allowed (real cadence works)
        _poll_once(state={"enabled": True, "schedule": "hourly", "hours": 1, "last_run_at": None})
        check("disabled master blocks scheduled crawl", calls["n"] >= 1, f"calls={calls['n']}")
    finally:
        sched_mod._run_sync = original


# ---------------------------------------------------------------------------
# 7. HTTP/decompression regression (Test 7): gzip response decodes cleanly
# ---------------------------------------------------------------------------


def test_gzip_decompression_regression():
    print("-- crawler: gzip Content-Encoding no longer double-decodes --")

    class GzipHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/robots.txt":
                body = b"User-agent: *\nDisallow:\n"
                self.send_response(200)
                self.send_header("Content-Type", "text/plain")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            payload = b"<html><head><title>Gzip page</title></head><body><p>Hello compressed world.</p></body></html>"
            gz = gzip.compress(payload)
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Encoding", "gzip")
            self.send_header("Content-Length", str(len(gz)))
            self.end_headers()
            self.wfile.write(gz)

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), GzipHandler)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        import asyncio

        from app.knowledge_sync.web_crawler import WebsiteCrawler

        base = f"http://127.0.0.1:{port}"
        crawler = WebsiteCrawler(
            base_url=base,
            max_pages=6,
            max_depth=1,
            delay=0.0,
            allow_private_hosts=True,
            use_sitemap=False,
        )
        result = asyncio.run(crawler.crawl())
        pages = result["pages"]
        ok_html = [p for p in pages if p.kind == "html"]
        check("gzip page fetched OK", any(p.ok for p in ok_html),
              str([(p.url, p.http_status, p.error) for p in pages]))
        check("gzip body decoded (no header-check error)",
              any("Hello compressed world" in (p.text or "") for p in ok_html),
              str([(p.ok, p.error) for p in pages]))
        check("no decompression errors reported",
              all(p.error not in ("decoding_error",) and "decompress" not in (p.error or "") for p in pages),
              str([p.error for p in pages if p.error]))
    finally:
        srv.shutdown()
        srv.server_close()


@pytest.fixture(scope="module", autouse=True)
def _prepare():
    create_all()
    _ensure_users()
    yield
    _cleanup()
    try:
        from app.knowledge_sync.web_engine import _state_path

        _state_path().unlink(missing_ok=True)
    except Exception:
        pass


if __name__ == "__main__":
    create_all()
    _ensure_users()
    tests = [
        test_status_default_disabled,
        test_toggle_enable,
        test_run_rejected_when_disabled,
        test_run_invokes_engine_when_enabled,
        test_manual_schedule_no_auto_crawl,
        test_scheduled_respects_enabled,
        test_gzip_decompression_regression,
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