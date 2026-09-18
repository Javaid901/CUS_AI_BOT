"""
backend/app/orchestrator/state.py

Enhanced conversation state management.

Extends the simple nav-path dict from intent_router with:
  - Breadcrumb trail for navigation flows
  - TTL-based eviction to prevent memory leaks

NOTE: Navigation path state is managed by intent_router._nav_state.
       This module handles conversation state only.

Phase 3C-5 (multi-worker): when Redis is enabled the conversation state is
shared across uvicorn workers. Each worker keeps a WORKING COPY in _STATE;
set_state() writes the copy through to Redis and get_state() adopts the remote
copy when its version is NEWER than the local one, so sequential turns keep
working even when they bounce between workers.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from dataclasses import dataclass, field
from typing import Any

from app.orchestrator.context import ConversationContext
from app.utils import redis_client

_STATE_REDIS_TIMEOUT = 0.35  # bounded per-op ceiling while Redis is degraded


@dataclass
class Breadcrumb:
    """A single breadcrumb entry for navigation history."""

    label: str
    type: str = "nav"  # nav | detail
    context: dict[str, Any] = field(default_factory=dict)


_MAX_BREADCRUMBS = 20


@dataclass
class ConversationState:
    """Full state for a single conversation session.

    Navigation path is NOT stored here — it lives in intent_router._nav_state.
    """

    chat_id: str
    breadcrumbs: list[Breadcrumb] = field(default_factory=list)
    context: ConversationContext = field(default_factory=ConversationContext)
    last_intent: str | None = None
    created_at: float = field(default_factory=time.time)
    touched_at: float = field(default_factory=time.time)

    # Academic catalogue navigation — saved when the engine yields a picker
    # (semester / minor / curriculum doc) so the next message continues.
    catalogue_pending: dict[str, Any] | None = None

    # Slot-fill continuation — when the planner asked for a missing entity
    # (e.g. the programme for a fee request), the pending topic is stored so
    # the next message resolves the pending request directly.
    slot_topic: str | None = None
    slot_request: dict[str, Any] | None = None

    # Last canonical query contract (serialized) for cross-turn resolution.
    last_contract: dict[str, Any] | None = None

    # Student Services auth gate — mirrors the server-side session so the
    # engine can drive the frontend sign-in form vs the authenticated hub.
    # Mirrors the cookie (source of truth); stores NO credentials. Set to
    # None whenever the current action leaves the Student Services flow
    # (the engine enforces this invariant on every turn).
    student_gate: dict[str, Any] | None = None

    # Phase 3C-5: per-worker freshness guard. Mirrors the Redis `_ver` field.
    _local_ver: int = 0

    def touch(self) -> None:
        self.touched_at = time.time()


# ---------------------------------------------------------------------------
# Singleton state store with periodic TTL cleanup
# ---------------------------------------------------------------------------

_STATE: dict[str, ConversationState] = {}
_LOCK = asyncio.Lock()
_TTL_SECONDS = 1800  # 30 minutes of inactivity
_evict_counter: int = 0
_GEN = 1  # monotonically increasing version for each persisted mutation


# ---------------------------------------------------------------------------
# (De)serialization for the shared Redis copy
# ---------------------------------------------------------------------------

def _state_to_dict(state: ConversationState) -> dict[str, Any]:
    ctx = dataclasses.asdict(state.context)
    return {
        "chat_id": state.chat_id,
        "breadcrumbs": [
            {"label": c.label, "type": c.type, "context": c.context}
            for c in state.breadcrumbs
        ],
        "context": ctx,
        "last_intent": state.last_intent,
        "created_at": state.created_at,
        "touched_at": state.touched_at,
        "catalogue_pending": state.catalogue_pending,
        "slot_topic": state.slot_topic,
        "slot_request": state.slot_request,
        "last_contract": state.last_contract,
        "student_gate": state.student_gate,
        "_ver": state._local_ver,
    }


def _state_from_dict(data: dict[str, Any]) -> ConversationState | None:
    """Rebuild a ConversationState from the Redis JSON copy. Returns None when
    the payload is malformed — the caller then falls back to a fresh state."""
    try:
        ctx_data = data.get("context") or {}
        # Use __dataclass_fields__ to include all dataclass fields (including those with defaults)
        valid_fields = ConversationContext.__dataclass_fields__.keys()
        ctx = ConversationContext(
            **{k: v for k, v in ctx_data.items() if k in valid_fields}
        )
        state = ConversationState(
            chat_id=string(data.get("chat_id"), ""),
            context=ctx,
            last_intent=data.get("last_intent"),
            created_at=float(data.get("created_at") or time.time()),
            touched_at=float(data.get("touched_at") or time.time()),
            catalogue_pending=data.get("catalogue_pending"),
            slot_topic=data.get("slot_topic"),
            slot_request=data.get("slot_request"),
            last_contract=data.get("last_contract"),
            student_gate=data.get("student_gate"),
            _local_ver=int(data.get("_ver") or 0),
        )
        for item in data.get("breadcrumbs") or []:
            if not isinstance(item, dict):
                continue
            state.breadcrumbs.append(
                Breadcrumb(
                    label=string(item.get("label"), ""),
                    type=string(item.get("type"), "nav"),
                    context=item.get("context") or {},
                )
            )
        return state
    except Exception:
        return None


def string(value: Any, default: str = "") -> str:
    return str(value) if value is not None else default


async def _bounded(coro, timeout: float = _STATE_REDIS_TIMEOUT):
    """Run a Redis primitive with a hard ceiling; None on timeout/error."""
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
        return None


async def get_state(chat_id: str) -> ConversationState:
    """Get or create a ConversationState for the given chat_id.

    Redis mode: adopts the shared copy when its version is NEWER than the local
    working copy (cross-worker bounce); otherwise keeps the live working copy so
    in-flight mutations are never clobbered mid-turn.
    """
    async with _LOCK:
        local = _STATE.get(chat_id)

    if redis_client.runtime.enabled():
        remote = await _bounded(redis_client.state_get(chat_id))
        if remote is not None:
            remote_ver = int(remote.get("_ver") or 0)
            if local is None or remote_ver > (local._local_ver or 0):
                rebuilt = _state_from_dict(remote)
                if rebuilt is not None:
                    rebuilt._local_ver = remote_ver
                    async with _LOCK:
                        _STATE[chat_id] = rebuilt
                    rebuilt.touch()
                    return rebuilt

    if local is not None:
        local.touch()
        async with _LOCK:
            return _STATE.get(chat_id, local)
    new = ConversationState(chat_id=chat_id)
    async with _LOCK:
        _STATE[chat_id] = new
    # Periodic eviction check (every 50 accesses)
    global _evict_counter
    _evict_counter += 1
    if _evict_counter % 50 == 0:
        await evict_stale()
    return new


async def set_state(chat_id: str, state: ConversationState) -> None:
    """Persist a ConversationState both locally and (Redis mode) to Redis."""
    global _GEN
    state._local_ver = _GEN
    _GEN += 1
    async with _LOCK:
        _STATE[chat_id] = state
    # state_set is a no-op when Redis is disabled/unreachable.
    await _bounded(redis_client.state_set(chat_id, _state_to_dict(state), _TTL_SECONDS))


async def clear_state(chat_id: str) -> None:
    """Clear all state for a chat session (incl. the legacy nav path)."""
    async with _LOCK:
        _STATE.pop(chat_id, None)
    if redis_client.runtime.enabled():
        await _bounded(redis_client.state_delete(chat_id))
    try:
        from app.chat.intent_router import clear_nav
        clear_nav(chat_id)
    except Exception:
        pass


async def persist_current(chat_id: str) -> None:
    """Flush whatever the process currently holds for `chat_id` (Phase 3C-5).

    Used by engine.process' finally block: if the turn cleared the conversation
    the shared copy is deleted too; otherwise the (possibly evolved) working
    copy is written through.
    """
    async with _LOCK:
        current = _STATE.get(chat_id)
    if current is None:
        if redis_client.runtime.enabled():
            await _bounded(redis_client.state_delete(chat_id))
        return
    await set_state(chat_id, current)


async def pop_breadcrumb(chat_id: str) -> Breadcrumb | None:
    """Pop the last breadcrumb and return it."""
    state = await get_state(chat_id)
    if state.breadcrumbs:
        return state.breadcrumbs.pop()
    return None


async def push_breadcrumb(chat_id: str, crumb: Breadcrumb) -> None:
    """Push a breadcrumb with duplicate prevention and size limit."""
    state = await get_state(chat_id)
    if state.breadcrumbs and state.breadcrumbs[-1].label == crumb.label:
        return  # Skip duplicate
    if len(state.breadcrumbs) >= _MAX_BREADCRUMBS:
        state.breadcrumbs.pop(0)  # Evict oldest
    state.breadcrumbs.append(crumb)


async def evict_stale() -> int:
    """Remove states that have exceeded the TTL. Returns count evicted.

    Shared Rediss copies expire on their own TTL (30 min); only the process-local
    working-copy map needs sweeping, mirroring the pre-Redis behavior.
    """
    now = time.time()
    async with _LOCK:
        stale = [cid for cid, s in _STATE.items() if now - s.touched_at > _TTL_SECONDS]
        for cid in stale:
            _STATE.pop(cid, None)
    if stale:
        # Keep the legacy nav-path store bounded in lockstep with the states.
        try:
            from app.chat.intent_router import clear_nav
            for cid in stale:
                clear_nav(cid)
        except Exception:
            pass
    return len(stale)
