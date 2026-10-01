"""
backend/app/multi_source/synthesize.py

Evidence-grounded synthesis for multi-source answering.

The synthesis step feeds the full evidence pool (grouped by sub-question) to
the existing local LLM (llama3.2) through the SAME shared gate and streaming
generator the main chat flow uses. The system prompt enforces:

  * EVIDENCE > MODEL MEMORY for all university facts;
  * the exact fallback sentence for any sub-question with no evidence;
  * a cautious, non-fabricating answer when sources conflict;
  * the same validation (empty / prompt-parroting / collapse) that protects
    the main chat flow before any text reaches the client.

Streaming with safety: the generation is not fully buffered before release.
The leading tokens are buffered only until they form a validated, grounded
head (non-empty, no prompt echo, no collapse); that head is then yielded and
every following token is streamed to the client as it arrives. A poisoned
head is never released — it collapses to the exact fallback sentence.
"""

from __future__ import annotations

import re
from typing import AsyncGenerator, Sequence

from app.config import settings
from app.ingest.generator import (
    GenerationError,
    stream_answer_async,
)
from app.llm.gate import shared_llm_gate
from app.multi_source.decompose import SubQuery
from app.multi_source.evidence import EvidencePool, ValidationResult

# The EXACT fallback sentence the spec requires whenever evidence is missing.
MISSING_EVIDENCE_FALLBACK = "I don't have information available."

# Minimum validated lead-in before live token streaming starts. A poisoned
# generation echoes the prompt from its VERY FIRST tokens, so buffering this
# many characters and running the parroting/collapse checks on that head lets
# us stream the remainder live while never releasing a poisoned prefix. Short
# answers below the threshold are fully buffered and validated as before.
_STREAM_HEAD_MIN_CHARS = 40
# Hard ceiling: even if no sentence-boundary token ever aligns, release the
# buffered head once it reaches this size so streaming can never stall on a
# tokenizer that splits mid-sentence.
_STREAM_HEAD_MAX_CHARS = 96

_SYSTEM_PROMPT = (
    "You are CUS AI Assistant, the official help desk for Cluster University Srinagar. "
    "The EVIDENCE fragments below are VERIFIED UNIVERSITY EVIDENCE and come from official "
    "Cluster University Srinagar sources: structured programme data, verified examination "
    "pages, verified date sheets and the trusted knowledge base.\n"
    "Rules you must follow:\n"
    "1. EVIDENCE OVER MEMORY. Use the verified evidence as the ONLY source of truth for "
    "university facts: fees, dates, eligibility, subjects, procedures, rules, documents, "
    "or policies. GENERAL MODEL KNOWLEDGE — anything you already know that is NOT in the "
    "evidence — must NEVER be presented as a university fact and must never supply "
    "numbers, dates, names, fees or rules.\n"
    "2. Answer each numbered sub-question separately, in the same order.\n"
    "3. For a sub-question with NO evidence, answer that part EXACTLY with the sentence: "
    f"\"{MISSING_EVIDENCE_FALLBACK}\"\n"
    "4. If the evidence for a sub-question CONFLICTS between sources, do NOT pick a side "
    "and do NOT invent a reconciliation. State the difference clearly and tell the user to "
    "confirm with the university office.\n"
    "5. NEVER invent numbers, dates, names, fees, links or procedures.\n"
    "6. When you use a piece of evidence, cite its source title as [Source: Title].\n"
    "7. Be concise, factual and friendly. Use bullet points when listing items.\n"
    "8. Do NOT mention 'evidence', 'fragments', 'sources list' or 'system' in your reply.\n"
)


def build_question(original_message: str, subs: Sequence[SubQuery]) -> str:
    """Render the numbered synthesis instruction for the LLM."""
    parts = [original_message.strip()]
    for idx, sub in enumerate(subs, start=1):
        parts.append(f"{idx}. {sub.text}")
    return "\n".join(parts)


