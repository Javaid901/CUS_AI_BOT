"""
Chat conversational-defect regression — leaked internal answer skeleton.

Locks the fix for the "conversational defect": a small local model, when it is
out of its depth, sometimes reproduces the INTERNAL interview skeleton instead
of answering naturally, e.g.::

    Here are the answers to the questions you asked.

    1. I don't have information available.
    2. - Source (ScehemeRegulationsofCUS2021NEP.pdf): This is not a legal
       document.

That numbered-template echo was NOT caught by the existing poison detectors
(the confession detector requires the exact canonical "not in knowledge base"
sentence; the parroting detector requires a BRACKETED "[Source N:" label),
so the numbered skeleton + parenthesised "- Source (Title.pdf):" evidence-card
format leaked to the student verbatim.

The fix adds a dedicated fingerprint ``_echoed_internal_structure``
(synthesize) / ``_llm_echoed_internal_structure`` (run_chat) that treats a
numbered lead combined with the internal evidence-card or disclaimer markers
as poison, holds the streaming head back, and collapses the generation to the
same safe fallback used for parroted/confessed output.

Contract covered here:
  1. run_chat: leaked internal skeleton -> clean fallback (no template echo).
  2. run_chat: a HUMAN-format numbered answer still passes through unchanged.
  3. run_chat: the head gate holds poisoned lead-ins back (no leak, no empty).
  4. synthesize: leaked skeleton -> MISSING_EVIDENCE_FALLBACK.
  5. synthesize: a supported grounded answer still streams unchanged.

Run:  python tests/test_chat_echo_defect.py   (or via pytest)
"""

from __future__ import annotations

import asyncio
import uuid

import pytest

import app.models  # noqa: F401  (register tables before any session)

from app.database import SessionLocal, create_all
from app.chat import service as chat_service
from app.multi_source import synthesize as synth_service

create_all()

CHUNK = {
    "document_id": "doc-1",
    "document_title": "Prospectus.pdf",
    "page_number": 5,
    "chunk_index": 0,
    "rerank_score": 0.9,
    "content": "BCA is a three-year undergraduate programme offered in six semesters.",
}

LEAKED_SKELETON = (
    "Here are the answers to the questions you asked.\n\n"
    "1. I don't have information available.\n"
    "2. - Source (ScehemeRegulationsofCUS2021NEP.pdf): This is not a legal document.\n"
    "3. - Source (AdvicePortal_v1.1.pdf): Please check the official website."
)

HUMAN_NUMBERED = "1. BCA fee is Rs 1200 per annum. 2. The exam fee is Rs 500 per semester."

GROUNDED_ANSWER = "BCA is a three-year degree with six semesters. [Source: Prospectus, Page 5]"


# ---------------------------------------------------------------------------
# run_chat decision layer (retrieval + generation mocked)
# ---------------------------------------------------------------------------

class _Gate:
    def __init__(self):
        self.held = 0
        self.avail = 1

    async def acquire(self, timeout: float = 0.0):
        self.held = 1
        self.avail = 0
        return True

    def release(self):
        self.held = 0
        self.avail = 1


def _drain(message, stream_impl, retrieve_impl=None):
    """Run run_chat with the shared gate stubbed (no cross-test budget)."""
    orig_r, orig_s, orig_g = (
        chat_service.retrieve,
        chat_service.stream_answer_async,
        chat_service.shared_llm_gate,
    )
    chat_service.retrieve = retrieve_impl or (lambda *a, **k: [CHUNK])
    chat_service.stream_answer_async = stream_impl
    chat_service.shared_llm_gate = _Gate()
    db = SessionLocal()
    events: list[dict] = []
    try:
        async def _run():
            async for ev in chat_service.run_chat(db, "test-user", message, None):
                events.append(ev)

        asyncio.run(_run())
        return events
    finally:
        chat_service.retrieve, chat_service.stream_answer_async, chat_service.shared_llm_gate = (
            orig_r, orig_s, orig_g,
        )
        db.close()


