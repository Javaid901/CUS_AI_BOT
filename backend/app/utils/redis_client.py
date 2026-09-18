"""Phase 3C-5 distributed state: bounded Redis clients + atomic primitives.

Availability model
------------------
* REDIS_URL empty => Redis layer DISABLED; every primitive returns None/falsy and
  the caller falls back to its process-local implementation. Single-process
  behavior is byte-for-byte unchanged.
* REDIS_URL set but unreachable => bounded timeout, single attempt, no reconnect
  loop; callers degrade locally (fail-safe everywhere except the LLM gate, which
  is fail-closed). Nothing raises into request handlers.
* One bounded asyncio pool + one bounded sync pool (grievance gate / website sync
  threads). No per-request connections. Health uses a cached ping (short negative
  TTL) to avoid ping-storms while Redis is down.

Keys all live under settings.REDIS_KEY_PREFIX ("cus") and every key carries a TTL.
  cus:rl:{bucket}:{client}   sliding-window sorted set
  cus:tb:{user}              token-bucket hash {t, s}
  cus:gate:llm               global LLM concurrency counter (lease TTL)
  cus:lock:{name}            distributed mutex (owner token, NX PX)
  cus:nav:{chat_id}          navigation path LIST
  cus:state:{chat_id}        conversation state JSON (TTL)
  cus:cache:{host}:{key}     shared response/orchestrator cache JSON
  cus:coalesce:claim:{key}   in-flight duplicate-request claim
  cus:coalesce:ans:{key}     replayed answer
  cus:maint:{name}           single-owner background maintenance guard
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from typing import Any

import redis
import redis.asyncio as aioredis

from app.config import settings

log = logging.getLogger("cus_ai")

# ---------------------------------------------------------------------------
# Lua scripts
# ---------------------------------------------------------------------------

_SC_SLIDING_WINDOW = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local k = KEYS[1]
local limit_n = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
if limit_n <= 0 then return {1, 0, 0} end
local member = ARGV[3]
local stale = redis.call('ZRANGEBYSCORE', k, '-inf', now - window)
if #stale > 0 then redis.call('ZREM', k, unpack(stale)) end
local count = redis.call('ZCARD', k)
if count >= limit_n then
  local oldest = redis.call('ZRANGE', k, 0, 0, 'WITHSCORES')
  local retry = 1
  if #oldest >= 2 then
    retry = math.ceil((tonumber(oldest[2]) + window) - now)
    if retry < 1 then retry = 1 end
  end
  return {0, retry, count}
end
redis.call('ZADD', k, now, member)
redis.call('PEXPIRE', k, window * 1000 + 1000)
return {1, 0, count}
"""

_SC_TOKEN_BUCKET = """
local t = redis.call('TIME')
local now = tonumber(t[1]) + tonumber(t[2]) / 1000000
local k = KEYS[1]
local max_tokens = tonumber(ARGV[1])
local rate = tonumber(ARGV[2])
local cost = tonumber(ARGV[3])
local ttl_ms = tonumber(ARGV[4])
local op = ARGV[5]
local f = redis.call('HMGET', k, 't', 's')
local tokens = tonumber(f[1]) or max_tokens
local ts = tonumber(f[2]) or now
local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end
tokens = math.min(max_tokens, tokens + elapsed * rate)
local allowed = 0
if op == 'consume' then
  if tokens >= cost then tokens = tokens - cost; allowed = 1 end
elseif op == 'refund' then
  tokens = math.min(max_tokens, tokens + cost); allowed = 1
end
redis.call('HMSET', k, 't', tokens, 's', now)
redis.call('PEXPIRE', k, ttl_ms)
return {allowed, tokens}
"""

_SC_GATE_CLAIM = """
local k = KEYS[1]
local maxn = tonumber(ARGV[1])
local ttl_ms = tonumber(ARGV[2])
local c = redis.call('INCR', k)
if c == 1 then redis.call('PEXPIRE', k, ttl_ms) end
if c <= maxn then
  -- refresh lease on every successful claim so a long generation (<=180s)
  -- never outlives its 300s slot TTL
  redis.call('PEXPIRE', k, ttl_ms)
  return 1
end
redis.call('DECR', k)
return 0
"""