def format_evidence_block(
    pool: EvidencePool,
    subs: Sequence[SubQuery],
    validation: ValidationResult | None = None,
    context: dict | None = None,
) -> str:
    """Render the evidence pool grouped by sub-question.

    ``context`` is optional and additive, used only by the student-assistant
    intelligent path (P1). Keys currently understood:
      * ``expected_facts``  — numbered list of information needs the synthesis
        must cover (from a bounded information plan);
      * ``mark_status``     — label each item as a current-status source or as
        background/context-only;
      * ``authority_ids``   — object ids of the status-authoritative items;
      * ``conflict_notes``  — sub-questions whose dated official sources
        conflict (never silently pick one).
    With no ``context`` this behaves exactly as before.
    """
    ctx = context or {}
    expected_facts = ctx.get("expected_facts") or ()
    conflicting = set((validation.conflicting or ()) if validation else ())
    conflict_notes = set(ctx.get("conflict_notes") or ())
    authority_ids = set(ctx.get("authority_ids") or ())
    mark_status = bool(ctx.get("mark_status"))

    lines: list[str] = []
    if expected_facts:
        lines.append("Required information needs for this answer:")
        for idx, fact in enumerate(expected_facts, start=1):
            lines.append(f"  {idx}. {fact}")
        lines.append("Cover each need above; for any need with no supporting "
                     "evidence, answer that part EXACTLY with "
                     f"\"{MISSING_EVIDENCE_FALLBACK}\"")
        lines.append("")
    for idx, sub in enumerate(subs, start=1):
        lines.append(f"[Question {idx}: {sub.text}]")
        items = pool.for_sub(sub.text)
        if not items:
            lines.append("  (no official information available for this part)")
        else:
            for item in items:
                title = (item.title or item.source.value).strip()
                if mark_status:
                    label = ("CURRENT OFFICIAL STATUS SOURCE"
                             if id(item) in authority_ids
                             else "BACKGROUND/CONTEXT ONLY — not a "
                                  "current-status source")
                    lines.append(f"- [{label}] Source ({title}): {item.text}")
                else:
                    lines.append(f"- Source ({title}): {item.text}")
        if sub.text in conflicting:
            lines.append("  (note: the evidence above for this part CONFLICTS.)")
        if sub.text in conflict_notes:
            lines.append("  (note: the dated official notices/documents for this "
                         "part CONFLICT — do not silently pick one.)")
        lines.append("")
    return "\n".join(lines)


def _plain(text: str) -> str:
    return " ".join((text or "").lower().replace("'", "").split())


def _parroted_prompt(text: str) -> bool:
    normalized = _plain(text)
    if "answer using only the excerpts" in normalized:
        return True
    if "evidence over memory" in normalized:
        return True
    return re.search(r"\[source\s*\d+\s*:", normalized) is not None


def _collapsed_to_fallback(text: str) -> bool:
    normalized = _plain(text).strip(".!\"")
    fallback = _plain(MISSING_EVIDENCE_FALLBACK).strip(".!\"")
    return normalized == fallback


def _echoed_internal_structure(text: str) -> bool:
    """True when the generation reproduces the INTERNAL answer skeleton
    instead of answering. Three signatures identify it:

      1. the template intro ("here are the answers to the questions ...") that
         the model emits when it re-organises its reply around the numbered
         sub-question list instead of answering;
      2. a numbered sub-question template ("1. ... 2. ...") that also leaks
         the evidence-card citation format ("- Source (Title):" with
         parentheses);
      3. the numbered template combined with the not-found disclaimer.

    A human-format grounded answer never does this: it cites as
    "[Source: Title, Page]" (brackets, no numeric labels) and never prefixes a
    "here are the answers to the numbered questions" re-organisation. Their
    presence therefore means the model copied the internal query/evidence
    template — collapse it exactly like a parroted generation.

    The not-found markers are compared against the apostrophe-STRIPPED
    normalization from :func:`_plain`, so the constants are spelled WITHOUT
    apostrophes (otherwise they could never match a real generation)."""
    normalized = _plain(text)
    if not normalized:
        return False
    # Signature 1: the LEAD-IN template intro ("here are the answers to the
    # questions ...") the model emits when it re-organises its reply around
    # the numbered sub-question list instead of answering. Checked FIRST
    # because the buffered head reaches it before any numbered item appears;
    # flagging it (only when it leads the reply, within the first head window)
    # stops the streaming hole where the head is released live.
    if re.search(
        r"\b(?:here are|below are|following are)\s+(?:the\s+)?answers",
        normalized[:80],
    ):
        return True
    if re.search(r"(?:^|\s)\d+\.\s", normalized) is None:
        return False
    if "- source (" in normalized:
        return True
    if (
        "dont have information available" in normalized
        or "couldnt find this information" in normalized
    ):
        return True
    return False


