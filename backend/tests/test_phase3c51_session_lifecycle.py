"""
backend/tests/test_phase3c51_session_lifecycle.py

Phase 3C-5.1 — Database Session Lifecycle & SSE Connection Decoupling.

Invariants under test:
  1. `run_chat` releases the request-scoped DB session immediately after the
     user-message write, BEFORE the retrieval / LLM-generation wait, so the
     SSE stream never parks a pooled connection for tens of seconds.
  2. The assistant-turn persistence happens in a FRESH short-lived session —
     open -> write -> commit -> close — never on the (closed) request session.
  3. The assistant message is really persisted (row, title, updated_at) and
     the flow still yields the exact SSE contract (token + done).

The generation and retrieval layers are mocked exactly like the other
chat test suites (no real Ollama/Chroma), so the pool/close assertions are
deterministic.
"""

import asyncio
import uuid
from sqlalchemy.orm import Session, sessionmaker

from app.chat import service as chat_service
from app.database import SessionLocal, create_all, engine

_RETRIEVED_CHUNK = {
    "id": "chunk-p3c51",
    "document_id": "00000000-0000-0000-0000-0000000000aa",
    "document_title": "Under-Graduate Courses 2024",
    "heading": "Courses Offered",
    "page_number": 1,
    "chunk_index": 0,
    "rerank_score": 0.91,
    "combined_score": 0.9,
    "text": "BCA is a three-year undergraduate programme.",
}


