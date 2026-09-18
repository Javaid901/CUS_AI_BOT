"""
backend/app/llm/gate.py

Shared global LLM concurrency gate (Phase 3C-1, extended Phase 3C-5).

ONE budget protects BOTH chat generation and grievance formalization so the
on-premise Ollama server can never be flooded from two independent paths.
The configured limit (MAX_CONCURRENT_LLM) is the single source of truth.

Design notes:
  * Phase 3C-1: the gate is a thread-safe counting gate. The chat path uses the
    async `acquire` (cancellation-safe: a slot is only claimed after an await
    returns, so an asyncio.CancelledError never leaks a slot). The grievance
    path uses the blocking `acquire_sync` from its sync worker thread.
  * Phase 3C-5: when Redis is enabled the counter lives in Redis so every
    uvicorn worker shares the SAME global ceiling. The guarantee is FAIL-CLOSED:
    if Redis is unavailable the gate reports busy (acquire returns False), and
    the caller's existing fallback path is used. We deliberately never fall
    back to per-process counting, because 2 workers x local_max would double
    the ceiling vs. the single-point-of-truth MAX_CONCURRENT_LLM.
  * Layering is preserved: the request-manager service semaphores stay intact;
    this gate adds shared LLM resource protection below them.
  * Slots are ALWAYS returned in `finally`. A timeout (gate busy) never
    raises — callers fall back to their existing graceful paths.

The chat path integration (chat/service.py) calls acquire()/release(). The
grievance path (grievance/llm.py) uses acquire_sync()/release().
"""

from __future__ import annotations

import asyncio
import threading
import time

from app.config import settings
from app.utils import redis_client


class SharedLLMGate:
    """Counting gate usable from both async and sync contexts (local or Redis)."""

    def __init__(self, max_concurrent: int | None = None) -> None:
        self._cv = threading.Condition()
        self._held = 0
        self._max = max(int(max_concurrent or settings.MAX_CONCURRENT_LLM) or 1, 1)

    # -- mode ----------------------------------------------------------------
    def _distributed(self) -> bool:
        """Redis drives the gate whenever it is configured (fail-closed below)."""
        return redis_client.runtime.enabled()

    # -- local primitives (Phase 3C-1, unchanged) -----------------------------
    def _try_claim(self) -> bool:
        with self._cv:
            if self._held < self._max:
                self._held += 1
                return True
            return False

    def _release_local(self) -> None:
        with self._cv:
            self._held = max(0, self._held - 1)
            self._cv.notify()

    def release(self) -> None:
        if self._distributed():
            try:
                asyncio.get_event_loop().create_task(redis_client.llm_gate_release())
            except RuntimeError:
                redis_client.llm_gate_release_sync()
            return
        self._release_local()

    def acquire_sync(self, timeout: float = 10.0) -> bool:
        """Blocking acquire for sync threads (grievance path).

        Returns False on timeout without raising. On True the caller MUST
        release() (use the `with` context manager or try/finally).
        """
        if self._distributed():
            deadline = time.monotonic() + max(timeout, 0.0)
            while True:
                claimed = redis_client.llm_gate_claim_sync(self._max)
                if claimed is True:
                    return True
                if claimed is None:
                    # Redis unavailable: fail closed (treat as gate busy).
                    return False
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                time.sleep(min(0.05, remaining))
        if self._try_claim():
            return True
        deadline = time.monotonic() + max(timeout, 0.0)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            with self._cv:
                self._cv.wait(min(remaining, 0.05))
            if self._try_claim():
                return True

    async def acquire(self, timeout: float = 10.0) -> bool:
        """Async acquire for the chat path (cancellation-safe).

        The loop polls with cancellable sleeps; a slot is claimed only
        synchronously right after an await, so a CancelledError arriving
        during the sleep can never leave a claimed-but-unreleased slot.
        """
        if self._distributed():
            loop = asyncio.get_running_loop()
            deadline = loop.time() + max(timeout, 0.0)
            while True:
                claimed = await redis_client.llm_gate_claim(self._max)
                if claimed is True:
                    return True
                if claimed is None:
                    # Redis unavailable: fail closed (treat as gate busy).
                    return False
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return False
                await asyncio.sleep(min(0.05, max(remaining, 0.0)))
        if self._try_claim():
            return True
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(timeout, 0.0)
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            if self._try_claim():
                return True
            await asyncio.sleep(min(0.05, max(remaining, 0.0)))

    # -- context managers ----------------------------------------------------
    async def __aenter__(self) -> "SharedLLMGate":
        await self.acquire(timeout=settings.MAX_SEMAPHORE_WAIT)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False

    def __enter__(self) -> "SharedLLMGate":
        acquired = self.acquire_sync(timeout=settings.MAX_SEMAPHORE_WAIT)
        if not acquired:
            raise TimeoutError("Shared LLM gate busy")
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        self.release()
        return False

    # -- metrics -------------------------------------------------------------
    @property
    def held(self) -> int:
        if self._distributed():
            return redis_client.llm_gate_held_sync()
        with self._cv:
            return self._held

    @property
    def max_concurrent(self) -> int:
        return self._max

    @property
    def available(self) -> int:
        if self._distributed():
            return max(0, self._max - redis_client.llm_gate_held_sync())
        with self._cv:
            return max(0, self._max - self._held)

    def stats(self) -> dict:
        return {"max_concurrent": self._max, "held": self.held, "available": self.available}


shared_llm_gate = SharedLLMGate()