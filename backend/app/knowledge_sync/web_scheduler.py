"""
backend/app/knowledge_sync/web_scheduler.py

Scheduled running of the Website Knowledge Sync engine.

Reads the persisted sync state (dashboard-controlled):
    enabled : bool
    schedule: manual | hourly | daily | weekly | monthly | disabled
    hours   : explicit cadence override (0 disables)

The scheduler polls the state every 60s and triggers a background full crawl
when the next run is due. It never blocks the chat/API event loop.
"""

from __future__ import annotations

import threading
from datetime import datetime, timezone

from app.knowledge_sync.web_engine import load_state, save_state
from app.utils.logging import log

_SCHEDULER_THREAD: threading.Thread | None = None
_STOP_EVENT = threading.Event()
_POLL_SECONDS = 60

# Schedule presets: hours between sync runs (0 = disabled / manual)
SCHEDULE_PRESETS = {
    "disabled": 0,       # master toggle must be ON for any sync
    "manual": 0,         # ON + manual = immediate sync, no repeats
    "hourly": 1,
    "6hourly": 6,
    "daily": 24,
    "weekly": 168,
    "monthly": 720,
}


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_master_enabled(state: dict | None = None) -> bool:
    """Check the master sync toggle. Returns True when the admin has turned
    the Website Sync master switch ON (via the dashboard or API)."""
    if state is None:
        state = load_state()
    return bool(state.get("enabled", False))


def _run_sync() -> None:
    """Fire-and-forget a full crawl in a dedicated thread."""
    def _worker() -> None:
        try:
            from app.database import SessionLocal
            from app.knowledge_sync.web_engine import WebsiteSyncEngine

            db = SessionLocal()
            try:
                # Check master toggle before running
                if not _is_master_enabled():
                    log.info("Sync skipped: master toggle is OFF")
                    return
                engine = WebsiteSyncEngine(db)
                stats = engine.run(trigger="scheduled")
                log.info("Scheduled website sync completed: %s", stats)
            finally:
                db.close()
        except Exception as exc:  # noqa: BLE001
            log.error("Scheduled sync crashed: %s", exc)

    threading.Thread(target=_worker, daemon=True).start()


def _poll_once(state: dict | None = None) -> None:
    """Perform one scheduler decision cycle.

    Only a positive automatic cadence (hourly/daily/weekly/...) combined with
    the master enable flag can start a crawl. "manual" and "disabled" presets
    (hours = 0) NEVER auto-crawl: only an explicit administrator "Sync Now"
    starts a sync in those modes.
    """
    if state is None:
        state = load_state()
    # Master toggle check — if OFF, never auto-crawl regardless of schedule.
    if not _is_master_enabled(state):
        log.info("Scheduler: master toggle OFF — skipping auto-crawl")
        return
    hours = SCHEDULE_PRESETS.get(state.get("schedule", "disabled"), 0)
    if hours <= 0:
        # manual / disabled cadence: manual means manual. The dashboard
        # "Sync Now" button is the only trigger; no run-once-then-wait.
        log.info("Scheduler: schedule '%s' — manual only; no automatic crawl",
                 state.get("schedule", "disabled"))
        return
    last_raw = state.get("last_run_at")
    due = True
    if last_raw:
        try:
            last = datetime.fromisoformat(last_raw)
            due = (_utcnow() - last).total_seconds() >= hours * 3600
        except ValueError:
            due = True
    if due:
        state["last_run_at"] = _utcnow().isoformat()
        save_state(state)
        _run_sync()


def _run_loop() -> None:
    """Poll the persisted state every POLL_SECONDS and sync when due."""
    log.info("Website Sync scheduler started (poll %ss)", _POLL_SECONDS)
    while not _STOP_EVENT.wait(_POLL_SECONDS):
        try:
            _poll_once()
        except Exception as exc:  # noqa: BLE001
            log.error("Website sync scheduler iteration failed: %s", exc)


def start() -> None:
    """Start the scheduler thread (no-op if already running)."""
    global _SCHEDULER_THREAD
    global _STOP_EVENT
    if _SCHEDULER_THREAD is not None and _SCHEDULER_THREAD.is_alive():
        log.info("Scheduler already running")
        return
    _STOP_EVENT = threading.Event()
    thread = threading.Thread(target=_run_loop, daemon=True)
    _SCHEDULER_THREAD = thread
    thread.start()
    log.info("Website Sync scheduler started")


def stop() -> None:
    """Signal the scheduler thread to stop."""
    _STOP_EVENT.set()