class TrackingSession(Session):
    """A Session that records close() calls so tests can observe lifecycle."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.close_count = 0

    def close(self, *args, **kwargs):
        self.close_count += 1
        return super().close(*args, **kwargs)


def _tracking_factory():
    return sessionmaker(bind=engine, class_=TrackingSession, autoflush=False, autocommit=False, future=True)


def _cleanup_conversation(conv_id, db=None):
    """Delete the conversation (and its messages) created by a test run."""
    from app.models import Conversation, Message

    closer = db or SessionLocal()
    try:
        cid = uuid.UUID(str(conv_id))
        closer.query(Message).filter(Message.conversation_id == cid).delete()
        closer.query(Conversation).filter(Conversation.id == cid).delete()
        closer.commit()
    except Exception:
        closer.rollback()
    finally:
        if db is None:
            closer.close()


def _run_chat(db, message="how many semesters in BCA", generate="BCA is a three-year degree. It has six semesters."):
    """Drain run_chat with mocked retrieval + generation; return (events, observed)."""
    observed = {}

    async def _fake_stream(question, context):
        # Invariant 1: at generation time the request session must already be
        # released, and the engine must hold NO checked-out connection.
        observed["closed_at_generation"] = db.close_count >= 1
        observed["checkedout_at_generation"] = engine.pool.checkedout()
        yield generate

    orig_retrieve = chat_service.retrieve
    orig_stream = chat_service.stream_answer_async
    try:
        chat_service.retrieve = lambda *a, **k: [_RETRIEVED_CHUNK]
        chat_service.stream_answer_async = _fake_stream
        events = []

        async def _drain():
            async for ev in chat_service.run_chat(db, "test-user", message, None):
                events.append(ev)

        asyncio.run(_drain())
        return events, observed
    finally:
        chat_service.retrieve = orig_retrieve
        chat_service.stream_answer_async = orig_stream


def test_run_chat_releases_request_session_before_generation():
    """The request session is closed before the LLM runs and no pooled
    connection is checked out during the generation wait."""
    create_all()
    factory = _tracking_factory()
    db = factory()
    events, observed = _run_chat(db)
    try:
        assert db.close_count >= 1, "run_chat must close the request session"
        assert observed.get("closed_at_generation") is True, (
            "request session must be closed BEFORE generation starts"
        )
        assert observed.get("checkedout_at_generation") == 0, (
            "no DB connection may be checked out during the LLM wait"
        )
        tokens = [e["text"] for e in events if e.get("type") == "token"]
        assert tokens and tokens[0] == "BCA is a three-year degree. It has six semesters."
        done = [e for e in events if e.get("type") == "done"]
        assert len(done) == 1 and done[0].get("chat_id")
        _cleanup_conversation(done[0]["chat_id"])
    finally:
        db.close()


def test_run_chat_persists_assistant_turn_in_fresh_session():
    """The assistant turn is written by a separate short-lived session, never
    the (already closed) request session, and the row really exists."""
    create_all()
    factory = _tracking_factory()

    fresh_sessions_used = []
    orig_session_local = chat_service.SessionLocal

    def _recording_session_local():
        s = orig_session_local()
        fresh_sessions_used.append(s)
        return s

    chat_service.SessionLocal = _recording_session_local
    try:
        db = factory()
        events, _ = _run_chat(db)
        done = [e for e in events if e.get("type") == "done"]
        assert len(done) == 1, "done event required"
        conv_id = done[0]["chat_id"]

        assert db.close_count >= 1, "request session must be closed"
        assert len(fresh_sessions_used) >= 1, "assistant persistence needs a fresh session"
        assert all(s is not db for s in fresh_sessions_used), (
            "persistence must never reuse the closed request session"
        )

        # Real rows: fresh re-read proves the write committed independently.
        verifier = orig_session_local()
        try:
            from app.models import Conversation, Message

            conv = verifier.get(Conversation, uuid.UUID(conv_id))
            assert conv is not None
            msgs = (
                verifier.query(Message)
                .filter(Message.conversation_id == conv.id)
                .order_by(Message.created_at)
                .all()
            )
            roles = [m.role for m in msgs]
            assert roles == ["user", "assistant"], f"got roles={roles}"
            assert msgs[-1].content == "BCA is a three-year degree. It has six semesters."
            assert conv.title == "how many semesters in BCA"
        finally:
            verifier.close()
        _cleanup_conversation(conv_id)
    finally:
        chat_service.SessionLocal = orig_session_local
        db.close()


def test_run_chat_no_db_write_after_session_close_when_no_query_terminates():
    """A refused-LLM (no evidence) turn still persists the fallback via the
    fresh session and the stream completes with the standard contract."""
    create_all()
    factory = _tracking_factory()

    fresh_sessions_used = []
    orig_session_local = chat_service.SessionLocal

    def _recording_session_local():
        s = orig_session_local()
        fresh_sessions_used.append(s)
        return s

    chat_service.SessionLocal = _recording_session_local
    orig_retrieve = chat_service.retrieve
    try:
        chat_service.retrieve = lambda *a, **k: []
        db = factory()
        events = []

        async def _drain():
            async for ev in chat_service.run_chat(db, "test-user", "thanks for your help", None):
                events.append(ev)

        asyncio.run(_drain())
        done = [e for e in events if e.get("type") == "done"]
        assert len(done) == 1
        assert db.close_count >= 1
        assert len(fresh_sessions_used) >= 1
        _cleanup_conversation(done[0]["chat_id"])
    finally:
        chat_service.retrieve = orig_retrieve
        chat_service.SessionLocal = orig_session_local
        db.close()


def test_run_chat_detached_conversation_id_used_only_for_citations_and_done():
    """After the close, only loaded primitive attributes (conv.id) are used —
    a detached lazy-load would raise DetachedInstanceError, so the done event
    proves the exit path is safe."""
    create_all()
    factory = _tracking_factory()
    db = factory()
    events, _ = _run_chat(db)
    try:
        done = [e for e in events if e.get("type") == "done"]
        assert len(done) == 1
        assert done[0]["cited_chunks"], "citations must be preserved"
        _cleanup_conversation(done[0]["chat_id"])
    finally:
        db.close()


def test_ask_route_releases_di_session_before_orchestrator_runs():
    """The full /api/chat/ask route must hand the orchestrator a CLOSED request
    session. The request-scoped DI session is only needed for the short
    pre-stream reads (auth + student cookie resolve); once the Admission
    Controller / LLM phase begins the stream holds NO pool connection. A
    queued or token-wait stream parked an open session in the old code and
    exhausted the aggregate pool under load (pg_conns ~31 -> 5xx)."""
    import asyncio
    from unittest import mock

    from fastapi.testclient import TestClient

    from app.auth.security import hash_password
    from app.main import app
    from app.models import User
    from app.utils import rate_limit as _rl

    create_all()
    with TestClient(app) as client:
        username = f"__p3c51_{uuid.uuid4().hex[:6]}"
        db = SessionLocal()
        try:
            db.add(User(
                id=uuid.uuid4(),
                username=username,
                email=f"{username}@t.local",
                hashed_password=hash_password("secret123"),
                role="student",
                is_active=True,
            ))
            db.commit()
        finally:
            db.close()

        try:
            _rl._HITS.clear()
            r = client.post(
                "/api/auth/login",
                data={"username": username, "password": "secret123"},
            )
            assert r.status_code == 200, r.text
            token = r.json()["access_token"]
        finally:
            _rl._HITS.clear()
        headers = {"Authorization": f"Bearer {token}"}

        import app.chat.routes as chat_routes

        observed = {}

        async def fake_process(routed_db, uid, msg, cid, **kwargs):
            # Invariant: by the time the orchestrator runs, the request DI
            # session must be closed and NOT hold a pooled connection.
            observed["entry_checkedout"] = engine.pool.checkedout()
            observed["entry_in_tx"] = routed_db.in_transaction()
            yield {"type": "token", "text": "hello"}
            await asyncio.sleep(0.4)
            observed["mid_checkedout"] = engine.pool.checkedout()
            observed["mid_in_tx"] = routed_db.in_transaction()
            yield {"type": "done", "chat_id": "probe_route", "cited_chunks": []}

        orig_process = chat_routes.process
        chat_routes.process = fake_process
        try:
            with client.stream(
                "POST",
                "/api/chat/ask",
                json={"message": "hi", "chat_id": "probe_route", "stream": True},
                headers=headers,
            ) as resp:
                lines = [ln for ln in resp.iter_lines()]
            assert resp.status_code == 200, resp.text
            assert any("hello" in ln for ln in lines), "token must be streamed"
        finally:
            chat_routes.process = orig_process

        assert observed.get("entry_checkedout") == 0, (
            "request session connection must be returned to the pool BEFORE "
            f"the orchestrator runs (observed checkedout={observed.get('entry_checkedout')})"
        )
        assert observed.get("entry_in_tx") is False, (
            "request session must be closed (no active transaction) at orchestrator entry "
        )
        assert observed.get("mid_checkedout") == 0, (
            "no connection may be checked out during the stream tail"
        )
        assert observed.get("mid_in_tx") is False, (
            "request session must stay closed across the whole stream"
        )

        # Cleanup the seeded login user.
        db = SessionLocal()
        try:
            db.query(User).filter(User.username == username).delete()
            db.commit()
        except Exception:
            db.rollback()
        finally:
            db.close()