def _tokens(events):
    return [e["text"] for e in events if e.get("type") == "token"]


def _cleanup(events):
    done = [e for e in events if e.get("type") == "done"]
    if not done:
        return
    try:
        cid = uuid.UUID(done[0]["chat_id"])
    except (ValueError, TypeError, KeyError):
        return
    from app.models import Conversation, Message

    db = SessionLocal()
    try:
        db.query(Message).filter(Message.conversation_id == cid).delete()
        conv = db.get(Conversation, cid)
        if conv:
            db.delete(conv)
        db.commit()
    finally:
        db.close()


def test_leaked_internal_skeleton_collapses_to_clean_fallback():
    async def leak_stream(*a, **k):
        yield LEAKED_SKELETON

    events = None
    try:
        events = _drain("who is the vice chancellor of CUS?", leak_stream)
        tokens = _tokens(events)
        assert tokens, "leaked skeleton must still produce fallback text"
        assert all(t.strip() for t in tokens), "no empty bubbles"
        joined = " ".join(tokens).lower()
        assert "here are the answers" not in joined
        assert "source (scehemeregulationsofcus2021nep.pdf)" not in joined
        assert "knowledge base" in joined, "collapses to the fallback text"
        assert "done" in [e["type"] for e in events]
    finally:
        if events:
            _cleanup(events)


def test_human_numbered_answer_passes_through_unchanged():
    async def ok_stream(*a, **k):
        yield HUMAN_NUMBERED

    events = None
    try:
        events = _drain("what is the bca fee structure?", ok_stream)
        tokens = _tokens(events)
        assert tokens and tokens[0] == HUMAN_NUMBERED
    finally:
        if events:
            _cleanup(events)


def test_supported_grounded_answer_passes_through_unchanged():
    async def ok_stream(*a, **k):
        yield GROUNDED_ANSWER

    events = None
    try:
        events = _drain("how many semesters does bca have?", ok_stream)
        tokens = _tokens(events)
        assert tokens and tokens[0] == GROUNDED_ANSWER
    finally:
        if events:
            _cleanup(events)


def test_empty_retrieval_deterministic_fallback_without_llm():
    """Mode B acceptance: when retrieval finds NO evidence, run_chat returns the
    deterministic fallback and NEVER touches the LLM gate or the generator —
    the no-information decision is made before any expensive synthesis."""
    captured: dict[str, bool] = {}
    gate = _Gate()

    async def boom_stream(*a, **k):
        captured["llm_stream_called"] = True
        raise AssertionError("LLM must NOT be called when retrieval is empty")

    orig_r, orig_s, orig_g = (
        chat_service.retrieve,
        chat_service.stream_answer_async,
        chat_service.shared_llm_gate,
    )
    chat_service.retrieve = lambda *a, **k: []
    chat_service.stream_answer_async = boom_stream
    chat_service.shared_llm_gate = gate
    db = SessionLocal()
    events: list[dict] = []
    try:
        async def _run():
            async for ev in chat_service.run_chat(db, "test-user",
                                                  "What is the CUS policy on quantum teleportation?",
                                                  None):
                events.append(ev)

        asyncio.run(_run())
        tokens = _tokens(events)
        assert tokens, "empty retrieval still yields honest fallback text"
        assert gate.held == 0, "no LLM gate acquisition when retrieval is empty"
        assert not captured.get("llm_stream_called"), "generator never invoked"
        joined = " ".join(tokens).lower()
        assert "information" in joined, "fallback states the information gap"
        assert "done" in [e["type"] for e in events]
    finally:
        chat_service.retrieve, chat_service.stream_answer_async, chat_service.shared_llm_gate = (
            orig_r, orig_s, orig_g,
        )
        db.close()
    _cleanup(events)