def _head_is_safe(text: str) -> bool:
    """A buffered lead-in is streamable only when it is a substantial,
    validated answer fragment: non-empty, past the collapse/parroting traps,
    and either ended at a natural sentence boundary (so a cut-off can never
    reach the client) AFTER the minimum length OR grown past the hard ceiling
    (so a tokenizer that never lands on a sentence boundary cannot stall the
    stream). Holding back until this is confirmed means a poisoned generation
    still collapses to the exact fallback instead of leaking its echo."""
    if not text or not text.strip():
        return False
    if _parroted_prompt(text):
        return False
    if _collapsed_to_fallback(text):
        return False
    if _echoed_internal_structure(text):
        return False
    stripped = text.strip()
    if len(stripped) < _STREAM_HEAD_MIN_CHARS:
        return False
    if len(stripped) >= _STREAM_HEAD_MAX_CHARS:
        return True
    return stripped.endswith((".", "?", "!", ":", "\n"))


async def synthesize_answer(
    original_message: str,
    subs: Sequence[SubQuery],
    pool: EvidencePool,
    chat_id: str = "",
    system: str | None = None,
    context: dict | None = None,
) -> AsyncGenerator[str, None]:
    """Yield the validated final answer as a short, safe lead-in followed by
    live token streaming (never a partially validated sentence fragment).

    The first tokens are buffered until ``_head_is_safe`` confirms a real,
    un-poisoned answer fragment; that head is yielded, then every following
    token is streamed to the client as it arrives. A generation that ends
    before the lead-in is confirmed safe (empty / short / prompt-parroting /
    collapse) yields the exact fallback sentence instead.

    Honors the shared LLM gate and MAX_SEMAPHORE_WAIT exactly like run_chat;
    cancels cleanly on gate timeout / generation failure with the exact
    fallback sentence (a failure AFTER streaming started is not overwritten).

    ``system`` optionally overrides the synthesis system prompt (the general
    student-assistant path passes its own mode prompt); the default keeps the
    existing multi-source behaviour unchanged.

    ``context`` optionally carries P1 evidence-labeling hints (status authority
    labels, information-plan expected facts, dated-notice conflicts); default
    ``None`` keeps the previous behaviour byte-for-byte.
    """
    # No evidence anywhere -> deterministic exact fallback, no LLM call.
    if not any(pool.for_sub(sub.text) for sub in subs):
        yield MISSING_EVIDENCE_FALLBACK
        return

    validation = validate_for_synthesis(pool, subs)
    evidence_block = format_evidence_block(pool, subs, validation, context=context)
    question = build_question(original_message, subs)

    acquired = await shared_llm_gate.acquire(timeout=settings.MAX_SEMAPHORE_WAIT)
    if not acquired:
        yield MISSING_EVIDENCE_FALLBACK
        return

    text = ""
    head = ""
    streaming = False
    try:
        async for token in stream_answer_async(
            question, evidence_block, system=system or _SYSTEM_PROMPT
        ):
            text += token
            if streaming:
                yield token
            else:
                head += token
                if _head_is_safe(head):
                    streaming = True
                    yield head
    except GenerationError:
        text = ""
    except Exception:
        text = ""
    finally:
        # Guaranteed slot release on success, failure AND cancellation.
        shared_llm_gate.release()

    # Once streaming started, everything after the validated head was already
    # delivered live; a tail failure must not overwrite it.
    if streaming:
        return

    if not text.strip():
        yield MISSING_EVIDENCE_FALLBACK
    elif _parroted_prompt(text):
        yield MISSING_EVIDENCE_FALLBACK
    elif _collapsed_to_fallback(text):
        yield MISSING_EVIDENCE_FALLBACK
    elif _echoed_internal_structure(text):
        yield MISSING_EVIDENCE_FALLBACK
    else:
        yield text


def validate_for_synthesis(
    pool: EvidencePool,
    subs: Sequence[SubQuery],
) -> ValidationResult:
    """Run the completeness/conflict validation over the evidence pool."""
    from app.multi_source.evidence import validate as _validate

    return _validate(pool, subs)