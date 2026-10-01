"""
backend/tests/test_full_project_audit.py

Full-project audit regression matrix.

Locks in every fix and every invariant surfaced by the full-project functional
and performance audit, so regressions are caught by the core suite:

  * Fix A — grievance detector accepts ordinary "complain about ..." phrasing
    ("i want to complain about my professor", "I want to complain to the
    Dean", "how do i complain about hostel") while never flagging process
    questions or unrelated text ("tell me a joke", "what is a grievance").
  * Fix B — current-status detector recognizes the Hinglish weak cue
    "khul gaye / khol gaye / khula hai" ("admission khol gaye hain") while the
    subject guard keeps non-status mentions ("shop khol gaye hain") neutral
    and "mca khula hai" un-hijacked.
  * P1/P0 route invariants that speed work must never disturb:
      - "show me the notices"      -> action "news"        (P1 design)
      - examination date sheet     -> dedicated route      (never rag/slot_fill)
      - exam-fee status question   -> stays structured     (never intelligent)
      - authority lookups          -> never intelligent
  * Generator shared streaming client (install/close/loop-safety + both code
    paths of stream_answer_async cleanly raising GenerationError).
  * Stage timers keep recording into the metrics summary.

Run:  python -m pytest tests/test_full_project_audit.py -q
"""

from __future__ import annotations

import asyncio
import threading
import uuid
from pathlib import Path

import pytest

from app.database import SessionLocal, create_all
from app.catalogue.seed import seed_catalogue
from app.grievance.detect import detect_grievance
from app.orchestrator.context import ConversationContext
from app.orchestrator.current_status import classify_current_status
from app.orchestrator.extractor import extract_entities
from app.orchestrator.planner import plan

create_all()


@pytest.fixture(scope="module", autouse=True)
def _seed_catalogue():
    db = SessionLocal()
    try:
        seed_catalogue(db)
        db.commit()
    finally:
        db.close()


def _planning_action(raw_message: str):
    e = extract_entities(raw_message)
    p = plan(raw_message, ConversationContext(), f"audit-{uuid.uuid4().hex[:8]}", e)
    return p.action, p.target, p.reason, p.extra


# ---------------------------------------------------------------------------
# A. Fix A — grievance "complain" phrasing
# ---------------------------------------------------------------------------

def test_grievance_accepts_complain_about_phrasing():
    for msg in (
        "i want to complain about my professor",
        "I want to complain to the Dean",
        "how do i complain about hostel",
        "complain about my result",
    ):
        assert detect_grievance(msg)["is_grievance"], msg


def test_grievance_hinglish_markers_still_work():
    for msg in ("meri complaint hai", "meri shikayat hai", "my fee not refunded"):
        assert detect_grievance(msg)["is_grievance"], msg


def test_grievance_rejects_process_and_unrelated_text():
    for msg in ("what is a grievance", "how to file a grievance",
                "where is the grievance cell", "tell me a joke",
                "when will my result come"):
        assert not detect_grievance(msg)["is_grievance"], msg


def test_grievance_route_in_planner():
    action, _, _, _ = _planning_action("i want to complain about my professor")
    assert action == "grievance", f"expected grievance action, got {action}"


# ---------------------------------------------------------------------------
# B. Fix B — Hinglish "khol gaya / khula" weak currentness cue
# ---------------------------------------------------------------------------

def test_status_hinglish_khol_cue():
    for msg in ("admission khol gaye hain", "admission khula hai",
                "admission khol gaay hain?"):
        assert classify_current_status(msg) is True, msg


def test_status_khol_band_cues():
    assert classify_current_status("admission band hai") is True
    assert classify_current_status("admission band hai kya") is True


def test_status_subject_guard_ignores_non_status_khol():
    assert classify_current_status("shop khol gaye hain") is False
    assert classify_current_status("mca khula hai") is False


# ---------------------------------------------------------------------------
# C. Route invariants (P0/P1) the speed work must not disturb
# ---------------------------------------------------------------------------

def test_notices_stays_news_route():
    action, _, _, _ = _planning_action("show me the notices")
    assert action == "news", f"P1 notices route changed -> {action}"


def test_exam_datesheet_stays_dedicated():
    for msg in ("show the mca date sheet", "when is the mca 3rd semester exam"):
        action, _, _, _ = _planning_action(msg)
        assert action not in ("intelligent", "rag", "slot_fill"), f"{msg!r} -> {action}"
        assert action != "catalogue", f"{msg!r} hijacked by catalogue -> {action}"


