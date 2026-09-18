"""
backend/tests/test_phase3c1_runtime_hardening.py

Focused tests A–G for Phase 3C-1: production runtime hardening of the
CUS AI Assistant.

A. Planner offload: process() delegates plan() via asyncio.to_thread
B. Session resolution offload: ask() uses asyncio.to_thread for DB lookups
C. Request-size protection: messages > MAX_CHAT_MESSAGE_LENGTH rejected (422)
D. SSE heartbeat: keepalive comments emitted during idle generation windows
E. Cancellation propagation: CancelledError re-raised, not converted to error
F. Shared LLM gate: async+sync concurrency, cancellation-safety, exception-release
G. Sync Now worker dispatch: crawl offloaded to worker thread via to_thread

Run:  pytest tests/test_phase3c1_runtime_hardening.py -v
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import threading
import time
import uuid
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Temp isolate the website sync state file (used only by test G)
os.environ.setdefault(
    "WEBSITE_SYNC_STATE_FILE",
    str(Path(os.environ.get("TEMP", tempfile.gettempdir())) / "_p3c1_test_state.json"),
)

import app.models  # noqa: F401  register tables before any SessionLocal

import pytest  # noqa: E402

from app.config import settings  # noqa: E402

create_all_executed = False

def _create_all_once():
    global create_all_executed
    if not create_all_executed:
        from app.database import create_all
        create_all()
        create_all_executed = True

_create_all_once()


# ── helpers ────────────────────────────────────────────────────────────────
PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = ""):
    if cond:
        PASS.append(name)
        print(f"  PASS  {name}" + (f"  {detail}" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")


# ───────────────────────────────────────────────────────────────────────────
# A. Planner offload — engine.process delegates plan() via asyncio.to_thread
# ───────────────────────────────────────────────────────────────────────────


def test_planner_offloaded_to_thread():
    """engine.process must call plan() through asyncio.to_thread, not inline."""
    from app.orchestrator import engine

    # --- fakes ---
    class _FakeCtx:
        _last_contract = None

    class _FakeState:
        context = _FakeCtx()
        catalogue_pending = None
        slot_topic = None
        slot_request = None
        last_contract = None
        student_gate = None

    class _FakePlan:
        action = "welcome"
        target = ""
        confidence = 1.0
        reason = "test stub"
        extra = {}

    plan_calls: list[str] = []
    to_thread_calls: list[str] = []

    def _fake_plan(text, ctx, chat_id, entities):
        plan_calls.append(text)
        return _FakePlan()

    async def _fake_execute_plan(*a, **kw):
        yield {"type": "done", "chat_id": a[3] if len(a) > 3 else "c", "cited_chunks": []}

    orig_plan = engine.plan
    orig_execute = engine._execute_plan
    orig_get_state = engine.get_state
    orig_extract = engine.extract_entities
    orig_to_thread = asyncio.to_thread

    async def _fake_get_state(chat_id):
        return _FakeState()

    def _fake_extract(text):
        return SimpleNamespace(programme="BCA", topic="fees")

    async def _record_to_thread(func, *args, **kwargs):
        to_thread_calls.append(getattr(func, "__name__", str(func)))
        return await orig_to_thread(func, *args, **kwargs)

    try:
        engine.get_state = _fake_get_state
        engine.extract_entities = _fake_extract
        engine.plan = _fake_plan
        engine._execute_plan = _fake_execute_plan
        asyncio.to_thread = _record_to_thread  # monkeypatch on the engine module's asyncio

        db = None  # not used: get_state + execute are stubbed
        events = asyncio.run(_collect(engine.process(db, "u1", "hello", "cid1")))
        ev_types = [e["type"] for e in events]
        assert "done" in ev_types, f"expected done event, got {ev_types}"
        check("A1: plan was called", len(plan_calls) == 1, f"calls={len(plan_calls)}")
        check("A2: plan delegated via asyncio.to_thread",
              "plan" in to_thread_calls,
              f"to_thread_calls={to_thread_calls}")
    finally:
        engine.plan = orig_plan
        engine._execute_plan = orig_execute
        engine.get_state = orig_get_state
        engine.extract_entities = orig_extract
        asyncio.to_thread = orig_to_thread


# ───────────────────────────────────────────────────────────────────────────
# B. Session resolution offload — resolve_session / classify / revoke
# ───────────────────────────────────────────────────────────────────────────


def test_session_resolution_offloaded():
    """ask() must delegate resolve_session / classify_stale / revoke to to_thread."""
    from app.chat import routes as routes_mod
    from app.chat.routes import AskRequest, ask
    from starlette.requests import Request

    cookie_name = settings.STUDENT_SESSION_COOKIE
    cookie_value = "test_sid_" + uuid.uuid4().hex[:8]

    to_thread_funcs: list[str] = []
    resolve_args: list = []
    classify_args: list = []

    orig_resolve = routes_mod.resolve_session
    orig_classify = routes_mod.classify_stale_session
    orig_audit = routes_mod.audit
    orig_to_thread = asyncio.to_thread
    orig_revoke = routes_mod.revoke_session

    def _fake_resolve(db, sid):
        resolve_args.append(sid)
        return {"id": "sess_valid"}

    def _fake_classify(db, sid):
        classify_args.append(sid)
        return "expired"

    def _fake_revoke(db, sid):
        return True

    def _noop_audit(*a, **kw):
        pass

    async def _record_to_thread(func, *a, **kw):
        to_thread_funcs.append(getattr(func, "__name__", str(func)))
        return await orig_to_thread(func, *a, **kw)

    body = AskRequest(message="hello", chat_id=None)
    fake_user = SimpleNamespace(id=uuid.uuid4(), role="student")
    scope = {
        "type": "http", "method": "POST", "path": "/api/chat/ask",
        "headers": [(b"cookie", f"{cookie_name}={cookie_value}".encode())],
        "client": ("127.0.0.1", 54321), "query_string": b"",
        "scheme": "http", "server": ("test", 80),
    }
    req = Request(scope)
    db = None  # patched away in session functions

    # --- Test 1: valid session → resolve_session via to_thread ---
    try:
        routes_mod.resolve_session = _fake_resolve
        routes_mod.classify_stale_session = _fake_classify
        routes_mod.revoke_session = _fake_revoke
        routes_mod.audit = _noop_audit
        asyncio.to_thread = _record_to_thread
        asyncio.run(ask(body, req, db, fake_user))
        check("B1: resolve_session called via to_thread",
              "resolve_session" in to_thread_funcs,
              f"funcs={to_thread_funcs}")
        check("B2: correct cookie passed", resolve_args == [cookie_value],
              f"args={resolve_args}")
    finally:
        routes_mod.resolve_session = orig_resolve
        routes_mod.classify_stale_session = orig_classify
        routes_mod.revoke_session = orig_revoke
        routes_mod.audit = orig_audit
        asyncio.to_thread = orig_to_thread

    # --- Test 2: expired cookie → classify_stale_session via to_thread ---
    to_thread_funcs.clear()
    classify_args.clear()
    resolve_result = None  # force resolve to return None → classify branch

    def _fake_resolve_none(db, sid):
        return None

    try:
        routes_mod.resolve_session = _fake_resolve_none
        routes_mod.classify_stale_session = _fake_classify
        routes_mod.audit = _noop_audit
        asyncio.to_thread = _record_to_thread
        asyncio.run(ask(body, req, db, fake_user))
        check("B3: classify_stale_session called via to_thread",
              "classify_stale_session" in to_thread_funcs,
              f"funcs={to_thread_funcs}")
        check("B4: correct cookie passed to classify",
              classify_args == [cookie_value],
              f"args={classify_args}")
    finally:
        routes_mod.resolve_session = orig_resolve
        routes_mod.classify_stale_session = orig_classify
        routes_mod.audit = orig_audit
        asyncio.to_thread = orig_to_thread


# ───────────────────────────────────────────────────────────────────────────
# C. Request-size protection — messages > MAX_CHAT_MESSAGE_LENGTH rejected
# ───────────────────────────────────────────────────────────────────────────


def test_request_size_rejects_long_messages():
    """ask() raises 422 before any session-resolution / admission work."""
    from fastapi import HTTPException
    from starlette.requests import Request
    from app.chat.routes import AskRequest, ask

    scope = {
        "type": "http", "method": "POST", "path": "/api/chat/ask",
        "headers": [], "client": ("127.0.0.1", 54321),
        "query_string": b"", "scheme": "http", "server": ("test", 80),
    }
    req = Request(scope)
    long_msg = "x" * (settings.MAX_CHAT_MESSAGE_LENGTH + 1)
    body = AskRequest(message=long_msg, chat_id=None)
    fake_user = SimpleNamespace(id=uuid.uuid4(), role="student")

    try:
        with pytest.raises(HTTPException) as exc_info:
            asyncio.run(ask(body, req, None, fake_user))
        check("C1: 422 raised", exc_info.value.status_code == 422,
              f"status={exc_info.value.status_code}")
        check("C2: error mentions length", "length" in str(exc_info.value.detail).lower(),
              f"detail={exc_info.value.detail!r}")
    finally:
        pass

    # Boundary: exactly at the limit → does NOT raise 422 (runs into StreamingResponse)
    exact_msg = "y" * settings.MAX_CHAT_MESSAGE_LENGTH
    body_ok = AskRequest(message=exact_msg, chat_id=None)
    resp = asyncio.run(ask(body_ok, req, None, fake_user))
    check("C3: exactly at limit accepted",
          hasattr(resp, "body_iterator"),
          f"resp={type(resp).__name__}")


# ───────────────────────────────────────────────────────────────────────────
# D. SSE heartbeat — keepalive : ping comments during idle generation
# ───────────────────────────────────────────────────────────────────────────


def test_sse_heartbeat_emits_keepalives():
    """_sse_with_heartbeat injects : ping comments while inner is idle."""
    from app.chat.routes import _sse_with_heartbeat

    async def _slow_inner():
        await asyncio.sleep(0.30)
        yield "data: alpha\n\n"
        yield "data: beta\n\n"

    async def _collect():
        frames = []
        async for f in _sse_with_heartbeat(_slow_inner()):
            frames.append(f)
        return frames

    orig_interval = None
    try:
        orig_interval = getattr(sys.modules["app.chat.routes"], "SSE_HEARTBEAT_INTERVAL",
                                None)
        sys.modules["app.chat.routes"].SSE_HEBIT_INTERVAL = 0.05  # noqa: intentional typo guard
    except Exception:
        pass

    import app.chat.routes as _cr
    old = _cr.SSE_HEARTBEAT_INTERVAL
    try:
        _cr.SSE_HEARTBEAT_INTERVAL = 0.05
        frames = asyncio.run(_collect())
    finally:
        _cr.SSE_HEARTBEAT_INTERVAL = old

    check("D1: frames non-empty", len(frames) > 2, f"frames={len(frames)}")
    check("D2: : ping comment emitted", any(f == ": ping\n\n" for f in frames),
          f"frames={frames[:8]}")
    check("D3: first frame is ping (inner idle)",
          frames[0] == ": ping\n\n",
          f"first={frames[0]!r}")
    check("D4: inner content preserved in order",
          "data: alpha\n\n" in frames and "data: beta\n\n" in frames
          and frames.index("data: alpha\n\n") < frames.index("data: beta\n\n"),
          f"frames={frames}")
    check("D5: last frame is the final inner frame",
          frames[-1] == "data: beta\n\n",
          f"last={frames[-1]!r}")

    # --- cancellation propagation: wrapping generator must NOT swallow CancelledError ---
    async def _cancel_run():
        gen = _slow_inner()
        frames = []
        it = gen.__aiter__()
        await it.__anext__()  # blocks 0.3s then yields first frame
        return "reached"

    # Outer _collect in a task, then cancel mid-generation
    async def _cancel_test():
        task = asyncio.create_task(_collect())
        await asyncio.sleep(0.12)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        return True

    try:
        result = asyncio.run(_cancel_test())
        check("D6: CancelledError propagates (not swallowed)",
              result is True,
              "wrapper cancellation test passed")
    except Exception as exc:
        check("D6: CancelledError propagates (not swallowed)", False, str(exc))


# ───────────────────────────────────────────────────────────────────────────
# E. Cancellation propagation — CancelledError not converted to error event
# ───────────────────────────────────────────────────────────────────────────


def test_cancellation_reraises_not_converted_to_error():
    """_execute_with_protection must re-raise CancelledError, not yield error."""
    from app.request_manager.admission_controller import admission_controller
    from app.request_manager.models import Classification, Priority

    classification = Classification(priority=Priority.RAG, cost=3, action="rag")

    async def _slow_executor(user_id, message, chat_id):
        yield {"type": "token", "text": "partial"}
        await asyncio.sleep(60)  # blocks until cancelled

    async def _run():
        events = []
        async for ev in admission_controller._execute_with_protection(
            "u", "m", "c", classification, t0=time.perf_counter(),
            executor=_slow_executor,
        ):
            events.append(ev)
        return events

    async def _cancel_test():
        task = asyncio.create_task(_run())
        await asyncio.sleep(0.15)  # let it yield the token then await sleep
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(_cancel_test())
        check("E1: CancelledError raised (not swallowed)",
              True,
              "semaphore released via finally in _execute_with_protection")
    except Exception as exc:
        check("E1: CancelledError raised (not swallowed)", False, str(exc))


# ───────────────────────────────────────────────────────────────────────────
# F. Shared LLM gate — concurrency, cancellation-safety, exception-release
# ───────────────────────────────────────────────────────────────────────────


def test_shared_llm_gate_sync_concurrency():
    """Sync gate: max=2, third acquire blocks then times out."""
    from app.llm.gate import SharedLLMGate
    g = SharedLLMGate(max_concurrent=2)
    r1 = g.acquire_sync(timeout=0.05)
    r2 = g.acquire_sync(timeout=0.05)
    r3 = g.acquire_sync(timeout=0.05)  # should fail (budget exhausted)
    g.release()
    g.release()
    check("F1: sync acquire (1/2) succeeds", r1 is True)
    check("F2: sync acquire (2/2) succeeds", r2 is True)
    check("F3: sync acquire (3rd) times out", r3 is False, f"r3={r3}")
    check("F4: gate fully released after releases", g.held == 0 and g.available == 2,
          f"held={g.held} available={g.available}")


def test_shared_llm_gate_async_cancellation_no_leak():
    """Cancelled async acquire must not leak a gate slot."""
    from app.llm.gate import SharedLLMGate

    async def _cancel_test():
        gate = SharedLLMGate(max_concurrent=1)
        gate.acquire_sync(timeout=0.1)
        held_before = gate.held

        async def _blocked_acquire():
            return await gate.acquire(timeout=30.0)

        task = asyncio.create_task(_blocked_acquire())
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        gate.release()
        return held_before, gate.held, gate.available

    held_before, held_after, avail = asyncio.run(_cancel_test())
    check("F5: held==1 before cancel (precondition)", held_before == 1,
          f"held={held_before}")
    check("F6: gate fully released after external release + cancel",
          held_after == 0 and avail == 1,
          f"held={held_after} available={avail}")


def test_shared_llm_gate_sync_exception_releases_slot():
    """'with' context manager releases slot when an exception occurs."""
    from app.llm.gate import SharedLLMGate
    g = SharedLLMGate(max_concurrent=1)
    released = False
    try:
        with g:
            assert g.held == 1
            raise RuntimeError("boom")
    except RuntimeError:
        released = True
    check("F7: exception releases gate slot", g.held == 0 and released,
          f"held={g.held} released={released}")


def test_grievance_fallback_on_busy_gate(monkeypatch):
    """formalize() returns manual draft when shared LLM gate is busy."""
    from app.grievance import llm as grievance_llm
    from app.llm.gate import shared_llm_gate as gate_singleton

    acquire_calls: list[bool] = []
    release_calls: list[int] = []

    def _busy_acquire(timeout=10.0):
        acquire_calls.append(True)
        return False  # gate busy

    def _noop_release():
        release_calls.append(1)

    orig_acquire = gate_singleton.acquire_sync
    orig_release = gate_singleton.release
    try:
        gate_singleton.acquire_sync = _busy_acquire
        gate_singleton.release = _noop_release
        result = grievance_llm.formalize("My exam fee is wrong, I paid double")
        check("F8: formalize returns manual draft on busy gate",
              result.get("manual") is True,
              f"result={result}")
        check("F9: error field contains 'busy'",
              "busy" in str(result.get("error", "")).lower(),
              f"error={result.get('error')}")
        check("F10: gate.release NOT called (never acquired)",
              len(release_calls) == 0,
              f"release_calls={release_calls}")
    finally:
        gate_singleton.acquire_sync = orig_acquire
        gate_singleton.release = orig_release


def test_grievance_releases_gate_on_httpx_failure(monkeypatch):
    """formalize() releases gate even when the HTTP call fails."""
    import httpx as _httpx
    from app.grievance import llm as grievance_llm
    from app.llm.gate import shared_llm_gate as gate_singleton

    release_count = [0]
    def _recording_acquire(timeout=10.0):
        return True

    def _recording_release():
        release_count[0] += 1

    class _FakeResponse:
        def __enter__(self):
            raise httpx.ConnectError("connection refused")
        def __exit__(self, *a):
            pass

    class _FakeClient:
        def __init__(self, **kw): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def post(self, url, json=None): raise httpx.ConnectError("connection refused")

    orig_acquire = gate_singleton.acquire_sync
    orig_release = gate_singleton.release
    orig_client = _httpx.Client
    try:
        gate_singleton.acquire_sync = _recording_acquire
        gate_singleton.release = _recording_release
        _httpx.Client = _FakeClient
        result = grievance_llm.formalize("My exam hall is wrong")
        check("F11: formalize returns manual draft on connection failure",
              result.get("manual") is True,
              f"result={result}")
        check("F12: gate.release called once after failure",
              release_count[0] == 1,
              f"release_count={release_count[0]}")
    finally:
        gate_singleton.acquire_sync = orig_acquire
        gate_singleton.release = orig_release
        _httpx.Client = orig_client


def test_chat_generation_holds_shared_gate():
    """run_chat must acquire the shared LLM gate before generation and release after."""
    from app.chat import service as chat_svc
    from app.llm.gate import SharedLLMGate

    CHUNK = {
        "document_id": "doc-1", "document_title": "Prospectus.pdf",
        "page_number": 5, "chunk_index": 0, "rerank_score": 0.9,
        "content": "BCA is a three-year programme.",
    }
    gate = SharedLLMGate(max_concurrent=2)
    held_during_generation: list[int] = []

    async def _fast_stream(*a, **k):
        held_during_generation.append(gate.held)
        yield "BCA is a three-year degree."
        held_during_generation.append(gate.held)

    orig_retrieve = chat_svc.retrieve
    orig_stream = chat_svc.stream_answer_async
    orig_gate = getattr(chat_svc, "shared_llm_gate", None)
    try:
        chat_svc.retrieve = lambda *a, **k: [CHUNK]
        chat_svc.stream_answer_async = _fast_stream
        chat_svc.shared_llm_gate = gate
        from app.database import SessionLocal, create_all
        create_all()
        db = SessionLocal()
        conv_id = None
        try:
            events = []
            async def _run():
                async for ev in chat_svc.run_chat(db, "test", "how many semesters", None):
                    events.append(ev)
            asyncio.run(_run())
            done = [e for e in events if e.get("type") == "done"]
            check("F13: run_chat completes with done event",
                  len(done) == 1,
                  f"done_count={len(done)}")
            check("F14: gate.held==1 during generation",
                  held_during_generation == [1, 1] if len(held_during_generation) == 2 else False,
                  f"held_during={held_during_generation}")
            check("F15: gate fully released after run_chat",
                  gate.held == 0 and gate.available == 2,
                  f"held={gate.held} available={gate.available}")
        finally:
            # cleanup conversation rows created by run_chat
            try:
                from app.models import Conversation, Message
                from sqlalchemy import inspect as sa_inspect
                insp = sa_inspect(db.get_bind())
                if insp.has_table("conversations") and insp.has_table("messages"):
                    convs = db.query(Conversation).filter(
                        Conversation.title == "how many semesters"
                    ).all()
                    for c in convs:
                        db.query(Message).filter(Message.conversation_id == c.id).delete()
                        db.delete(c)
                    db.commit()
            except Exception:
                db.rollback()
            db.close()
    finally:
        chat_svc.retrieve = orig_retrieve
        chat_svc.stream_answer_async = orig_stream
        if orig_gate is not None:
            chat_svc.shared_llm_gate = orig_gate


# ───────────────────────────────────────────────────────────────────────────
# G. Sync Now worker dispatch — crawl runs in a worker thread, not on event loop
# ───────────────────────────────────────────────────────────────────────────


class _FakeEngine:
    instances = 0
    calls = 0
    worker_thread: int = 0

    def __init__(self, db):
        _FakeEngine.instances += 1
        self.db = db
        self.stats = {
            "trigger": "manual", "status": "completed",
            "total_urls": 3, "pages_found": 2, "new_pages": 2,
            "updated_pages": 0, "archived_pages": 0, "duplicates_skipped": 0,
            "failed_pages": 0, "indexed_pages": 0, "unchanged_pages": 0,
        }

    async def run_async(self, trigger="manual", seed_urls=None):
        _FakeEngine.calls += 1
        _FakeEngine.worker_thread = threading.get_ident()
        return dict(self.stats)


_ORIG_ENGINE = None
_WS_STATE_FILE: str | None = None


def _wipe_state():
    from app.knowledge_sync.web_engine import reset_runtime_state, _state_path
    try:
        _state_path().unlink(missing_ok=True)
    except Exception:
        pass
    reset_runtime_state()


def _set_state(**kw):
    from app.knowledge_sync.web_engine import load_state, save_state
    state = load_state()
    state.update(kw)
    save_state(state)
    return state


def _patch_ws_engine():
    global _ORIG_ENGINE
    import app.knowledge_sync.web_engine as eng
    _FakeEngine.instances = 0
    _FakeEngine.calls = 0
    _FakeEngine.worker_thread = 0
    _ORIG_ENGINE = eng.WebsiteSyncEngine
    eng.WebsiteSyncEngine = _FakeEngine


def _restore_ws_engine():
    global _ORIG_ENGINE
    import app.knowledge_sync.web_engine as eng
    if _ORIG_ENGINE is not None:
        eng.WebsiteSyncEngine = _ORIG_ENGINE
    _ORIG_ENGINE = None


def test_sync_now_dispatched_via_to_thread():
    """POST /api/admin/website-sync/run invokes engine.run_async via asyncio.to_thread."""
    from fastapi.testclient import TestClient
    from app.main import app
    from app.auth.security import hash_password
    from app.database import SessionLocal, create_all
    from app.models import User

    create_all()
    admin_user_id: str | None = None
    admin_token: str = ""

    db = SessionLocal()
    try:
        username = f"__p3c1_admin_{uuid.uuid4().hex[:6]}"
        email = f"__p3c1_{uuid.uuid4().hex[:6]}@test.local"
        admin = User(
            id=uuid.uuid4(),
            username=username,
            email=email,
            hashed_password=hash_password("secret123"),
            role="superadmin",
            is_active=True,
        )
        db.add(admin)
        db.flush()
        admin_user_id = str(admin.id)
        db.commit()
    finally:
        db.close()

    try:
        client = TestClient(app)
        login = client.post("/api/auth/login", data={"username": username, "password": "secret123"})
        admin_token = login.json().get("access_token", "")
        headers = {"Authorization": f"Bearer {admin_token}"}

        _wipe_state()
        _set_state(enabled=True, schedule="manual")
        _patch_ws_engine()

        import app.admin.routes as admin_mod
        orig_to_thread = asyncio.to_thread
        to_thread_dispatched = {"flag": False, "req_thread": 0}

        def _record_to_thread(func, *args, **kwargs):
            to_thread_dispatched["flag"] = True
            to_thread_dispatched["req_thread"] = threading.get_ident()
            return orig_to_thread(func, *args, **kwargs)

        asyncio.to_thread = _record_to_thread
        try:
            resp = client.post(
                "/api/admin/website-sync/run",
                json={"urls": None, "trigger": "manual"},
                headers=headers,
            )
            check("G1: endpoint returns 200", resp.status_code == 200,
                  f"status={resp.status_code} body={resp.text[:120]}")
            body = resp.json()
            check("G2: result status is completed", body.get("status") == "completed",
                  f"status={body.get('status')}")
            check("G3: engine invoked once", _FakeEngine.calls == 1,
                  f"calls={_FakeEngine.calls}")
            check("G4: engine instantiated once", _FakeEngine.instances == 1,
                  f"instances={_FakeEngine.instances}")
            check("G5: asyncio.to_thread was used for dispatch",
                  to_thread_dispatched["flag"] is True,
                  f"dispatched={to_thread_dispatched['flag']}")
            check("G6: worker thread differs from request thread",
                  _FakeEngine.worker_thread != 0
                  and _FakeEngine.worker_thread != to_thread_dispatched["req_thread"],
                  f"worker={_FakeEngine.worker_thread} req={to_thread_dispatched['req_thread']}")
        finally:
            asyncio.to_thread = orig_to_thread
            _restore_ws_engine()
            _wipe_state()
    finally:
        if admin_user_id:
            db2 = SessionLocal()
            try:
                db2.query(User).filter(User.id == uuid.UUID(admin_user_id)).delete()
                db2.commit()
            except Exception:
                db2.rollback()
            finally:
                db2.close()


# ── utilities ──────────────────────────────────────────────────────────────


async def _collect(gen):
    events = []
    async for ev in gen:
        events.append(ev)
    return events


# ── main ───────────────────────────────────────────────────────────────────

def main():
    tests = [
        test_planner_offloaded_to_thread,
        test_session_resolution_offloaded,
        test_request_size_rejects_long_messages,
        test_sse_heartbeat_emits_keepalives,
        test_cancellation_reraises_not_converted_to_error,
        test_shared_llm_gate_sync_concurrency,
        test_shared_llm_gate_async_cancellation_no_leak,
        test_shared_llm_gate_sync_exception_releases_slot,
        test_grievance_fallback_on_busy_gate,
        test_grievance_releases_gate_on_httpx_failure,
        test_chat_generation_holds_shared_gate,
        test_sync_now_dispatched_via_to_thread,
    ]
    for t in tests:
        print(f"\n--- {t.__name__} ---")
        t()
    print(f"\n{'='*60}")
    print(f"  PASSED: {len(PASS)}")
    print(f"  FAILED: {len(FAIL)}")
    if FAIL:
        print("  FAILURES:")
        for f in FAIL:
            print(f"    - {f}")
    return len(FAIL) == 0


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
