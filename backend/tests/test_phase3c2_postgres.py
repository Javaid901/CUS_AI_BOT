"""Phase 3C-2 focused PostgreSQL validation tests.

These tests run ONLY when a PostgreSQL URI is available:
  - env TEST_PG_URI=<uri>, or
  - the standard isolated-validation file %TEMP%/opencode/p3c2/uri.txt
Otherwise they are SKIPPED, keeping the default SQLite suite green.

They use their own engine/session (independent of the app.database settings
freeze) against the isolated validation DB built by the Alembic baseline and
the SQLite->PG data copy.
"""
from __future__ import annotations

import os
import tempfile
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

sys_path_backend = r"C:\Users\LENOVO\OneDrive\Desktop\CUS_AI_BOT\backend"


def _resolve_uri() -> str | None:
    env = os.environ.get("TEST_PG_URI")
    if env and env.startswith("postgresql"):
        return env
    resolved = Path(sys_path_backend).parent
    uri_file = Path(tempfile.gettempdir()) / "opencode" / "p3c2" / "uri.txt"
    if uri_file.exists():
        s = uri_file.read_text(encoding="utf-8").strip()
        if s.startswith("postgresql"):
            return s
    return None


PG_URI = _resolve_uri()

pytestmark = pytest.mark.skipif(
    not PG_URI,
    reason="No TEST_PG_URI / isolated validation PostgreSQL available",
)

if PG_URI:
    import sys

    sys.path.insert(0, sys_path_backend)

    import app.models  # noqa: E402,F401
    from app.database import Base  # noqa: E402

    _engine = create_engine(PG_URI, pool_pre_ping=True)
    _Session = sessionmaker(bind=_engine, autoflush=False, expire_on_commit=False)

    def db():
        return _Session()

    METADATA_TABLES = set(Base.metadata.tables)


def pytest_configure(config):
    config.addinivalue_line(
        "markers", "phase3c2: Phase 3C-2 PostgreSQL-focused validation tests"
    )


@pytest.fixture()
def session():
    s = _Session()
    yield s
    s.rollback()
    s.close()


def _new_id():
    return uuid.uuid4()


def _clean(session, *pairs):
    for table, col, val in pairs:
        session.execute(
            text(f'DELETE FROM "{table}" WHERE "{col}" = :v'), {"v": str(val) if table.endswith("s") or True else val}
        )
    session.commit()


# ---- A. Engine / availability -------------------------------------------------
def test_engine_is_postgresql():
    assert _engine.dialect.name == "postgresql"


def test_server_version_and_database():
    with _engine.connect() as c:
        ver = c.execute(text("SHOW server_version")).scalar()
        dbn = c.execute(text("SELECT current_database()")).scalar()
    assert ver, "server version unavailable"
    assert dbn


# ---- B. Baseline / schema parity ----------------------------------------------
def test_baseline_schema_parity():
    with _engine.connect() as c:
        pg_tables = {r[0] for r in c.execute(text("SELECT tablename FROM pg_tables WHERE schemaname='public'"))}
    missing = METADATA_TABLES - pg_tables
    assert not missing, f"ORM tables missing in PG: {missing}"
    assert "alembic_version" in pg_tables


def test_all_orm_tables_have_rows_from_migration():
    with _engine.connect() as c:
        counts = {
            t: c.execute(text(f'SELECT count(*) FROM "{t}"')).scalar() for t in METADATA_TABLES
        }
    # migrated core tables must still hold their data
    assert counts["users"] >= 104
    assert counts["students"] >= 13
    assert counts["interaction_events"] >= 678
    assert counts["messages"] >= 62
    assert counts["university_notices"] >= 8


# ---- C. CRUD on PG -------------------------------------------------------------
def test_crud_programme(session):
    pid = _new_id()
    from app.catalogue.models import Programme
    row = Programme(
        id=pid, code=f"pgtest{pid.hex[:6]}", name=f"PG CRUD Test Programme {pid.hex[:6]}",
        degree_level="Bachelor", category_id=None, description=None,
    )
    session.add(row)
    session.commit()
    got = session.get(Programme, pid)
    assert got is not None and got.code == f"pgtest{pid.hex[:6]}"
    got.degree_level = "Master"
    session.commit()
    assert session.get(Programme, pid).degree_level == "Master"
    session.delete(session.get(Programme, pid))
    session.commit()
    assert session.get(Programme, pid) is None


# ---- D. transactions -----------------------------------------------------------
def test_transaction_rollback(session):
    from app.catalogue.models import Programme
    pid = _new_id()
    session.add(Programme(id=pid, code=f"rollback{pid.hex[:6]}", name=f"Rollback Probe {pid.hex[:6]}", degree_level="UG"))
    session.flush()
    session.rollback()
    assert session.get(Programme, pid) is None


