"""
backend/app/multi_source/evidence.py

Request-scoped evidence collection + validation for multi-source answering.

For every decomposed sub-question the collector queries the MOST authoritative
source for that fragment:

  PROGRAMME   → structured ProgrammeFacts (eligibility / duration / subjects /
                credits / fee structure / documents) — NEVER substitutes data.
  EXAMINATION → verified official examination pages (exam fee / division
                improvement / model papers) via the dedicated examination
                service — the same zero-hallucination gate the main flow uses.
  NOTICES     → VERIFIED + PUBLISHED date-sheet entries only.
  RAG         → the existing hybrid Chroma + BM25 retriever (bounded top-k),
                run in a worker thread exactly like the main chat flow.

All evidence carries provenance (source class, title/id, text, relevance,
confidence) so the synthesis step can ground the answer AND be honest about
what it does NOT know.

Structured data is authoritative: RAG evidence may supplement it but never
overrides it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Sequence

from app.multi_source.decompose import SourceType, SubQuery

# Bounded retrieval top-k per RAG fragment (never the full settings.TOP_K).
_RAG_TOP_K = 4


@dataclass(frozen=True)
class EvidenceItem:
    """One grounded fact fragment plus its provenance."""

    sub_question: str
    source: SourceType
    text: str
    title: str = ""
    source_id: str = ""
    relevance: float = 1.0
    confidence: float = 1.0
    direct: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "sub_question": self.sub_question,
            "source": self.source.value,
            "text": self.text,
            "title": self.title,
            "source_id": self.source_id,
            "relevance": self.relevance,
            "confidence": self.confidence,
            "direct": self.direct,
        }


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


@dataclass
class EvidencePool:
    """In-memory, request-scoped evidence pool with light deduplication."""

    items: list[EvidenceItem] = field(default_factory=list)

    def add(self, item: EvidenceItem) -> None:
        norm = _norm(item.text)
        if not norm:
            return
        for existing in self.items:
            if (
                existing.sub_question == item.sub_question
                and existing.source == item.source
                and _norm(existing.text) == norm
            ):
                return
        self.items.append(item)

    def add_many(self, items: Sequence[EvidenceItem]) -> None:
        for item in items:
            self.add(item)

    def for_sub(self, sub_question: str) -> list[EvidenceItem]:
        return [i for i in self.items if i.sub_question == sub_question]

    def total(self) -> int:
        return len(self.items)


@dataclass(frozen=True)
class ValidationResult:
    """Per-sub-question evidence completeness status."""

    covered: tuple[str, ...]
    missing: tuple[str, ...]
    conflicting: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "covered": list(self.covered),
            "missing": list(self.missing),
            "conflicting": list(self.conflicting),
        }


def validate(pool: EvidencePool, subs: Sequence[SubQuery]) -> ValidationResult:
    """Classify each sub-question as covered / missing / conflicting.

    Conflict detection is deliberately coarse: when a sub-question has direct
    answers from two different source classes whose text disagrees, we flag it
    so the synthesis prompt can demand a cautious, non-fabricating answer.
    """
    covered: list[str] = []
    missing: list[str] = []
    conflicting: list[str] = []

    for sub in subs:
        items = pool.for_sub(sub.text)
        direct = [i for i in items if i.direct]
        if not direct:
            missing.append(sub.text)
            continue
        covered.append(sub.text)
        # Cross-source conflict: two or more distinct source classes (e.g.
        # structured programme facts vs. RAG) give divergent text for the same
        # sub-question -> the synthesis prompt must flag it as a conflict.
        by_source: dict[str, set[str]] = {}
        for item in direct:
            by_source.setdefault(item.source.value, set()).add(_norm(item.text))
        all_norms: set[str] = set()
        for norms in by_source.values():
            all_norms.update(norms)
        if len(by_source) >= 2 and len(all_norms) >= 2:
            conflicting.append(sub.text)
    return ValidationResult(
        covered=tuple(covered),
        missing=tuple(missing),
        conflicting=tuple(conflicting),
    )


# ---------------------------------------------------------------------------
# Structured collectors (PROGRAMME)
# ---------------------------------------------------------------------------

_PROGRAMME_FIELD_MAP = {
    "eligibility": ("eligibility", "Eligibility"),
    "duration": ("duration_years", "Duration"),
    "subjects": ("subjects", "Subjects"),
    "credits": ("total_credits", "Total Credits"),
    "documents": ("linked_documents", "Documents"),
    "fee": ("fee_structure", "Fee Structure"),
}


def _programme_ref(sub: SubQuery, entities: Any, ctx: Any) -> str | None:
    """Resolve the programme a programme sub-question refers to."""
    from app.examination.metadata import extract_programme

    for candidate in (sub.text, getattr(entities, "programme", None) or "",
                      getattr(ctx, "programme", None) or ""):
        prog = extract_programme(candidate)
        if prog:
            return prog
    return None


def _format_fee(fee_structure: Sequence[dict[str, Any]] | tuple) -> str:
    if not fee_structure:
        return ""
    lines = []
    for entry in fee_structure:
        amount = entry.get("amount")
        label = entry.get("label") or entry.get("category") or ""
        lines.append(f"{label}: {amount}" if label and amount else (str(entry) if entry else ""))
    return "; ".join(l for l in lines if l) or ""


def _evidence_from_programme(
    sub: SubQuery,
    db: Any,
    entities: Any,
    ctx: Any,
) -> list[EvidenceItem]:
    from app.catalogue.facts import get_programme_facts

    prog = _programme_ref(sub, entities, ctx)
    if not prog:
        return []
    facts = get_programme_facts(prog, db=db)
    if facts is None:
        return []

    items: list[EvidenceItem] = []
    field_matches: list[tuple[str, str]] = []  # (attr, display)
    from app.multi_source.decompose import _PROGRAMME_ATTR_RES
    for attr, regex in _PROGRAMME_ATTR_RES.items():
        if regex.search(sub.text):
            display = _PROGRAMME_FIELD_MAP.get(attr, (None, attr.capitalize()))[1]
            field_matches.append((attr, display))

    if not field_matches:
        # No explicit attribute — expose the core overview facts.
        field_matches = [("eligibility", "Eligibility"), ("duration", "Duration"),
                         ("subjects", "Subjects"), ("fee", "Fee Structure")]

    for attr, display in field_matches:
        value = getattr(facts, _PROGRAMME_FIELD_MAP[attr][0], None)
        if attr == "duration":
            text = f"{value} year(s)" if value else ""
        elif attr == "subjects":
            if not value:
                text = ""
            else:
                text = ", ".join(
                    f"{s.get('subject_name') or s.get('subject_code')}" for s in value
                ) or f"{facts.subject_count} subjects"
        elif attr == "fee":
            text = _format_fee(value)
        elif attr == "documents":
            text = ", ".join(d.get("title") or "" for d in (value or ())) if value else ""
        else:
            text = str(value) if value not in (None, "") else ""
        if not text:
            continue
        items.append(EvidenceItem(
            sub_question=sub.text,
            source=SourceType.PROGRAMME,
            text=f"{facts.name} — {display}: {text}",
            title=f"[{prog.upper()}] {facts.name} (structured catalogue)",
            source_id=facts.programme_id,
            relevance=1.0,
            confidence=1.0,
            direct=True,
        ))
    return items


# ---------------------------------------------------------------------------
# Structured collectors (EXAMINATION / NOTICES)
# ---------------------------------------------------------------------------

def _semester_ref(sub: SubQuery, entities: Any, ctx: Any) -> int | None:
    """Resolve the semester a sub-question refers to (sub-text > entities > ctx)."""
    from app.examination.metadata import normalize_semester

    sem = normalize_semester(sub.text)
    if sem is not None:
        return sem
    for candidate in (getattr(entities, "semester", None), getattr(ctx, "semester", None)):
        if candidate not in (None, ""):
            return candidate
    return None


def _evidence_from_examination(
    sub: SubQuery,
    db: Any,
    entities: Any = None,
    ctx: Any = None,
) -> list[EvidenceItem]:
    from app.examination.service import (
        division_improvement_source,
        exam_fee_source,
        list_model_papers,
    )

    items: list[EvidenceItem] = []
    text = sub.text

    source = None
    if _has_any("division", "improve", text=text):
        source = division_improvement_source(db)
        label = "Division Improvement (official)"
    if source is None and _has_any("exam", "fee", "charge", text=text):
        source = exam_fee_source(db)
        label = "Examination Fee (official)"
    if source is None and _has_any("model paper", "previous year", "question paper", text=text):
        # NO SUBSTITUTION: when the sub-question names a programme/semester, the
        # verified model-paper list MUST honour it. If nothing matches, return
        # no evidence (the synthesis step then emits the exact fallback) rather
        # than listing an unrelated programme's papers.
        prog = _programme_ref(sub, entities, ctx)
        sem = _semester_ref(sub, entities, ctx)
        constraints = {"programme": prog, "semester": sem}
        try:
            papers = list_model_papers(db, **constraints)[:3]
        except Exception:
            # A single bad row must degrade THIS part to "no evidence" (the
            # honest fallback) instead of failing the whole multi-source answer.
            papers = []
        if papers:
            titles = [str(p.get("title") or "").strip() for p in papers]
            scope = ", ".join(
                label for label in (
                    prog.upper() if prog else "",
                    f"semester {sem}" if sem is not None else "",
                ) if label
            )
            prefix = f"Available model papers ({scope}): " if scope else "Available model papers: "
            items.append(EvidenceItem(
                sub_question=text,
                source=SourceType.EXAMINATION,
                text=prefix + "; ".join(t for t in titles if t),
                title="Model papers (verified)",
                source_id="examination:model_papers",
                relevance=1.0,
                confidence=1.0,
                direct=True,
            ))
        return items

    if source and (source.get("content") or "").strip():
        content = (source.get("content") or "").strip()
        items.append(EvidenceItem(
            sub_question=text,
            source=SourceType.EXAMINATION,
            text=content[:1400],
            title=str(source.get("title") or label),
            source_id=str(source.get("url") or "examination:official"),
            relevance=1.0,
            confidence=1.0,
            direct=True,
        ))
    return items


def _evidence_from_notices(sub: SubQuery, db: Any, entities: Any, ctx: Any) -> list[EvidenceItem]:
    from app.notices.service import get_verified_schedule, list_notices

    from app.examination.metadata import extract_programme
    # Resolve scope from the most specific source first: the sub-question text,
    # then the whole-message entities, then prior conversation context. This
    # keeps "MCA 3rd semester" from the message preamble applied to every
    # decomposed part (the sub-question itself may not repeat it).
    programme = (
        extract_programme(sub.text)
        or (getattr(entities, "programme", None) if entities is not None else None)
        or getattr(ctx, "programme", None)
    )
    semester = getattr(ctx, "semester", None)
    from app.examination.metadata import normalize_semester
    sem = normalize_semester(sub.text)
    if sem is None and entities is not None:
        sem = getattr(entities, "semester", None)
    if sem is not None:
        semester = sem

    try:
        notices = list_notices(db, published_only=True, programme=programme, limit=5)
    except Exception:
        notices = []
    if not notices:
        return []
    try:
        rows = get_verified_schedule(
            db,
            [n.id for n in notices],
            programme=programme,
            semester=semester,
            limit=12,
        )
    except Exception:
        rows = []
    if not rows:
        return []

    lines: list[str] = []
    for row in rows[:10]:
        parts = []
        if getattr(row, "programme_id", None):
            parts.append(row.programme_id.upper())
        if getattr(row, "semester", None):
            parts.append(f"semester {row.semester}")
        if getattr(row, "subject", None):
            parts.append(str(row.subject))
        elif getattr(row, "subject_name", None):
            parts.append(str(row.subject_name))
        if getattr(row, "exam_date", None):
            parts.append(str(row.exam_date))
        if getattr(row, "day", None):
            parts.append(str(row.day))
        if getattr(row, "start_time", None):
            time_label = str(row.start_time)
            if getattr(row, "end_time", None):
                time_label = f"{time_label}-{row.end_time}"
            parts.append(time_label)
        if parts:
            lines.append(" — ".join(parts))

    title = ""
    if notices:
        title = str(getattr(notices[0], "title", None) or "Date sheet")

    if not lines:
        return []

    items: list[EvidenceItem] = []
    items.append(EvidenceItem(
        sub_question=sub.text,
        source=SourceType.NOTICES,
        text=" (verified date sheet) ".join(lines)[:1400],
        title=title,
        source_id="notices:date_sheet",
        relevance=1.0,
        confidence=1.0,
        direct=True,
    ))
    return items


# ---------------------------------------------------------------------------
# RAG collector
# ---------------------------------------------------------------------------

def _has_any(*needles: str, text: str) -> bool:
    low = text.lower()
    return any(n in low for n in needles)


def _evidence_from_rag(
    sub: SubQuery,
    rag_ctx: dict[str, Any] | None = None,
    top_k: int = _RAG_TOP_K,
) -> list[EvidenceItem]:
    """Documentary evidence for one fragment via the existing hybrid retriever."""
    from app.ingest.retrieve import retrieve

    try:
        chunks = retrieve(sub.text, top_k=top_k, context=rag_ctx or {})
    except Exception:
        return []
    items: list[EvidenceItem] = []
    for chunk in chunks or []:
        content = str(chunk.get("content") or "").strip()
        if not content:
            continue
        # The retriever never sets a `_score` key; read the same ordered score
        # fields the main chat flow uses so relevance is not stuck at 0.0.
        score = 0.0
        for key in ("rerank_score", "combined_score", "embedding_score"):
            val = chunk.get(key)
            if val is not None:
                score = float(val)
                break
        title = str(chunk.get("document_title") or chunk.get("source") or "Document")
        items.append(EvidenceItem(
            sub_question=sub.text,
            source=SourceType.RAG,
            text=content[:900] if len(content) > 900 else content,
            title=title,
            source_id=str(chunk.get("document_id") or chunk.get("source_id") or ""),
            relevance=max(0.0, min(1.0, score)),
            confidence=1.0,
            direct=True,
        ))
    return items


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def collect_evidence(
    db: Any,
    subs: Sequence[SubQuery],
    entities: Any,
    ctx: Any,
    rag_ctx: dict[str, Any] | None = None,
) -> EvidencePool:
    """Collect evidence for every sub-question, one bounded retrieval per RAG
    fragment. Structured collectors run in the current event-loop thread (fast
    bounded SQL reads, exactly like the existing structured handlers); the
    hybrid retriever — which can embed + refresh BM25 — runs in a worker
    thread exactly like the main chat flow."""
    import asyncio

    pool = EvidencePool()
    for sub in subs:
        if sub.source == SourceType.PROGRAMME:
            pool.add_many(_evidence_from_programme(sub, db, entities, ctx))
        elif sub.source == SourceType.EXAMINATION:
            pool.add_many(_evidence_from_examination(sub, db, entities, ctx))
        elif sub.source == SourceType.NOTICES:
            pool.add_many(_evidence_from_notices(sub, db, entities, ctx))
        elif sub.source == SourceType.RAG:
            try:
                items = await asyncio.to_thread(_evidence_from_rag, sub, rag_ctx or {})
            except Exception:
                items = []
            pool.add_many(items)
    return pool