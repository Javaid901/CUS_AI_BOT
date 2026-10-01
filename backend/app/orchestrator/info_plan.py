"""
backend/app/orchestrator/info_plan.py

P1-B — Bounded information planning inside the EXISTING intelligent path.

This is NOT a second planner and NOT a second LLM service. The existing
deterministic planner remains the routing authority; this step is a small,
optional refinement that runs ONLY for genuinely complex intelligent
knowledge questions. Its purpose is a single question: "WHAT information does
this particular question require before we can answer it?"

Design rules:
  * Guarded — `should_information_plan` fires only for multi-aspect complex
    knowledge questions (compound structure + several information needs), so
    simple procedures, status questions and every protected/deterministic
    route keep their existing call counts.
  * Small & structured — the plan is JSON-only, low-token, bounded
    (mode + needs_current + ≤6 required facts + ≤5 source preferences).
  * Safely degradeable — malformed JSON, unknown values, a busy gate, a
    generation failure or a timeout all return ``None``; the engine then runs
    the exact P0 intelligent behavior. A plan failure can never become a chat
    failure and never costs more than ONE extra LLM call.
  * No new powers — the LLM never chooses a protected route, never generates
    URLs, never invents source names and never requests unbounded retrieval.
    The deterministic evidence layer decides HOW information is collected.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from app.config import settings
from app.ingest.generator import GenerationError, stream_answer_async
from app.ingest.prompts import INFORMATION_PLAN_SYSTEM_PROMPT
from app.llm.gate import shared_llm_gate
from app.multi_source.decompose import SourceType, SubQuery

# Hard bounds — the plan can never request more than this.
_MAX_REQUIRED_FACTS = 6
_MAX_SOURCE_PREFS = 5
_MAX_FACT_SUBS = 4
# The extra plan LLM call is abandoned after this long (generation only).
_PLAN_TIMEOUT = 45.0

_ALLOWED_MODES = frozenset({
    "fact", "procedure", "status", "document", "comparison", "general",
})
_ALLOWED_FACTS = frozenset({
    "eligibility", "documents", "fee", "application_process", "deadline",
    "selection", "duration", "requirements",
})
_ALLOWED_SOURCES = frozenset({
    "programme", "notices", "documents", "website", "rag",
})

_PLAN_MODE_ALIASES = {"document": "comparison"}


@dataclass(frozen=True)
class InfoPlan:
    """A few bounded answers to 'what does this question need?'."""

    mode: str
    needs_current: bool = False
    required_facts: tuple[str, ...] = ()
    source_preferences: tuple[str, ...] = (
        "programme", "notices", "documents", "website", "rag",
    )

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "needs_current": self.needs_current,
            "required_facts": list(self.required_facts),
            "source_preferences": list(self.source_preferences),
        }


# ---------------------------------------------------------------------------
# Deterministic guard — only genuinely complex knowledge questions plan
# ---------------------------------------------------------------------------

_FACT_MARKERS: tuple[tuple[str, re.Pattern], ...] = (
    ("eligibility", re.compile(r"\beligibl", re.IGNORECASE)),
    ("documents", re.compile(r"\bdocuments?\b|\bpaperwork\b|required docs?", re.IGNORECASE)),
    ("fee", re.compile(r"\bfees?\b|\btuition\b|\bcharges?\b|\bamount\b|fee structure", re.IGNORECASE)),
    ("application_process", re.compile(
        r"\bapply|application|\bprocedure\b|\bprocess\b|\bsteps?\b|step-?by-?step|how to",
        re.IGNORECASE,
    )),
    ("deadline", re.compile(r"\blast ?date\b|\bdeadline\b|\bdue ?date\b|closing ?date\b|last day", re.IGNORECASE)),
    ("selection", re.compile(r"\bselection\b|\bcounselling\b|\bcounseling\b|\bmerit\b|entrance test", re.IGNORECASE)),
    ("duration", re.compile(r"\bduration\b|how long|how many years", re.IGNORECASE)),
    ("requirements", re.compile(r"\bre?quirements?\b|\bcriteria\b|\bqualifications?\b", re.IGNORECASE)),
)

_COMPOUND_SEP = re.compile(r"\s+and\b|\s*,\s*|\s*;\s*|\balso\b|\bincluding\b", re.IGNORECASE)


def should_information_plan(message: str, kind: str = "knowledge") -> bool:
    """True ONLY for genuinely complex intelligent knowledge questions.

    Requires at least three distinct information-need markers, or a compound
    (conjoined) question carrying at least two. Simple single-aspect questions
    ("explain the mca admission procedure", "how do i apply for mca") return
    False and keep the exact P0 call count.
    """
    if kind != "knowledge":
        return False
    text = str(message or "").strip()
    if not text:
        return False
    found = {
        name for name, rx in _FACT_MARKERS if rx.search(text)
    }
    if len(found) >= 3:
        return True
    return bool(len(found) >= 2 and _COMPOUND_SEP.search(text))


# ---------------------------------------------------------------------------
# Defensive parsing — a bad response degrades, never derails
# ---------------------------------------------------------------------------

def parse_info_plan(raw: str) -> InfoPlan | None:
    """Parse and strictly validate a JSON-only plan response.

    Returns ``None`` (caller falls back to P0 behavior) on malformed JSON,
    an unknown mode, an excessive/unrecognized fact or an unknown source.
    Surplus/duplicate entries are trimmed silently when within bounds.
    """
    text = (raw or "").strip()
    if not text:
        return None
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group(0))
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None

    mode = str(data.get("mode") or "").strip().lower()
    mode = _PLAN_MODE_ALIASES.get(mode, mode)
    if mode not in _ALLOWED_MODES:
        return None

    has_facts = data.get("required_facts") is not None
    facts_raw = data.get("required_facts") or []
    if not isinstance(facts_raw, list) or len(facts_raw) > _MAX_REQUIRED_FACTS:
        return None
    facts: list[str] = []
    for f in facts_raw:
        val = str(f or "").strip().lower()
        if val not in _ALLOWED_FACTS:
            return None
        if val not in facts:
            facts.append(val)
    if has_facts and not facts:
        return None

    has_sources = data.get("source_preferences") is not None
    sources_raw = data.get("source_preferences") or []
    if not isinstance(sources_raw, list) or len(sources_raw) > _MAX_SOURCE_PREFS:
        return None
    sources: list[str] = []
    for s in sources_raw:
        val = str(s or "").strip().lower()
        if val not in _ALLOWED_SOURCES:
            return None
        if val not in sources:
            sources.append(val)
    if has_sources and not sources:
        return None

    return InfoPlan(
        mode=mode,
        needs_current=bool(data.get("needs_current")),
        required_facts=tuple(facts),
        source_preferences=tuple(sources) or ("programme", "notices",
                                               "documents", "website", "rag"),
    )


# ---------------------------------------------------------------------------
# Applying the plan — what the deterministic layer may collect differently
# ---------------------------------------------------------------------------

def expand_subs_with_plan(
    subs: Sequence[SubQuery],
    plan: InfoPlan,
    question: str,
) -> list[SubQuery]:
    """Extend the intelligent evidence sub-questions with bounded per-fact
    retrieval fragments (RAG only). Never adds more than ``_MAX_FACT_SUBS``
    and never removes a source the deterministic layer already collects."""
    extended = list(subs)
    for fact in list(plan.required_facts)[:_MAX_FACT_SUBS]:
        extended.append(SubQuery(
            text=f"Official university knowledge base detail on {fact} for: {question}",
            source=SourceType.RAG,
        ))
    return extended


# ---------------------------------------------------------------------------
# The call itself — the SAME gate, generator and timeout rules as synthesis
# ---------------------------------------------------------------------------

async def plan_information(question: str, programme: str | None = None) -> InfoPlan | None:
    """Run ONE bounded, JSON-only information-plan call over the existing LLM.

    Any failure (gate busy, generation error, malformed output, timeout)
    returns ``None`` so the engine falls back to the exact P0 behavior.
    """
    prompt_question = question.strip()
    if programme:
        prompt_question = f"{programme.upper()}: {question}"
    if not prompt_question:
        return None

    acquired = await shared_llm_gate.acquire(timeout=settings.MAX_SEMAPHORE_WAIT)
    if not acquired:
        return None

    text = ""
    got_timeout = False
    try:
        async def _collect() -> str:
            parts: list[str] = []
            async for token in stream_answer_async(
                f"Plan information for this university question: {prompt_question}",
                "",
                system=INFORMATION_PLAN_SYSTEM_PROMPT,
            ):
                parts.append(token)
            return "".join(parts)

        try:
            text = await asyncio.wait_for(_collect(), timeout=_PLAN_TIMEOUT)
        except asyncio.TimeoutError:
            got_timeout = True
            text = ""
    except GenerationError:
        text = ""
    except Exception:
        text = ""
    finally:
        shared_llm_gate.release()

    if got_timeout or not text.strip():
        return None
    return parse_info_plan(text)