_SC_GATE_RELEASE = """
local c = tonumber(redis.call('GET', KEYS[1]))
if c and c > 0 then
  local v = redis.call('DECR', KEYS[1])
  if v <= 0 then redis.call('DEL', KEYS[1]) end
end
return 1
"""

_SC_LOCK_RELEASE = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
  return redis.call('DEL', KEYS[1])
end
return 0
"""

_SC_NAV_PUSH = """
local k = KEYS[1]
local ttl_ms = tonumber(ARGV[1])
for i = 2, #ARGV do redis.call('RPUSH', k, ARGV[i]) end
redis.call('PEXPIRE', k, ttl_ms)
return redis.call('LLEN', k)
"""

_SC_NAV_REPLACE = """
local k = KEYS[1]
local ttl_ms = tonumber(ARGV[1])
redis.call('DEL', k)
for i = 2, #ARGV do redis.call('RPUSH', k, ARGV[i]) end
if #ARGV > 1 then redis.call('PEXPIRE', k, ttl_ms) end
return redis.call('LLEN', k)
"""

_SC_NAV_POP = """
local k = KEYS[1]
local ttl_ms = tonumber(ARGV[1])
local v = redis.call('LPOP', k)
if v then redis.call('PEXPIRE', k, ttl_ms) end
return v or redis.empty_string
"""

_SC_CACHE_JSON_SET = """
local k = KEYS[1]
local ttl_ms = tonumber(ARGV[1])
if #ARGV == 1 then redis.call('DEL', k); return 1 end
redis.call('SET', k, ARGV[2], 'PX', ttl_ms)
return 1
"""

# ---------------------------------------------------------------------------
# Runtime
# ---------------------------------------------------------------------------


class RedisRuntime:
    """Lazy dual-client runtime with bounded pools; never raises to callers."""

    def __init__(self, url: str = "") -> None:
        self._lock = threading.Lock()
        self._url = url
        self._async_client: aioredis.Redis | None = None
        self._sync_client: redis.Redis | None = None
        self._avail: bool | None = None
        self._avail_at: float = 0.0

    # -- config -----------------------------------------------------------
    def enabled(self) -> bool:
        return bool(self._url)

    def configure(self, url: str) -> None:
        """Rebind the runtime (used by tests); closes previous clients."""
        with self._lock:
            old_a = self._async_client
            old_s = self._sync_client
            self._async_client = None
            self._sync_client = None
            self._avail = None
            self._avail_at = 0.0
            if url and self._url:
                log.info("redis: rebinding client to %s", _safe_url(url))
            self._url = url
        if old_s:
            try:
                old_s.close()
            except Exception:  # noqa: BLE001
                pass
        if old_a is not None:
            _close_async(old_a)

    # -- clients -----------------------------------------------------------
    def _async(self) -> aioredis.Redis | None:
        if not self.enabled():
            return None
        with self._lock:
            if self._async_client is None:
                try:
                    self._async_client = aioredis.from_url(
                        self._url,
                        encoding="utf-8",
                        decode_responses=True,
                        protocol=2,  # RESP2: the bundled/portable Redis (5.x) has no HELLO/RESP3
                        max_connections=max(4, int(settings.REDIS_POOL_MAX)),
                        socket_connect_timeout=settings.REDIS_CONNECT_TIMEOUT,
                        socket_timeout=settings.REDIS_SOCKET_TIMEOUT,
                        socket_keepalive=True,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("redis: async client init failed: %s", exc)
                    self._url = ""
                    return None
            return self._async_client

    def _sync(self) -> redis.Redis | None:
        if not self.enabled():
            return None
        with self._lock:
            if self._sync_client is None:
                try:
                    self._sync_client = redis.Redis.from_url(
                        self._url,
                        encoding="utf-8",
                        decode_responses=True,
                        protocol=2,  # RESP2: the bundled/portable Redis (5.x) has no HELLO/RESP3
                        max_connections=max(4, int(settings.REDIS_POOL_MAX)),
                        socket_connect_timeout=settings.REDIS_CONNECT_TIMEOUT,
                        socket_timeout=settings.REDIS_SOCKET_TIMEOUT,
                        socket_keepalive=True,
                    )
                except Exception as exc:  # noqa: BLE001
                    log.warning("redis: sync client init failed: %s", exc)
                    self._url = ""
                    return None
            return self._sync_client

    # -- health -----------------------------------------------------------
    def ping(self) -> bool:
        return self.enabled() and self._cached_ping(sync=True)

    def health_ok(self) -> bool:
        """Cached reachability check for sync paths (30s negative cache)."""
        return self.enabled() and self._cached_ping(sync=True)

    def available_now(self) -> bool:
        """Cheap guard for event-loop callers: may we attempt a Redis op NOW?

        Uses the cached availability only (never pings — pinging in-loop could
        block for up to socket_timeout). Unprobed/stale state allows one attempt,
        which on success/failure updates the 30s cache.
        """
        if not self._url:
            return False
        if self._avail is None:
            return True
        if time.monotonic() - self._avail_at > settings.REDIS_HEALTH_INTERVAL:
            return True
        return self._avail

    def _mark_avail(self, ok: bool) -> None:
        self._avail = ok
        self._avail_at = time.monotonic()

    async def aping(self) -> bool:
        if not self.enabled():
            return False
        if self._avail is not None and time.monotonic() - self._avail_at < settings.REDIS_HEALTH_INTERVAL:
            return self._avail
        ok = False
        client = self._async()
        if client is not None:
            try:
                ok = bool(await client.ping())
            except Exception:  # noqa: BLE001
                ok = False
        self._avail = ok
        self._avail_at = time.monotonic()
        return ok

    def _cached_ping(self, sync: bool = False) -> bool:
        if self._avail is not None and time.monotonic() - self._avail_at < settings.REDIS_HEALTH_INTERVAL:
            return self._avail
        ok = False
        client = self._sync() if sync else self._async()
        if client is not None:
            try:
                ok = bool(client.ping())
            except Exception:  # noqa: BLE001
                ok = False
        self._avail = ok
        self._avail_at = time.monotonic()
        return ok

    def close(self) -> None:
        """Best-effort graceful close of both pools."""
        with self._lock:
            s = self._sync_client
            a = self._async_client
            self._sync_client = None
            self._async_client = None
        if s:
            try:
                s.close()
            except Exception:  # noqa: BLE001
                pass
        if a is not None:
            _close_async(a)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _close_async(client: aioredis.Redis) -> None:
    try:
        loop = asyncio.get_running_loop()
        loop.create_task(client.aclose())
    except RuntimeError:
        try:
            asyncio.run(client.aclose())
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass


def _safe_url(url: str) -> str:
    try:
        pre, _, rest = url.partition("://")
        if "@" in rest:
            userinfo, _, host = rest.partition("@")
            user = userinfo.split(":", 1)[0]
            return f"{pre}://{user}:****@{host}"
    except Exception:  # noqa: BLE001
        pass
    return url.split("@")[-1]


def _k(*parts: str) -> str:
    prefix = settings.REDIS_KEY_PREFIX or "cus"
    return ":".join([prefix, *parts])


def _err(name: str, exc) -> None:
    log.debug("redis %s failed: %s", name, type(exc).__name__)


# ---------------------------------------------------------------------------
# Rate limiting (async)
# ---------------------------------------------------------------------------

async def sliding_window_consume(bucket: str, limit: int, window: float, seq: int) -> tuple | None:
    """(allowed:int, retry:int) or None when Redis is unavailable."""
    client = runtime._async()
    if client is None:
        return None
    member = f"{time.time():.6f}-{seq}"
    try:
        allowed, retry, _count = await client.eval(
            _SC_SLIDING_WINDOW, 1, _k("rl", bucket), limit, window, member
        )
        runtime._mark_avail(True)
        return int(allowed), int(retry)
    except Exception as exc:  # noqa: BLE001
        _err("sliding_window", exc)
        runtime._mark_avail(False)
        return None


async def token_bucket_consume(key: str, max_tokens: float, rate: float, cost: int) -> bool | None:
    client = runtime._async()
    if client is None:
        return None
    try:
        allowed, _t = await client.eval(
            _SC_TOKEN_BUCKET, 1, _k("tb", key),
            max_tokens, rate, cost, settings.REDIS_STATE_TTL * 1000, "consume",
        )
        runtime._mark_avail(True)
        return bool(int(allowed))
    except Exception as exc:  # noqa: BLE001
        _err("token_bucket_consume", exc)
        runtime._mark_avail(False)
        return None


async def token_bucket_refund(key: str, max_tokens: float, rate: float, cost: int) -> None:
    client = runtime._async()
    if client is None:
        return
    try:
        await client.eval(
            _SC_TOKEN_BUCKET, 1, _k("tb", key),
            max_tokens, rate, cost, settings.REDIS_STATE_TTL * 1000, "refund",
        )
        runtime._mark_avail(True)
    except Exception as exc:  # noqa: BLE001
        _err("token_bucket_refund", exc)
        runtime._mark_avail(False)


def sliding_window_consume_sync(bucket: str, limit: int, window: float, seq: int) -> tuple | None:
    """Sync variant for blocking contexts (worker threads). None = unavailable."""
    if not runtime.available_now():
        return None
    client = runtime._sync()
    if client is None:
        return None
    member = f"{time.time():.6f}-{seq}"
    try:
        allowed, retry, _count = client.eval(
            _SC_SLIDING_WINDOW, 1, _k("rl", bucket), limit, window, member
        )
        runtime._mark_avail(True)
        return int(allowed), int(retry)
    except Exception as exc:  # noqa: BLE001
        _err("sliding_window(sync)", exc)
        runtime._mark_avail(False)
        return None


def token_bucket_consume_sync(key: str, max_tokens: float, rate: float, cost: int) -> bool | None:
    if not runtime.available_now():
        return None
    client = runtime._sync()
    if client is None:
        return None
    try:
        allowed, _t = client.eval(
            _SC_TOKEN_BUCKET, 1, _k("tb", key),
            max_tokens, rate, cost, settings.REDIS_STATE_TTL * 1000, "consume",
        )
        runtime._mark_avail(True)
        return bool(int(allowed))
    except Exception as exc:  # noqa: BLE001
        _err("token_bucket_consume(sync)", exc)
        runtime._mark_avail(False)
        return None


def token_bucket_refund_sync(key: str, max_tokens: float, rate: float, cost: int) -> None:
    if not runtime.available_now():
        return
    client = runtime._sync()
    if client is None:
        return
    try:
        client.eval(
            _SC_TOKEN_BUCKET, 1, _k("tb", key),
            max_tokens, rate, cost, settings.REDIS_STATE_TTL * 1000, "refund",
        )
        runtime._mark_avail(True)
    except Exception as exc:  # noqa: BLE001
        _err("token_bucket_refund(sync)", exc)
        runtime._mark_avail(False)


# ---------------------------------------------------------------------------
# LLM gate (async + sync)
# ---------------------------------------------------------------------------

async def llm_gate_claim(max_concurrent: int) -> bool | None:
    """Fail-closed: None on Redis failure => caller MUST treat gate as busy."""
    client = runtime._async()
    if client is None:
        return None
    try:
        ok = await client.eval(
            _SC_GATE_CLAIM, 1, _k("gate", "llm"),
            max_concurrent, settings.REDIS_LLM_GATE_TTL * 1000,
        )
        runtime._mark_avail(True)
        return bool(int(ok))
    except Exception as exc:  # noqa: BLE001
        log.warning("redis llm gate claim failed: %s", type(exc).__name__)
        runtime._mark_avail(False)
        return None


async def llm_gate_release() -> None:
    client = runtime._async()
    if client is None:
        return
    try:
        await client.eval(_SC_GATE_RELEASE, 1, _k("gate", "llm"))
    except Exception as exc:  # noqa: BLE001
        _err("llm_gate_release", exc)


def llm_gate_claim_sync(max_concurrent: int) -> bool | None:
    if not runtime.available_now():
        return None
    client = runtime._sync()
    if client is None:
        return None
    try:
        ok = client.eval(_SC_GATE_CLAIM, 1, _k("gate", "llm"), max_concurrent, settings.REDIS_LLM_GATE_TTL * 1000)
        runtime._mark_avail(True)
        return bool(int(ok))
    except Exception as exc:  # noqa: BLE001
        log.warning("redis llm gate claim(sync) failed: %s", type(exc).__name__)
        runtime._mark_avail(False)
        return None


def llm_gate_release_sync() -> None:
    client = runtime._sync()
    if client is None:
        return
    try:
        client.eval(_SC_GATE_RELEASE, 1, _k("gate", "llm"))
    except Exception as exc:  # noqa: BLE001
        _err("llm_gate_release(sync)", exc)


def llm_gate_held_sync() -> int:
    """Number of LLM slots currently in use (0 if Redis unavailable)."""
    client = runtime._sync()
    if client is None:
        return 0
    try:
        val = client.get(_k("gate", "llm"))
        return int(val) if val is not None else 0
    except Exception as exc:  # noqa: BLE001
        _err("llm_gate_held(sync)", exc)
        return 0


# ---------------------------------------------------------------------------
# Navigation state
# ---------------------------------------------------------------------------

async def nav_get(chat_id: str) -> list[str] | None:
    client = runtime._async()
    if client is None:
        return None
    try:
        items = await client.lrange(_k("nav", chat_id), 0, -1)
        return list(items or [])
    except Exception as exc:  # noqa: BLE001
        _err("nav_get", exc)
        return None


async def nav_advance(chat_id: str, *selections: str) -> list[str] | None:
    client = runtime._async()
    if client is None:
        return None
    key = _k("nav", chat_id)
    ttl = settings.REDIS_STATE_TTL * 1000
    try:
        await client.eval(_SC_NAV_PUSH, 1, key, ttl, *[str(s) for s in selections])
        return list(await client.lrange(key, 0, -1) or [])
    except Exception as exc:  # noqa: BLE001
        _err("nav_advance", exc)
        return None


async def nav_pop(chat_id: str) -> str | None:
    client = runtime._async()
    if client is None:
        return None
    key = _k("nav", chat_id)
    ttl = settings.REDIS_STATE_TTL * 1000
    try:
        return await client.eval(_SC_NAV_POP, 1, key, ttl) or None
    except Exception as exc:  # noqa: BLE001
        _err("nav_pop", exc)
        return None


# Sync nav variants — used by the legacy sync intent_router entry points that
# run inside the async engine flow (never on a dedicated worker thread), so the
# Redis round-trip is sub-millisecond on a healthy server. Same availability
# contract: return None / fall back to the caller's local store on failure.
def nav_get_sync(chat_id: str) -> list[str] | None:
    if not runtime.available_now():
        return None
    client = runtime._sync()
    if client is None:
        return None
    try:
        items = client.lrange(_k("nav", chat_id), 0, -1)
        runtime._mark_avail(True)
        return list(items or [])
    except Exception as exc:  # noqa: BLE001
        _err("nav_get(sync)", exc)
        runtime._mark_avail(False)
        return None


def nav_replace_sync(chat_id: str, path: list[str]) -> None:
    if not runtime.available_now():
        return
    client = runtime._sync()
    if client is None:
        return
    key = _k("nav", chat_id)
    ttl = settings.REDIS_STATE_TTL * 1000
    try:
        client.eval(_SC_NAV_REPLACE, 1, key, ttl, *[str(p) for p in path])
        runtime._mark_avail(True)
    except Exception as exc:  # noqa: BLE001
        _err("nav_replace(sync)", exc)
        runtime._mark_avail(False)


def nav_advance_sync(chat_id: str, *selections: str) -> list[str] | None:
    if not runtime.available_now():
        return None
    client = runtime._sync()
    if client is None:
        return None
    key = _k("nav", chat_id)
    ttl = settings.REDIS_STATE_TTL * 1000
    try:
        client.eval(_SC_NAV_PUSH, 1, key, ttl, *[str(s) for s in selections])
        path = list(client.lrange(key, 0, -1) or [])
        runtime._mark_avail(True)
        return path
    except Exception as exc:  # noqa: BLE001
        _err("nav_advance(sync)", exc)
        runtime._mark_avail(False)
        return None


def nav_clear_sync(chat_id: str) -> None:
    if not runtime.available_now():
        return
    client = runtime._sync()
    if client is None:
        return
    try:
        client.delete(_k("nav", chat_id))
        runtime._mark_avail(True)
    except Exception as exc:  # noqa: BLE001
        _err("nav_clear(sync)", exc)
        runtime._mark_avail(False)


# ---------------------------------------------------------------------------
# Conversation state (async)
# ---------------------------------------------------------------------------

async def state_get(chat_id: str) -> dict | None:
    client = runtime._async()
    if client is None:
        return None
    try:
        raw = await client.get(_k("state", chat_id))
        if raw is None:
            return None
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception as exc:  # noqa: BLE001
        _err("state_get", exc)
        return None


async def state_set(chat_id: str, data: dict, ttl: int) -> None:
    client = runtime._async()
    if client is None:
        return
    try:
        payload = json.dumps(data)
        await client.set(_k("state", chat_id), payload, ex=ttl)
    except Exception as exc:  # noqa: BLE001
        _err("state_set", exc)


async def state_delete(chat_id: str) -> None:
    client = runtime._async()
    if client is None:
        return
    try:
        await client.delete(_k("state", chat_id))
    except Exception as exc:  # noqa: BLE001
        _err("state_delete", exc)


# ---------------------------------------------------------------------------
# Distributed cache (JSON)
# ---------------------------------------------------------------------------

def _serializable(value: Any) -> Any:
    """Keep only JSON-safe values; anything else is refused (never stored)."""
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            try:
                out.append(json.loads(json.dumps(_serializable(item))))
            except Exception:  # noqa: BLE001
                return None
        return out
    if isinstance(value, dict):
        out = {}
        for kk, vv in value.items():
            if not isinstance(kk, str):
                return None
            sv = _serializable(vv)
            if sv is None and vv is not None:
                return None
            out[kk] = sv
        return out
    return None


async def cache_get(namespace: str, key: str) -> Any | None:
    client = runtime._async()
    if client is None:
        return None
    try:
        raw = await client.get(_k("cache", namespace, key))
        if raw is None:
            return None
        return json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        _err("cache_get", exc)
        return None


async def cache_set(namespace: str, key: str, value: Any, ttl: int) -> bool:
    client = runtime._async()
    if client is None:
        return False
    safe = _serializable(value)
    if safe is None:
        return False
    ttl = max(1, min(int(ttl), settings.REDIS_CACHE_TTL_CAP))
    try:
        await client.eval(_SC_CACHE_JSON_SET, 1, _k("cache", namespace, key), ttl * 1000, json.dumps(safe))
        return True
    except Exception as exc:  # noqa: BLE001
        _err("cache_set", exc)
        return False


async def cache_delete(namespace: str, key: str) -> None:
    client = runtime._async()
    if client is None:
        return
    try:
        await client.delete(_k("cache", namespace, key))
    except Exception as exc:  # noqa: BLE001
        _err("cache_delete", exc)


def cache_get_sync(namespace: str, key: str) -> Any | None:
    """Sync JSON cache read (for non-async callers). None when unavailable."""
    if not runtime.available_now():
        return None
    client = runtime._sync()
    if client is None:
        return None
    try:
        raw = client.get(_k("cache", namespace, key))
        if raw is None:
            runtime._mark_avail(True)
            return None
        runtime._mark_avail(True)
        return json.loads(raw)
    except Exception as exc:  # noqa: BLE001
        _err("cache_get(sync)", exc)
        runtime._mark_avail(False)
        return None


def cache_set_sync(namespace: str, key: str, value: Any, ttl: int) -> bool:
    if not runtime.available_now():
        return False
    client = runtime._sync()
    if client is None:
        return False
    safe = _serializable(value)
    if safe is None:
        return False
    ttl = max(1, min(int(ttl), settings.REDIS_CACHE_TTL_CAP))
    try:
        client.set(_k("cache", namespace, key), json.dumps(safe), ex=ttl)
        runtime._mark_avail(True)
        return True
    except Exception as exc:  # noqa: BLE001
        _err("cache_set(sync)", exc)
        runtime._mark_avail(False)
        return False


def cache_purge_sync(namespace: str) -> int:
    """Delete every key under a cache namespace (SCAN+DEL, capped). Returns count."""
    if not runtime.available_now():
        return 0
    client = runtime._sync()
    if client is None:
        return 0
    prefix = f"{_k('cache', namespace)}:*"
    deleted = 0
    try:
        for key in client.scan_iter(match=prefix, count=200):
            client.delete(key)
            deleted += 1
        runtime._mark_avail(True)
    except Exception as exc:  # noqa: BLE001
        _err("cache_purge(sync)", exc)
        runtime._mark_avail(False)
    return deleted


# ---------------------------------------------------------------------------
# Distributed locks / coalescing / maintenance guards
# ---------------------------------------------------------------------------

async def acquire_lock(name: str, ttl: int, owner: str | None = None) -> str | None:
    """SET NX PX returning an owner token, or None if held/unavailable."""
    client = runtime._async()
    if client is None:
        return None
    token = owner or _rand_token()
    try:
        ok = await client.set(_k("lock", name), token, nx=True, px=max(1000, int(ttl) * 1000))
        return token if ok else None
    except Exception as exc:  # noqa: BLE001
        _err("acquire_lock", exc)
        return None


async def release_lock(name: str, owner: str) -> None:
    client = runtime._async()
    if client is None or not owner:
        return
    try:
        await client.eval(_SC_LOCK_RELEASE, 1, _k("lock", name), owner)
    except Exception as exc:  # noqa: BLE001
        _err("release_lock", exc)


async def renewal_loop(name: str, owner: str, ttl: int) -> asyncio.Task:
    """Heartbeat loop refreshing a lock lease until released/cancelled."""
    client = runtime._async()
    if client is None:
        async def _noop():
            return None
        return asyncio.get_event_loop().create_task(_noop())
    interval = max(1.0, int(ttl) / 3.0)

    async def _beat():
        while True:
            await asyncio.sleep(interval)
            try:
                await client.expire(_k("lock", name), int(ttl))
            except Exception as exc:  # noqa: BLE001
                _err("renewal", exc)
    return asyncio.get_event_loop().create_task(_beat())


async def maint_try_acquire(name: str) -> str | None:
    """Single-owner guard for background maintenance. None if another holds it."""
    return await acquire_lock(f"maint:{name}", settings.REDIS_MAINT_GUARD_TTL)


async def maint_release(name: str, owner: str) -> None:
    await release_lock(f"maint:{name}", owner)


async def coalesce_claim(key: str) -> str | None:
    """Attempt to claim an in-flight duplicate. Returns owner token or None."""
    client = runtime._async()
    if client is None:
        return None
    token = _rand_token()
    try:
        ok = await client.set(_k("coalesce", "claim", key), token, nx=True, px=settings.REDIS_COALESCE_TTL * 1000)
        return token if ok else None
    except Exception as exc:  # noqa: BLE001
        _err("coalesce_claim", exc)
        return None


async def coalesce_publish(key: str, answer: Any) -> None:
    client = runtime._async()
    if client is None:
        return
    safe = _serializable(answer)
    if safe is None:
        return
    try:
        await client.set(
            _k("coalesce", "ans", key), json.dumps(safe),
            ex=settings.REDIS_COALESCE_ANSWER_TTL,
        )
        await client.delete(_k("coalesce", "claim", key))
    except Exception as exc:  # noqa: BLE001
        _err("coalesce_publish", exc)


async def coalesce_wait(key: str, timeout: float) -> Any | None:
    """Wait for a coalesced answer. Returns parsed value or None on timeout.

    Never deletes the claim: the original generator owns it (its 30s claim
    TTL self-expires if it never finishes).
    """
    client = runtime._async()
    if client is None:
        return None
    deadline = time.monotonic() + timeout
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return None
        try:
            raw = await client.get(_k("coalesce", "ans", key))
            if raw is not None:
                return json.loads(raw)
            await asyncio.sleep(min(0.1, remaining))
        except Exception as exc:  # noqa: BLE001
            _err("coalesce_wait", exc)
            return None


async def coalesce_cleanup(key: str, owner: str | None) -> None:
    """Owner-scoped release of a coalesce claim (compare-and-delete).

    With no owner the claim key is deleted unconditionally — used only by the
    admission controller's release path when the publisher could not store the
    answer (non-serializable result)."""
    client = runtime._async()
    if client is None:
        return
    try:
        if owner:
            await client.eval(_SC_LOCK_RELEASE, 1, _k("coalesce", "claim", key), owner)
        else:
            await client.delete(_k("coalesce", "claim", key))
    except Exception:  # noqa: BLE001
        pass


def _rand_token() -> str:
    import uuid
    return uuid.uuid4().hex


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------

runtime = RedisRuntime(settings.REDIS_URL)


async def startup_check() -> bool:
    """Called at app startup (non-fatal). Logs reachability."""
    if not runtime.enabled():
        log.info("redis: DISABLED (REDIS_URL empty) - single-process behavior")
        return True
    running = await runtime.aping()
    log.info(
        "redis: %s at %s",
        "reachable" if running else "UNREACHABLE (degraded to process-local state)",
        _safe_url(settings.REDIS_URL),
    )
    return running


def shutdown_close() -> None:
    runtime.close()