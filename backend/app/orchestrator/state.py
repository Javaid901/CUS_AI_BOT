"""
backend/app/orchestrator/state.py

Enhanced conversation state management.

Extends the simple nav-path dict from intent_router with:
  - Breadcrumb trail for navigation flows
  - TTL-based eviction to prevent memory leaks

NOTE: Navigation path state is managed by intent_router._nav_state.
      This module handles conversation state only.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any

from app.orchestrator.context import ConversationContext


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

    def touch(self) -> None:
        self.touched_at = time.time()


# ---------------------------------------------------------------------------
# Singleton state store with periodic TTL cleanup
# ---------------------------------------------------------------------------

_STATE: dict[str, ConversationState] = {}
_LOCK = asyncio.Lock()
_TTL_SECONDS = 1800  # 30 minutes of inactivity
_evict_counter: int = 0


async def get_state(chat_id: str) -> ConversationState:
    """Get or create a ConversationState for the given chat_id."""
    async with _LOCK:
        if chat_id not in _STATE:
            _STATE[chat_id] = ConversationState(chat_id=chat_id)
        state = _STATE[chat_id]
        state.touch()
    # Periodic eviction check (every 50 accesses)
    global _evict_counter
    _evict_counter += 1
    if _evict_counter % 50 == 0:
        await evict_stale()
    return state


async def set_state(chat_id: str, state: ConversationState) -> None:
    async with _LOCK:
        _STATE[chat_id] = state


async def clear_state(chat_id: str) -> None:
    """Clear all state for a chat session (incl. the legacy nav path)."""
    async with _LOCK:
        _STATE.pop(chat_id, None)
    try:
        from app.chat.intent_router import clear_nav
        clear_nav(chat_id)
    except Exception:
        pass


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
    """Remove states that have exceeded the TTL. Returns count evicted."""
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
