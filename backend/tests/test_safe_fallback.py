"""
Safe information fallback — deterministic, no-fabrication behavior for the
general Q&A (RAG) path of the CUS AI Assistant.

Locks the contract from the safe-information-fallback scope:

  1. Clearly out-of-university-domain questions (general knowledge, weather,
     translation, ...) are answered with the deterministic outside-scope
     fallback — NEVER sent to the LLM, retrieval is skipped.
  2. Supported questions (programme / fee / result / date-sheet / authority
     vocabulary) are never misrouted to the outside-scope response.
  3. An LLM generation that comes back empty or that answers with ONLY the
     "not in knowledge base" sentence is replaced by the full professional
     fallback (text + next steps) — no empty bubbles, no dead-ends.
  4. Empty retrieval still produces the full fallback.

Run:  python tests/test_safe_fallback.py   (or via pytest)
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

import app.models  # noqa: F401  (register tables before any session)

from app.database import SessionLocal, create_all
from app.chat.fallback import FALLBACK_MESSAGE, _is_outside_scope, build_fallback_response
from app.chat import service as chat_service

create_all()

CHUNK = {
    "document_id": "doc-1",
    "document_title": "Prospectus.pdf",
    "page_number": 5,
    "chunk_index": 0,
    "rerank_score": 0.9,
    "content": "BCA is a three-year undergraduate programme offered in six semesters.",
}

OUTSIDE_QUERIES = [
    "What is the capital of France?",
    "what is the weather in srinagar today?",
    "Is it raining in Srinagar?",
    "Tell me a joke",
    "translate this sentence to Hindi",
    "who is the prime minister of India?",
    "give me today's sports score",
    "What is the population of China?",
]

SUPPORTED_QUERIES = [
    "Who is the current vice chancellor of CUS?",
    "Who is the registrar of CUS?",
    "What is the fee structure for MCA?",
    "how many semesters does BCA have?",
    "MCA admission last date?",
    "BA total credits",
    "what is the syllabus for 3rd semester BCA",
    "show my 3rd sem BCA result",
    "where can i check university results?",
    "Who is the sports officer of the college?",
    "tell me about the training and placement cell",
    "give me the result table for BCA",
]


# ---------------------------------------------------------------------------
# 1. Deterministic outside-scope detection
# ---------------------------------------------------------------------------

def test_outside_scope_detection_is_deterministic():
    for q in OUTSIDE_QUERIES:
        assert _is_outside_scope(q), f"expected outside-scope for {q!r}"


def test_supported_queries_are_never_outside_scope():
    # Regression: the guard must not block reachable, supported questions.
    for q in SUPPORTED_QUERIES:
        assert not _is_outside_scope(q), f"wrongly flagged outside-scope: {q!r}"


def test_outside_scope_fallback_has_text_no_card():
    r = build_fallback_response("What is the capital of France?")
    # Professional unavailable text, no invented authority card, next steps.
    assert r.get("text") and "knowledge base" in r["text"].lower()
    assert r.get("card") is None, "outside-scope must never recommend an office"
    assert r.get("options", {}).get("type") == "options"


# ---------------------------------------------------------------------------
# 2. run_chat decision-layer behavior (retrieval + generation mocked)
# ---------------------------------------------------------------------------

def _drain(db, message, retrieve_impl, stream_impl):
    orig_r, orig_s = chat_service.retrieve, chat_service.stream_answer_async
    chat_service.retrieve, chat_service.stream_answer_async = retrieve_impl, stream_impl
    try:
        events: list[dict] = []

        async def _run():
            async for ev in chat_service.run_chat(db, "test-user", message, None):
                events.append(ev)

        asyncio.run(_run())
        return events
    finally:
        chat_service.retrieve, chat_service.stream_answer_async = orig_r, orig_s


def _tokens(events):
    return [e["text"] for e in events if e.get("type") == "token"]


def _cleanup(db, events):
    done = [e for e in events if e.get("type") == "done"]
    if not done:
        return
    try:
        cid = uuid.UUID(done[0]["chat_id"])
    except (ValueError, TypeError, KeyError):
        return
    from app.models import Conversation, Message

    db.query(Message).filter(Message.conversation_id == cid).delete()
    conv = db.get(Conversation, cid)
    if conv:
        db.delete(conv)
    db.commit()


async def _noop_stream(*a, **k):
    if False:  # pragma: no cover - async generator that yields nothing
        yield ""


def test_outside_scope_skips_retrieval_entirely():
    db = SessionLocal()
    events = None
    try:
        def fail_retrieve(*a, **k):
            raise AssertionError("retrieval must NOT run for outside-scope questions")

        events = _drain(db, "What is the capital of France?",
                        retrieve_impl=fail_retrieve,
                        stream_impl=_noop_stream)
        types = [e["type"] for e in events]
        assert "error" not in types
        tokens = _tokens(events)
        assert tokens, "outside-scope must emit a text fallback"
        assert "knowledge base" in tokens[0].lower()
        assert "done" in types
        assert all(t.strip() for t in tokens), "no empty bubbles"
    finally:
        if events:
            _cleanup(db, events)
        db.close()


def test_empty_generation_yields_full_fallback_text():
    db = SessionLocal()
    events = None
    try:
        async def empty_stream(*a, **k):
            if False:  # pragma: no cover - never yields
                yield ""

        events = _drain(db, "BA total credits",
                        retrieve_impl=lambda *a, **k: [CHUNK],
                        stream_impl=empty_stream)
        tokens = _tokens(events)
        assert tokens, "an empty generation must still produce fallback text"
        assert all(t.strip() for t in tokens), "no empty bubbles on empty generation"
        assert "knowledge base" in tokens[0].lower()
    finally:
        if events:
            _cleanup(db, events)
        db.close()


def test_confession_only_generation_keeps_clean_nonempty_text():
    db = SessionLocal()
    events = None
    try:
        async def confession_stream(*a, **k):
            yield "I couldn't find this information in the Cluster University Srinagar knowledge base."

        events = _drain(db, "Who is the vice chancellor of CUS?",
                        retrieve_impl=lambda *a, **k: [CHUNK],
                        stream_impl=confession_stream)
        tokens = _tokens(events)
        assert tokens, "a bare confession must never be left as an empty bubble"
        assert all(t.strip() for t in tokens)
        assert tokens[0].lower().startswith("i couldn't find this information"), tokens[0]
        assert "done" in [e["type"] for e in events]
    finally:
        if events:
            _cleanup(db, events)
        db.close()


def test_empty_retrieval_yields_full_fallback_text():
    db = SessionLocal()
    events = None
    try:
        events = _drain(db, "syllabus of 3rd semester BCA",
                        retrieve_impl=lambda *a, **k: [],
                        stream_impl=_noop_stream)
        types = [e["type"] for e in events]
        assert "error" not in types
        tokens = _tokens(events)
        assert tokens and tokens[0].startswith(FALLBACK_MESSAGE)
    finally:
        if events:
            _cleanup(db, events)
        db.close()


def test_supported_query_answer_passes_through_unchanged():
    db = SessionLocal()
    events = None
    try:
        answer = "BCA is a three-year degree with six semesters. [Source: Prospectus, Page 5]"

        async def ok_stream(*a, **k):
            yield answer

        events = _drain(db, "how many semesters does BCA have?",
                        retrieve_impl=lambda *a, **k: [CHUNK],
                        stream_impl=ok_stream)
        tokens = _tokens(events)
        # Supported + valid generation must NOT be replaced by the fallback.
        assert tokens and tokens[0] == answer
    finally:
        if events:
            _cleanup(db, events)
        db.close()


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))