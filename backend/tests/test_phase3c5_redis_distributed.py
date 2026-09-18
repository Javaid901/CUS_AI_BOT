"""
backend/tests/test_phase3c5_redis_distributed.py

Phase 3C-5 Redis integration tests (skipped unless TEST_REDIS_URL is set).

The main suite is forced Redis-free by conftest.py (REDIS_URL=""). These tests
opt in via the TEST_REDIS_URL env var (e.g. redis://127.0.0.1:6399/5).

Windows caveat: redis.asyncio connections live on the event loop that created
them, and this codebase runs on CPython/Windows (Proactor loop). All shared-state
checks therefore run sequentially inside ONE event loop (like the standalone
smoke harness), and the runtime is returned to the disabled state afterwards —
the deterministic suite can never be affected.

Run:
    $env:TEST_REDIS_URL='redis://127.0.0.1:6399/5'; python -m pytest tests/test_phase3c5_redis_distributed.py -v
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import pytest  # noqa: E402

redis_client = pytest.importorskip("app.utils.redis_client", reason="app package unimportable")

TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "").strip()

pytestmark = pytest.mark.skipif(
    not TEST_REDIS_URL,
    reason="TEST_REDIS_URL not set (Redis integration tests are opt-in)",
)


async def _clean(*keys: str) -> None:
    client = redis_client.runtime._async()
    await client.delete(*keys)


async def _shared_suite() -> list[str]:
    ok: list[str] = []
    rc = redis_client
    runtime = rc.runtime

    runtime.configure(TEST_REDIS_URL)
    assert runtime.enabled()
    assert await runtime.aping(), "Redis under TEST_REDIS_URL is unreachable"
    ok.append("aping")

    # ── rate limiting ---------------------------------------------------
    key = rc._k("rl", "chat:it")
    await _clean(key)
    a1 = await rc.sliding_window_consume("chat:it", 2, 60.0, seq=1)
    a2 = await rc.sliding_window_consume("chat:it", 2, 60.0, seq=2)
    rej = await rc.sliding_window_consume("chat:it", 2, 60.0, seq=3)
    assert a1[0] == 1 and a2[0] == 1, "sliding window allow"
    assert rej[0] == 0, "sliding window reject over shared limit"
    ok.append("sliding_window")

    tb_key = rc._k("tb", "it")
    await _clean(tb_key)
    assert await rc.token_bucket_consume("it", 2, 0.1, cost=1) is True
    assert await rc.token_bucket_consume("it", 2, 0.1, cost=1) is True
    assert await rc.token_bucket_consume("it", 2, 0.1, cost=1) is False
    await rc.token_bucket_refund("it", 2, 0.1, cost=1)
    ok.append("token_bucket")

    # ── LLM gate (fail-closed, shared ceiling) ---------------------------
    gate_key = rc._k("gate", "llm")
    chat = "p3c5-it-chat"
    await _clean(gate_key, rc._k("nav", chat), rc._k("state", chat))
    assert await rc.llm_gate_claim(max_concurrent=2) is True
    assert await rc.llm_gate_claim(max_concurrent=2) is True
    assert await rc.llm_gate_claim(max_concurrent=2) is False
    assert rc.llm_gate_held_sync() == 2
    await rc.llm_gate_release()
    await rc.llm_gate_release()
    assert await rc.llm_gate_claim(max_concurrent=2) is True
    await rc.llm_gate_release()
    ok.append("llm_gate")

    # ── navigation state -------------------------------------------------
    from app.utils.redis_client import nav_get_sync, nav_replace_sync, nav_clear_sync

    nav_replace_sync(chat, ["overview", "admissions"])
    assert nav_get_sync(chat) == ["overview", "admissions"]
    assert await rc.nav_advance(chat, "admissions")
    nav_clear_sync(chat)
    assert nav_get_sync(chat) == []
    ok.append("nav")

    # ── conversation state + version adoption ----------------------------
    from app.orchestrator.state import get_state, set_state

    st = await get_state(chat)  # fresh local working copy
    st.last_intent = "fee_query"
    await set_state(chat, st)
    got = await get_state(chat)
    assert got.last_intent == "fee_query"
    data = await rc.state_get(chat)
    data["_ver"] = data.get("_ver", 0) + 5
    data["last_intent"] = "newer_from_other_worker"
    await rc.state_set(chat, data, ttl=300)
    adopted = await get_state(chat)
    assert adopted.last_intent == "newer_from_other_worker"
    assert adopted.chat_id == chat
    ok.append("state_version_adoption")

    # ── caches ------------------------------------------------------------
    from app.request_manager.response_cache import response_cache
    from app.orchestrator.cache import get_cache

    rkey = response_cache._make_key(q="what are fees?", action="app_fee_structured")
    response_cache.set_generic("the fee is 500", ttl=60, q="what are fees?", action="app_fee_structured")
    assert rc.cache_get_sync("resp", rkey) == "the fee is 500"
    hit, value = response_cache.get_generic(q="what are fees?", action="app_fee_structured")
    assert hit and value == "the fee is 500"
    assert rc.cache_purge_sync("resp") >= 1
    ok.append("response_cache_write_through")

    cache = get_cache()
    await cache.set("intent", "q1", {"intent": "greeting"}, ttl=60)
    assert await cache.get("intent", "q1") == {"intent": "greeting"}
    assert await rc.cache_get("orrch", "intent:q1") == {"intent": "greeting"}
    await cache.clear_namespace("intent")
    assert await cache.get("intent", "q1") is None
    ok.append("ttl_cache")

    await rc.cache_delete("orrch", "llm:broken")
    await rc.runtime._async().set(rc._k("orrch", "llm:broken"), "not-json-{", ex=60)
    assert await cache.get("llm", "broken") is None  # malformed shared -> miss
    ok.append("ttl_cache_malformed_guard")

    # ── coalescing + distributed locks -----------------------------------
    ckey = "p3c5-coalesce-it"
    claim = await rc.coalesce_claim(ckey)
    assert claim is not None
    assert await rc.coalesce_claim(ckey) is None  # held by owner
    waiter = asyncio.create_task(rc.coalesce_wait(ckey, 1.0))
    await asyncio.sleep(0.15)
    await rc.coalesce_publish(ckey, "answer-text")
    assert await waiter == "answer-text"
    ok.append("coalesce")

    lock = await rc.acquire_lock("sync:p3c5", ttl=60)
    assert lock is not None
    assert await rc.acquire_lock("sync:p3c5", ttl=60) is None
    await rc.release_lock("sync:p3c5", lock)
    mg = await rc.maint_try_acquire("analytics:aggregation")
    assert mg is not None
    assert await rc.maint_try_acquire("analytics:aggregation") is None
    await rc.maint_release("analytics:aggregation", mg)
    ok.append("locks_maint")

    # teardown back to disabled
    runtime.close()
    runtime.configure("")
    assert not runtime.enabled()
    ok.append("reset_disabled")

    return ok


def test_shared_state_integration():
    """All shared-state primitives, exercised sequentially in one event loop."""
    checks = asyncio.run(_shared_suite())
    print("\n  P3C-5 shared-state checks: " + ", ".join(checks))