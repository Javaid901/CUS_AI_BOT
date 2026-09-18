"""
backend/app/request_manager/token_bucket.py

Token Bucket rate limiter.

Replaces the old sliding-window rate limiter (utils/rate_limit.py).

Each user gets a token bucket. Tokens are refilled at a configurable rate.
Each request costs a number of tokens based on its weight.
Heavy users naturally slow down; normal users never see 429.
"""

from __future__ import annotations

import threading
import time

from app.config import settings
from app.utils import redis_client


def _server_now(client) -> float:
    """Redis server time (seconds); falls back to local clock."""
    try:
        sec, usec = client.time()
        return float(sec) + float(usec) / 1_000_000.0
    except Exception:  # noqa: BLE001
        return time.time()


class _UserBucket:
    """Per-user token bucket."""

    __slots__ = ("last_refill", "max_tokens", "refill_rate", "tokens")

    def __init__(self, max_tokens: int, refill_rate: float) -> None:
        self.tokens = float(max_tokens)
        self.last_refill = time.monotonic()
        self.max_tokens = float(max_tokens)
        self.refill_rate = refill_rate  # tokens per second

    def _refill(self) -> None:
        now = time.monotonic()
        elapsed = now - self.last_refill
        self.tokens = min(self.max_tokens, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now

    def consume(self, cost: int = 1) -> bool:
        """Try to consume *cost* tokens. Returns True if allowed."""
        self._refill()
        if self.tokens >= cost:
            self.tokens -= cost
            return True
        return False

    def refund(self, cost: int = 1) -> None:
        """Credit back *cost* tokens (never exceeds the bucket cap)."""
        self.tokens = min(self.max_tokens, self.tokens + cost)

    @property
    def available(self) -> float:
        self._refill()
        return self.tokens

    def wait_seconds_for(self, cost: int) -> float:
        """Estimated seconds until *cost* tokens are available."""
        self._refill()
        if self.refill_rate <= 0:
            return float("inf")
        return max(0.0, (cost - self.tokens) / self.refill_rate)


class TokenBucket:
    """Global token bucket manager — one bucket per user key."""

    def __init__(
        self,
        max_tokens: int | None = None,
        refill_rate: float | None = None,
    ) -> None:
        self._max_tokens = float(max_tokens or settings.TOKEN_BUCKET_SIZE)
        self._refill_rate = float(refill_rate or settings.TOKEN_REFILL_RATE)
        self._buckets: dict[str, _UserBucket] = {}
        self._lock = threading.Lock()

    def _get_or_create(self, key: str) -> _UserBucket:
        if key not in self._buckets:
            self._buckets[key] = _UserBucket(self._max_tokens, self._refill_rate)
        return self._buckets[key]

    def _sweep_idle(self, now: float) -> None:
        """Drop buckets idle for over an hour so memory stays bounded."""
        cutoff = now - 3600.0
        stale = [k for k, b in self._buckets.items() if b.last_refill < cutoff]
        for k in stale:
            del self._buckets[k]

    # -- distributed (Phase 3C-5) ---------------------------------------------
    def _redis_enabled(self) -> bool:
        """Redis buckets are used only while Redis is reachable; a failure
        falls back to the local bucket for that call so nothing breaks.

        Uses the cached availability (a ping would block the asyncio loop)."""
        return redis_client.runtime.enabled() and redis_client.runtime.available_now()

    def _redis_key(self, key: str) -> str:
        prefix = settings.REDIS_KEY_PREFIX or "cus"
        return f"{prefix}:tb:{key}"

    def consume(self, key: str, cost: int = 1) -> bool:
        """Deduct *cost* tokens from the user's bucket. Returns True if allowed."""
        if self._redis_enabled():
            ok = redis_client.token_bucket_consume_sync(key, self._max_tokens, self._refill_rate, cost)
            if ok is not None:
                return ok
        with self._lock:
            if len(self._buckets) > 5000:
                self._sweep_idle(time.monotonic())
            bucket = self._get_or_create(key)
            return bucket.consume(cost)

    def available(self, key: str) -> float:
        """Return current token count for a user."""
        if self._redis_enabled():
            client = redis_client.runtime._sync()
            try:
                val = client.hmget(self._redis_key(key), "t", "s") if client else [None, None]
                now = _server_now(client)
                tokens = float(val[0]) if val and val[0] is not None else float(self._max_tokens)
                ts = float(val[1]) if val and len(val) > 1 and val[1] is not None else now
                tokens = min(self._max_tokens, tokens + max(0.0, now - ts) * self._refill_rate)
                return round(tokens, 1)
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            bucket = self._get_or_create(key)
            return bucket.available

    def wait_estimate(self, key: str, cost: int = 1) -> float:
        """Estimate seconds until *cost* tokens are available."""
        if self._redis_enabled():
            client = redis_client.runtime._sync()
            try:
                val = client.hmget(self._redis_key(key), "t", "s") if client else [None, None]
                now = _server_now(client)
                tokens = float(val[0]) if val and val[0] is not None else float(self._max_tokens)
                ts = float(val[1]) if val and len(val) > 1 and val[1] is not None else now
                tokens = min(self._max_tokens, tokens + max(0.0, now - ts) * self._refill_rate)
                if self._refill_rate <= 0:
                    return float("inf")
                return max(0.0, (cost - tokens) / self._refill_rate)
            except Exception:  # noqa: BLE001
                pass
        with self._lock:
            bucket = self._get_or_create(key)
            return bucket.wait_seconds_for(cost)

    def reset(self, key: str) -> None:
        """Reset a user's bucket to full."""
        if redis_client.runtime.enabled() and redis_client.runtime.available_now():
            client = redis_client.runtime._sync()
            if client is not None:
                try:
                    client.delete(self._redis_key(key))
                except Exception:  # noqa: BLE001
                    pass
        with self._lock:
            self._buckets.pop(key, None)

    def refund(self, key: str, cost: int = 1) -> None:
        """Credit *cost* tokens back to the user's bucket (used when a
        request is queued/rejected without being served)."""
        if self._redis_enabled():
            redis_client.token_bucket_refund_sync(key, self._max_tokens, self._refill_rate, cost)
        with self._lock:
            bucket = self._get_or_create(key)
            bucket.refund(cost)

    @property
    def active_users(self) -> int:
        return len(self._buckets)

    def stats(self) -> dict:
        with self._lock:
            active = len(self._buckets)
            total_tokens = sum(b.available for b in self._buckets.values())
        return {
            "active_users": active,
            "total_tokens_remaining": round(total_tokens, 1),
            "max_tokens_per_user": self._max_tokens,
            "refill_rate_per_sec": self._refill_rate,
        }


token_bucket = TokenBucket()