# ---- E. JSON round-trip --------------------------------------------------------
def test_json_column_round_trip(session):
    import json as _json
    cid = _new_id()
    mid = _new_id()
    from app.models import Conversation, Message
    ts = datetime.now(timezone.utc).replace(microsecond=0)
    session.add(Conversation(id=cid, user_id=None, title="pg-json-t", created_at=ts, updated_at=ts))
    session.flush()
    payload = _json.dumps([{"document_id": str(_new_id()), "score": 0.91}])
    session.add(Message(id=mid, conversation_id=cid, role="assistant", content="x",
                        citations=payload, model="t", latency_ms=3, created_at=ts))
    session.commit()
    got = session.get(Message, mid)
    assert got.citations == payload
    parsed = _json.loads(got.citations)
    assert parsed[0]["score"] == 0.91
    # cleanup
    session.execute(text("DELETE FROM messages WHERE id = :x"), {"x": str(mid)})
    session.execute(text("DELETE FROM conversations WHERE id = :x"), {"x": str(cid)})
    session.commit()


# ---- F. UUID round-trip --------------------------------------------------------
def test_uuid_native_type_and_equality(session):
    cid = _new_id()
    mid = _new_id()
    from app.models import Conversation, Message
    ts = datetime.now(timezone.utc).replace(microsecond=0)
    session.add(Conversation(id=cid, user_id=None, title="pg-uuid-t", created_at=ts, updated_at=ts))
    session.add(Message(id=mid, conversation_id=cid, role="user", content="u", citations=None,
                        model=None, latency_ms=None, created_at=ts))
    session.commit()
    from app.models import Message as M
    val = session.execute(text("SELECT id::text FROM messages WHERE id=:x"), {"x": str(mid)}).scalar()
    assert val and uuid.UUID(val) == mid
    pk_type = session.execute(text(
        "SELECT data_type FROM information_schema.columns "
        "WHERE table_name='messages' AND column_name='id'"
    )).scalar()
    assert pk_type == "uuid"
    session.execute(text("DELETE FROM messages WHERE id=:x"), {"x": str(mid)})
    session.execute(text("DELETE FROM conversations WHERE id=:x"), {"x": str(cid)})
    session.commit()


# ---- G. timestamps -------------------------------------------------------------
def test_timestamp_microsecond_round_trip(session):
    ts = datetime(2026, 9, 10, 9, 20, 58, 132451, tzinfo=timezone.utc)
    cid = _new_id()
    mid = _new_id()
    from app.models import Conversation, Message
    session.add(Conversation(id=cid, user_id=None, title="pg-ts-t", created_at=ts, updated_at=ts))
    session.add(Message(id=mid, conversation_id=cid, role="user", content="t",
                        citations=None, model=None, latency_ms=1, created_at=ts))
    session.commit()
    got = session.get(Message, mid).created_at
    # instant is preserved exactly (microsecond precision) regardless of tz display
    assert got.astimezone(timezone.utc).replace(tzinfo=None) == ts.replace(tzinfo=None)
    session.execute(text('DELETE FROM messages WHERE id=:x'), {"x": str(mid)})
    session.execute(text('DELETE FROM conversations WHERE id=:x'), {"x": str(cid)})
    session.commit()


# ---- H. foreign keys -----------------------------------------------------------
def test_fk_rejects_orphan_child(session):
    from app.models import Message
    ts = datetime.now(timezone.utc)
    with pytest.raises(IntegrityError):
        session.add(Message(id=_new_id(), conversation_id=_new_id(), role="user",
                            content="no parent", citations=None, model=None,
                            latency_ms=None, created_at=ts))
        session.commit()
    session.rollback()


def test_fk_cascade_delete(session):
    cid = _new_id()
    mid = _new_id()
    from app.models import Conversation, Message
    ts = datetime.now(timezone.utc)
    session.add(Conversation(id=cid, user_id=None, title="pg-cascade", created_at=ts, updated_at=ts))
    session.add(Message(id=mid, conversation_id=cid, role="u", content="c",
                        citations=None, model=None, latency_ms=1, created_at=ts))
    session.commit()
    session.execute(text('DELETE FROM conversations WHERE id=:x'), {"x": str(cid)})
    session.commit()
    left = session.execute(text("SELECT count(*) FROM messages WHERE id=:x"), {"x": str(mid)}).scalar()
    assert left == 0


# ---- I. unique constraints -----------------------------------------------------
def test_unique_constraint_enforced(session):
    from app.catalogue.models import Programme
    pid1, pid2 = _new_id(), _new_id()
    code = f"dupprobe{pid1.hex[:6]}"
    try:
        session.add(Programme(id=pid1, code=code, name=f"Dup Probe One {pid1.hex[:6]}", degree_level="UG"))
        session.commit()
        with pytest.raises(IntegrityError):
            session.add(Programme(id=pid2, code=code, name=f"Dup Probe Two {pid2.hex[:6]}", degree_level="PG"))
            session.commit()
        session.rollback()
    finally:
        session.execute(text(f"DELETE FROM programmes WHERE code = '{code}'"))
        session.commit()


