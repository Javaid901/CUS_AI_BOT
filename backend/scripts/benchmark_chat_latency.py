"""
backend/scripts/benchmark_chat_latency.py

End-to-end chat-latency benchmark for the CUS AI Assistant.

Drives the REAL orchestration engine (app.orchestrator.engine.process) against
the real DB and Ollama — the same code path a user's /api/chat/ask request
takes after the Admission Controller passes it to the orchestrator. Measures
steady-state ("warm") latency per route category.

Categories & targets (from the performance spec):
  deterministic / simple  -> warm < 300 ms   (welcome, blocked, structured,
                                               catalogue, examination, notices,
                                               grievances, student-service gate)
  no-info / status        -> warm < 1 s      (status without evidence,
                                               out-of-scope, empty-corpus)
  rag (evidence + LLM)    -> reported only   (LLM-generation dominated)

Usage:
  cd backend
  python scripts/benchmark_chat_latency.py [--routes welcome,blocked,...]
                                             [--repeat 3]
                                             [--llm]

It runs in-process so per-route results are directly comparable across code
changes. The classifier / Chroma / BM25 warm-ups are done up-front so the
numbers measure steady-state behaviour, not first-call model loading.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
import time
import uuid
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

import app.models  # noqa: F401  (register tables before any session)

from app.database import SessionLocal, create_all  # noqa: E402

HARNESS_UID = "b1111111-2222-4333-8444-555555555555"

BATTERY: dict[str, list[str]] = {
    "welcome": ["hi", "hello"],
    "blocked": ["how do i fake my marks in the portal"],
    "structured": ["how many semesters in bca"],
    "catalogue": ["what is the mca fee"],
    "examination": ["show the mca date sheet", "when is the bca semester 3 exam"],
    "status_noinfo": ["is admission open", "admission band hai kya"],
    "outside_scope": ["what is the weather in srinagar tomorrow", "tell me a joke"],
    "grievance": ["i want to complain about my professor"],
    "student_gate": ["check my result", "download my admit card"],
    "notices": ["show me the notices"],
    "nonsense": ["qux quux garply zonk frobnicate"],
    "sorta": ["when will bca results be announced"],
}


def _ensure_user() -> None:
    import uuid as _u

    from app.models.db_models import User

    db = SessionLocal()
    try:
        if db.get(User, _u.UUID(HARNESS_UID)) is None:
            db.add(User(id=HARNESS_UID, username="benchmarkuser",
                        email="bench@example.edu", hashed_password="x", role="student"))
            db.commit()
    finally:
        db.close()


async def _run_one(msg: str) -> float:
    db = SessionLocal()
    t0 = time.perf_counter()
    try:
        async for _ in __import__("app.orchestrator.engine", fromlist=["process"]).process(
            db, HARNESS_UID, msg, "bench-" + uuid.uuid4().hex[:10],
            student_session=None, student_auth_kind="none",
        ):
            pass
    finally:
        db.close()
    return (time.perf_counter() - t0) * 1000.0


async def _warm_models() -> None:
    """Pre-warm the classifier, Chroma and BM25 so the benchmark measures
    steady-state latency (mimics production startup warm-ups)."""
    from app.orchestrator.intent_classifier import _ensure_loaded

    await asyncio.to_thread(_ensure_loaded)
    from app.ingest.retriever import get_diagnostics  # noqa: F401
    from app.orchestrator.context import ConversationContext
    from app.orchestrator.extractor import extract_entities
    from app.orchestrator import planner

    # One throwaway plan call warms embed/centroid caches.
    ctx = ConversationContext()
    await asyncio.to_thread(
        planner.plan, "sample warmup query about mca admission", ctx,
        "warm-chat", extract_entities("sample warmup query about mca admission"),
    )
    # One throwaway retrieval warms Chroma / BM25.
    from app.ingest.retrieve import retrieve

    await asyncio.to_thread(retrieve, "sample warmup query about mca admission")
    await asyncio.to_thread(retrieve, "warmup second retrieval passthrough query")


async def _bench_llm() -> dict[str, float]:
    from app.ingest.generator import stream_answer_async

    question = "What is the duration of the MCA programme at Cluster University Srinagar?"
    context = (
        "The MCA programme is a two-year postgraduate course offered in four "
        "semesters by Cluster University Srinagar."
    )
    tokens: list[str] = []
    t0 = time.perf_counter()
    async for tok in stream_answer_async(question, context):
        tokens.append(tok)
    total_ms = (time.perf_counter() - t0) * 1000.0
    n = len("".join(tokens))
    return {
        "total_ms": round(total_ms, 1),
        "chars": n,
        "tokens_approx": n,
        "chars_per_sec": round(n / (total_ms / 1000.0), 1) if total_ms else 0.0,
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--routes", default=",".join(BATTERY), help="comma list of route keys")
    ap.add_argument("--repeat", type=int, default=3)
    ap.add_argument("--llm", action="store_true", help="also benchmark a raw LLM generation")
    args = ap.parse_args()

    create_all()
    _ensure_user()
    print("warming classifier / chroma / bm25 ...")
    await _warm_models()

    wanted = [r for r in args.routes.split(",") if r]
    rows: list[tuple[str, list[float]]] = []
    for route in wanted:
        if route not in BATTERY:
            print(f"unknown route: {route}")
            continue
        results: list[float] = []
        for msg in BATTERY[route]:
            for _ in range(args.repeat):
                results.append(await _run_one(msg))
        rows.append((route, results))

    print("\n== warm latencies (ms) ==")
    print(f"{'route':<16} {'n':>3} {'p50':>8} {'p90':>8} {'max':>8} {'target':>10}")
    for route, samples in rows:
        p50 = statistics.median(samples)
        p90 = sorted(samples)[int(len(samples) * 0.9) - 1] if samples else 0.0
        target = "<300ms" if route in (
            "welcome", "blocked", "structured", "catalogue", "examination",
            "grievance", "student_gate", "notices",
        ) else "<1s"
        flag = ""
        if route in ("welcome", "blocked", "structured", "catalogue", "examination",
                     "grievance", "student_gate", "notices") and p50 >= 300:
            flag = "  <-- SLOW (over 300ms)"
        elif route in ("status_noinfo", "outside_scope", "nonsense") and p50 >= 1000:
            flag = "  <-- SLOW (over 1s)"
        print(f"{route:<16} {len(samples):>3} {p50:>8.1f} {p90:>8.1f} {max(samples):>8.1f} {target:>10}{flag}")

    if args.llm:
        print("\n== raw LLM generation (single call, warm) ==")
        try:
            stats = await _bench_llm()
            print(stats)
        except Exception as exc:  # noqa: BLE001
            print("LLM benchmark failed:", exc)


if __name__ == "__main__":
    asyncio.run(main())