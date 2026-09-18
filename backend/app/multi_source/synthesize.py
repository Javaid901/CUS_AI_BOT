"""
backend/app/multi_source/synthesize.py

Evidence-grounded synthesis for multi-source answering.

The synthesis step feeds the full evidence pool (grouped by sub-question) to
the existing local LLM (llama3.2) through the SAME shared gate and streaming
generator the main chat flow uses. The system prompt enforces:

  * EVIDENCE > MODEL MEMORY for all university facts;
  * the exact fallback sentence for any sub-question with no evidence;
  * a cautious, non-fabricating answer when sources conflict;
  * the same post-generation validation (empty / prompt-parroting / collapse)
    that protects the main chat flow before any text reaches the client.

Only validated text is yielded — never a partial or poisoned generation.
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
) -> str:
    """Render the evidence pool grouped by sub-question."""
    conflicting = set((validation.conflicting or ()) if validation else ())
    lines: list[str] = []
    for idx, sub in enumerate(subs, start=1):
        lines.append(f"[Question {idx}: {sub.text}]")
        items = pool.for_sub(sub.text)
        if not items:
            lines.append("  (no official information available for this part)")
        else:
            for item in items:
                title = (item.title or item.source.value).strip()
                lines.append(f"- Source ({title}): {item.text}")
        if sub.text in conflicting:
            lines.append("  (note: the evidence above for this part CONFLICTS.)")
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


async def synthesize_answer(
    original_message: str,
    subs: Sequence[SubQuery],
    pool: EvidencePool,
    chat_id: str = "",
) -> AsyncGenerator[str, None]:
    """Yield ONE validated final answer string (never partial/poisoned text).

    Honors the shared LLM gate and MAX_SEMAPHORE_WAIT exactly like run_chat;
    cancels cleanly on gate timeout / generation failure with the exact
    fallback sentence.
    """
    # No evidence anywhere -> deterministic exact fallback, no LLM call.
    if not any(pool.for_sub(sub.text) for sub in subs):
        yield MISSING_EVIDENCE_FALLBACK
        return

    validation = validate_for_synthesis(pool, subs)
    evidence_block = format_evidence_block(pool, subs, validation)
    question = build_question(original_message, subs)

    acquired = await shared_llm_gate.acquire(timeout=settings.MAX_SEMAPHORE_WAIT)
    if not acquired:
        yield MISSING_EVIDENCE_FALLBACK
        return

    text = ""
    try:
        async for token in stream_answer_async(
            question, evidence_block, system=_SYSTEM_PROMPT
        ):
            text += token
    except GenerationError:
        text = ""
    except Exception:
        text = ""
    finally:
        # Guaranteed slot release on success, failure AND cancellation.
        shared_llm_gate.release()

    if not text.strip():
        yield MISSING_EVIDENCE_FALLBACK
    elif _parroted_prompt(text):
        yield MISSING_EVIDENCE_FALLBACK
    elif _collapsed_to_fallback(text):
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