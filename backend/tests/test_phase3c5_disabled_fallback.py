"""
backend/tests/test_phase3c5_disabled_fallback.py

Phase 3C-5 unit tests that must hold with the Redis layer DISABLED (the
conftest default REDIS_URL=""): every distributed primitive degrades to the
original single-process behavior, and the PostgreSQL aggregate pool budget is
derived correctly for any worker count (1/2/3/4/10).

Run:  pytest tests/test_phase3c5_disabled_fallback.py -v
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

from app.utils import redis_client  # noqa: E402


def test_redis_disabled_by_default():
    assert redis_client.runtime.enabled() is False


def test_db_pool_budget_scales_with_workers():
    """Per-worker pool = aggregate 30 budget // workers, capped so N workers can
    never exceed the server's max_connections (100)."""
    import app.database as db

    worker_budget = {
        1: (10, 20),    # identical to pre-3C-5 sizing (30 total)
        2: (5, 10),     # 15 per worker x 2 = 30
        3: (3, 7),      # 10 per worker x 3 = 30
        4: (2, 5),      # 7 per worker x 4 = 28
        10: (1, 2),     # 3 per worker x 10 = 30
    }
    for workers, (expect_size, expect_over) in worker_budget.items():
        ns = type(
            "SettingsStub",
            (),
            {
                "DATABASE_URL": "postgresql+psycopg://u:p@127.0.0.1:63999/db",
                "DB_ECHO": False,
                "UVICORN_WORKERS": workers,
                "DB_POOL_SIZE": 10,
                "DB_MAX_OVERFLOW": 20,
                "DB_MAX_AGGREGATE_POOL": 30,
                "DB_POOL_TIMEOUT": 5,
                "DB_POOL_RECYCLE": 600,
            },
        )()
        original_settings = db.settings
        db.settings = ns
        try:
            e = db._make_engine()
            assert e.pool.size() == expect_size, f"workers={workers} size"
            assert e.pool._max_overflow == expect_over, f"workers={workers} overflow"
            e.dispose()
        finally:
            db.settings = original_settings


def test_sqlite_pooling_untouched():
    """Default test DATABASE_URL is SQLite -> no Redis/pool scaling involved."""
    import app.database as db

    assert db.engine is not None


def test_gate_local_when_disabled():
    from app.llm.gate import shared_llm_gate

    gate = shared_llm_gate
    # Distributed path is off (Redis disabled) -> local counter used.
    acquired = gate.acquire_sync(timeout=0.001)
    assert acquired
    try:
        assert gate.held >= 1
    finally:
        gate.release()
    assert gate.held == 0


def test_response_cache_local_when_disabled():
    from app.request_manager.response_cache import response_cache

    response_cache.invalidate_all()
    response_cache.set_generic("hello", ttl=60, q="hi", action="greet")
    hit, value = response_cache.get_generic(q="hi", action="greet")
    assert hit and value == "hello"
    assert response_cache.invalidate_all() >= 1
    hit2, _ = response_cache.get_generic(q="hi", action="greet")
    assert not hit2


def test_token_bucket_local_when_disabled():
    from app.request_manager.token_bucket import token_bucket

    token_bucket.reset("p3c5-fallback-user")
    assert token_bucket.consume("p3c5-fallback-user", cost=1)
    assert not token_bucket.consume("p3c5-fallback-user", cost=999)  # way over budget
    token_bucket.reset("p3c5-fallback-user")


def test_ttl_cache_local_when_disabled():
    from app.orchestrator.cache import get_cache

    async def run():
        cache = get_cache()
        await cache.clear_all()
        await cache.set("intent", "k", {"i": 1}, ttl=60)
        assert await cache.get("intent", "k") == {"i": 1}
        await cache.delete("intent", "k")
        assert await cache.get("intent", "k") is None

    import asyncio

    asyncio.run(run())


def test_available_now_false_when_disabled():
    # No Redis configured -> consumers must not attempt shared ops.
    assert redis_client.runtime.available_now() is False
    assert redis_client.runtime.health_ok() is False