def test_exam_fee_status_stays_structured():
    action, _, _, _ = _planning_action("has the mca exam fee been announced?")
    assert action not in ("intelligent", "rag", "slot_fill"), f"-> {action}"
    assert action == "structured", f"P0 structured route changed -> {action}"


def test_authority_lookups_never_intelligent():
    for msg in ("who is registrar", "who handles exams", "who is the registrar?"):
        action, _, _, _ = _planning_action(msg)
        assert action != "intelligent", f"{msg!r} hijacked -> {action}"


def test_gibberish_never_intelligent():
    action, _, _, _ = _planning_action("qux quux garply zonk frobnicate")
    assert action != "intelligent", f"gibberish hijacked -> {action}"


# ---------------------------------------------------------------------------
# D. Generator shared streaming client lifecycle
# ---------------------------------------------------------------------------

def test_generator_shared_client_install_loop_and_close():
    """Production-shaped lifecycle: install + use + close on ONE loop;
    a close from a different loop must NOT uninstall the client."""
    from app.ingest import generator as g

    async def main(ctx: dict):
        g.install_async_client()
        g.install_async_client()  # second install is a no-op
        ctx["installed"] = g._ASYNC_CLIENT is not None
        ctx["same_loop"] = g._shared_async_client() is g._ASYNC_CLIENT

        # A close from a foreign loop must leave the client installed.
        async def other_loop_close():
            await g.close_async_client()

        holder: dict = {}
        def run_foreign_close() -> None:
            other = asyncio.new_event_loop()
            asyncio.set_event_loop(other)
            try:
                other.run_until_complete(other_loop_close())
                holder["ok"] = True
            finally:
                other.close()
                asyncio.set_event_loop(None)

        t = threading.Thread(target=run_foreign_close)
        t.start()
        t.join()
        assert holder.get("ok")

        ctx["kept_after_foreign_close"] = g._ASYNC_CLIENT is not None

        # Owning-loop close clears it; a second close stays safe.
        await g.close_async_client()
        ctx["closed"] = g._ASYNC_CLIENT is None
        await g.close_async_client()
        ctx["idempotent"] = g._ASYNC_CLIENT is None

    ctx: dict = {}
    asyncio.run(main(ctx))
    assert ctx["installed"], "install_async_client did not create the client"
    assert ctx["same_loop"], "shared client not returned on the owning loop"
    assert ctx["kept_after_foreign_close"], "foreign-loop close must not uninstall"
    assert ctx["closed"], "owning-loop close must clear the client"
    assert ctx["idempotent"], "repeated close must be safe"


def test_stream_answer_async_both_paths_clean_up(monkeypatch):
    """Both the shared-client and per-call paths reach httpx, raise a clean
    GenerationError, and leave the shared client reusable. Runs on a single
    loop exactly like the FastAPI lifecycle."""
    from app.ingest import generator as g
    from app.ingest.generator import GenerationError

    # Force a fast connection failure so no real Ollama request is made.
    monkeypatch.setattr(g.settings, "OLLAMA_BASE_URL", "http://127.0.0.1:9")

    async def expect_generation_error() -> None:
        with pytest.raises(GenerationError):
            async for _ in g.stream_answer_async(
                "question", "context", system="sys"
            ):
                pass

    async def main() -> None:
        g.install_async_client()
        assert g._shared_async_client() is not None
        await expect_generation_error()      # shared-client code path
        await g.close_async_client()
        assert g._shared_async_client() is None
        await expect_generation_error()      # per-call fallback code path
        await g.close_async_client()         # idempotent after both paths

    asyncio.run(main())


# ---------------------------------------------------------------------------
# E. Stage timers keep recording into metrics
# ---------------------------------------------------------------------------

def test_stage_timer_records_and_summary_exposes_it():
    from app.orchestrator.metrics import clear_metrics, stage_elapsed, stage_timer

    clear_metrics()
    with stage_timer("audit_probe_stage"):
        pass
    assert stage_elapsed("audit_probe_stage") is not None
    summary = __import__("app.orchestrator.metrics", fromlist=["metrics_summary"]).metrics_summary()
    assert "audit_probe_stage" in summary
    clear_metrics()


def test_engine_imports_cleanly():
    import app.orchestrator.engine  # noqa: F401
    assert True


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))