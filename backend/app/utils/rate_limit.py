"""
backend/app/utils/rate_limit.py

Sliding-window rate limiter for the chat endpoint.

Phase 3C-5: when Redis is enabled the sliding window is enforced ATOMICALLY in
Redis so all uvicorn workers share one budget per client. When Redis is disabled
or unreachable, the original process-local deque is used and behavior is
byte-for-byte identical to the single-process deployment.

Used via the `chat_rate_limit` dependency which raises 429 when exceeded.
"""

from __future__ import annotations

import asyncio
import itertools
import threading
import time
from collections import defaultdict, deque

from app.config import settings
from app.utils import redis_client
from fastapi import HTTPException, Request, status

_HITS: dict[str, deque[float]] = defaultdict(deque)
_RATE_LOCK = threading.Lock()
_SEQ = itertools.count(1)

# Worst-case per-request time we are willing to spend on the Redis round-trip
# before falling back to the process-local limiter (protects latency during an
# outage; healthy Redis answers far below this).
_REDIS_OP_TIMEOUT = 0.35


def _client_key(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _local_allow(key: str, limit: int, now: float, window_start: float) -> None:
    """Original in-memory sliding window. Raises 429 when the window is full."""
    with _RATE_LOCK:
        dq = _HITS[key]
        while dq and dq[0] < window_start:
            dq.popleft()
        if len(dq) >= limit:
            retry = int(dq[0] - window_start) + 1
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded. Try again in {retry}s.",
            )
        dq.append(now)


async def _allow(request: Request, bucket: str, local_key: str, limit: int) -> None:
    if limit <= 0:
        return
    now = time.time()
    window_start = now - 60

    if redis_client.runtime.enabled():
        try:
            result = await asyncio.wait_for(
                redis_client.sliding_window_consume(bucket, limit, 60.0, next(_SEQ)),
                timeout=_REDIS_OP_TIMEOUT,
            )
        except asyncio.TimeoutError:
            result = None
        if result is None:
            # Redis unavailable -> local fallback (single-process behavior).
            _local_allow(local_key, limit, now, window_start)
            return
        allowed, retry = result
        if not allowed:
            raise HTTPException(
                status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                detail=f"Rate limit exceeded. Try again in {int(retry)}s.",
            )
        return

    _local_allow(local_key, limit, now, window_start)


async def chat_rate_limit(request: Request) -> None:
    """FastAPI dependency: enforce RATE_LIMIT_PER_MINUTE per client IP."""
    key = _client_key(request)
    await _allow(request, bucket=f"chat:{key}", local_key=key, limit=settings.RATE_LIMIT_PER_MINUTE)


def endpoint_rate_limit(max_per_minute: int, bucket: str = "endpoint"):
    """Factory for per-IP sliding-window limits on public endpoints.

    Usage:  Depends(endpoint_rate_limit(settings.GRIEVANCE_CREATE_LIMIT, "grievance-create"))
    Raises HTTPException(429) when the client exceeds the limit.
    """

    async def _dep(request: Request) -> None:
        key = _client_key(request)
        await _allow(
            request,
            bucket=f"{bucket}:{key}",
            local_key=f"{bucket}:{key}",
            limit=max_per_minute,
        )

    return _dep