def test_leading_template_intro_alone_is_held_at_the_head_gate():
    # The LIVE leak emits the "Here are the answers to the questions ..."
    # intro BEFORE any numbered item exists in the buffered head. If that
    # lead-in were streamed, the numbered skeleton that follows would also
    # stream uncensored (synthesize_answer returns early once streaming
    # starts). The intro itself must therefore be poison at head level.
    lead = "Here are the answers to the questions about the admission procedure at Cluster University Srinagar:"
    assert chat_service._llm_echoed_internal_structure(lead) is True
    assert synth_service._echoed_internal_structure(lead) is True
    assert chat_service._stream_head_safe(lead) is False
    assert synth_service._head_is_safe(lead) is False

    # A natural answer that merely mentions it found answers (no numbered
    # template, no re-organisation into the sub-question skeleton) must NOT be
    # flagged by the intro signature alone.
    natural = "The answers about the admission procedure are summarised in the sections below."
    assert chat_service._llm_echoed_internal_structure(natural) is False
    assert synth_service._echoed_internal_structure(natural) is False


def test_head_gate_never_releases_a_poisoned_lead():
    # A leak whose EARLY head is a plausible-sounding sentence must still be
    # held back; the final chain collapses it instead of streaming it live.
    sneaky_leak = (
        "The answer to your question is provided below.\n\n"
        "1. There is no official information available.\n"
        "2. - Source (ScehemeRegulationsofCUS2021NEP.pdf): not a document."
    )

    async def leak_stream(*a, **k):
        yield sneaky_leak

    assert chat_service._llm_echoed_internal_structure(sneaky_leak) is True
    assert chat_service._stream_head_safe(sneaky_leak) is False

    events = None
    try:
        events = _drain("what is the admission procedure at cluster university?", leak_stream)
        tokens = _tokens(events)
        assert tokens and all(t.strip() for t in tokens)
        joined = " ".join(tokens).lower()
        assert "source (scehemeregulationsofcus2021nep.pdf)" not in joined
    finally:
        if events:
            _cleanup(events)


# ---------------------------------------------------------------------------
# synthesize decision layer
# ---------------------------------------------------------------------------

def _synth(yielded):
    """Run synthesize_answer with the gate/stream/evidence mocked."""
    async def fake_stream(question, evidence, system=None, **k):
        if isinstance(yielded, list):
            for tok in yielded:
                yield tok
        else:
            yield yielded

    orig_s, orig_g = synth_service.stream_answer_async, synth_service.shared_llm_gate
    synth_service.stream_answer_async = fake_stream
    synth_service.shared_llm_gate = _Gate()

    from app.multi_source.decompose import SourceType, SubQuery
    from app.multi_source.evidence import EvidenceItem, EvidencePool

    pool = EvidencePool()
    pool.add(EvidenceItem(
        sub_question="what is the admission procedure at cluster university srinagar",
        source=SourceType.RAG,
        text="Cluster University of Srinagar is a residential university in J&K.",
        title="ScehemeRegulationsofCUS2021NEP.pdf",
        direct=True,
    ))
    subs = [SubQuery("what is the admission procedure at cluster university srinagar", SourceType.RAG)]

    async def _run():
        return [t async for t in synth_service.synthesize_answer(
            "what is the admission procedure at cluster university srinagar?",
            subs,
            pool,
        )]

    try:
        return asyncio.run(_run())
    finally:
        synth_service.stream_answer_async, synth_service.shared_llm_gate = orig_s, orig_g


def test_synthesize_leaked_skeleton_yields_exact_fallback():
    out = _synth(LEAKED_SKELETON)
    assert out, "must still yield a token"
    joined = "".join(out).lower()
    assert "here are the answers" not in joined
    assert "source (scehemeregulationsofcus2021nep.pdf)" not in joined
    assert out[-1] == synth_service.MISSING_EVIDENCE_FALLBACK


def test_synthesize_grounded_answer_streams_unchanged():
    out = _synth("BCA is a three-year degree. It has six semesters.")
    assert out and "".join(out) == "BCA is a three-year degree. It has six semesters."


def test_synthesize_human_numbered_answer_passes():
    out = _synth(HUMAN_NUMBERED)
    assert out and "".join(out) == HUMAN_NUMBERED


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))