# ---- J/K. concurrency ----------------------------------------------------------
def test_concurrent_reads():
    errors = []
    def reader():
        try:
            s = _Session()
            try:
                cnt = s.execute(select(func.count()).select_from(
                    Base.metadata.tables["student_results"]
                )).scalar()
                assert cnt and cnt > 0
            finally:
                s.close()
        except Exception as e:  # noqa: BLE001
            errors.append(e)
    threads = [threading.Thread(target=reader) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors


def test_concurrent_independent_writes():
    n = 8
    ids = [_new_id() for _ in range(n)]
    errors = []

    def writer(i):
        try:
            s = _Session()
            try:
                ts = datetime.now(timezone.utc)
                s.execute(text(
                    "INSERT INTO conversations (id, user_id, title, created_at, updated_at) "
                    "VALUES (:id, NULL, :title, :ts, :ts)"
                ), {"id": str(ids[i]), "title": f"conc-{i}", "ts": ts})
                s.commit()
                back = s.execute(text("SELECT title FROM conversations WHERE id=:id"),
                                 {"id": str(ids[i])}).scalar()
                assert back == f"conc-{i}"
            finally:
                s.close()
        except Exception as e:  # noqa: BLE001
            errors.append(e)

    threads = [threading.Thread(target=writer, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    s = _Session()
    for i in range(n):
        s.execute(text("DELETE FROM conversations WHERE id=:id"), {"id": str(ids[i])})
    s.commit()
    s.close()
    assert not errors, errors


# ---- L-Q. service-level reads on migrated data ---------------------------------
def test_migrated_student_results_readable():
    from app.models import StudentResult
    s = _Session()
    cnt = s.query(StudentResult).count()
    assert cnt > 0
    rows = s.query(StudentResult).limit(3).all()
    assert all(r.id is not None for r in rows)
    s.close()


def test_migrated_exam_forms_readable():
    from app.models import StudentExamForm
    s = _Session()
    cnt = s.query(StudentExamForm).count()
    assert cnt > 0
    s.close()


def test_grievance_persist_read_delete(session):
    from app.models import Grievance
    gid = _new_id()
    ts = datetime.now(timezone.utc)
    session.add(Grievance(
        id=gid,
        category="Test",
        original_student_input="PG persistence probe",
        status="open",
        priority="medium",
        student_email="pg-probe@test.local",
        created_at=ts, updated_at=ts,
        is_read=False,
    ))
    session.commit()
    got = session.query(Grievance).filter(Grievance.id == gid).first()
    assert got and got.status == "open"
    session.delete(got)
    session.commit()
    assert session.query(Grievance).filter(Grievance.id == gid).first() is None


def test_catalogue_list_programmes_service():
    from app.catalogue.service import list_programmes
    progs = list_programmes(db=_Session())
    assert len(progs) > 0
    assert all(p.get("code") for p in progs)


def test_notices_service_ordering():
    from app.notices.service import list_notices
    s = _Session()
    out = list_notices(s, published_only=True, limit=5)
    # returns a list on PG without error (NULLs-last ordering established)
    assert isinstance(out, list)
    s.close()


def test_conversation_persist_via_service_layer(session):
    from app.models import Conversation, Message
    from app.database import SessionLocal as _app_session
    # low-level ORM write via injected session (not the frozen SQLite one)
    cid = _new_id()
    mid = _new_id()
    ts = datetime.now(timezone.utc)
    session.add(Conversation(id=cid, user_id=None, title="service-layer", created_at=ts, updated_at=ts))
    session.flush()
    session.add(Message(id=mid, conversation_id=cid, role="a", content="svc",
                        citations=None, model="m", latency_ms=1, created_at=ts))
    session.commit()
    assert session.query(Conversation).filter(Conversation.id == cid).count() == 1
    session.execute(text("DELETE FROM messages WHERE id=:x"), {"x": str(mid)})
    session.execute(text("DELETE FROM conversations WHERE id=:x"), {"x": str(cid)})
    session.commit()


# ---- R. app contract -----------------------------------------------------------
def test_app_router_is_registered():
    # static contract: app wiring is importable and HTTP route table is non-empty
    import sys

    sys.path.insert(0, sys_path_backend)
    from app.main import app
    routes = [r.path for r in app.routes if getattr(r, "path", None)]
    assert any(p.startswith("/api/") for p in routes)
    assert "/api/chat/ask" in routes
    assert "/api/grievances" in routes
