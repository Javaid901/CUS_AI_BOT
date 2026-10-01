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
    # --- P0: general student-assistant provenance metadata ---
    # All optional and appended AFTER the original fields so every existing
    # positional call site keeps its meaning.
    url: str = ""            # verified https URL when one exists (never craft)
    issued_at: str = ""      # ISO date when the fact was published
    last_synced: str = ""    # ISO date when a source was last synced
    verified: bool = False   # True when the underlying record is admin-verified
    source_label: str = ""   # human label of the provenance (e.g. document type)
    programme: str = ""      # programme id this record belongs to (if any)
    semester: str = ""       # semester this record belongs to (if any)
    batch: str = ""          # academic batch this record belongs to (if any)
    doc_id: str = ""         # canonical document id (UniversityDocument / page)

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
            "url": self.url,
            "issued_at": self.issued_at,
            "last_synced": self.last_synced,
            "verified": self.verified,
            "source_label": self.source_label,
            "programme": self.programme,
            "semester": self.semester,
            "batch": self.batch,
            "doc_id": self.doc_id,
        }


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _https_only(url: str | None) -> str:
    """Return the URL only when it is a verified https address (never invent)."""
    url = (url or "").strip()
    return url if url.startswith("https://") else ""


def _norm_programme(programme: str | None) -> str | None:
    return str(programme or "").strip().lower() or None


_STOPWORDS = frozenset({
    "what", "when", "how", "why", "who", "which", "where", "is", "are", "was",
    "were", "do", "does", "did", "can", "could", "will", "would", "should",
    "the", "a", "an", "of", "to", "for", "in", "on", "at", "and", "or", "but",
    "with", "from", "about", "please", "me", "my", "i", "you", "yes", "no",
    "tell", "show", "give", "list", "have", "has", "been", "released", "open",
    "being", "your", "this", "that", "it", "its",
})


def _sig_tokens(text: str, limit: int = 4) -> list[str]:
    """Significant tokens from the message used as a bounded keyword probe."""
    seen: list[str] = []
    for tok in re.findall(r"[a-z0-9]+", (text or "").lower()):
        if len(tok) < 3 or tok in _STOPWORDS:
            continue
        if tok not in seen:
            seen.append(tok)
        if len(seen) >= limit:
            break
    return seen


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
# P1 — Current-status evidence isolation
# ---------------------------------------------------------------------------
#
# A current-status question ("is admission open?", "has the result been
# declared?", "which notice is newer?") must be answered ONLY from evidence
# that can actually establish the CURRENT state of something. General RAG
# knowledge and programme-profile facts (eligibility / fee / duration) can be
# true and still say nothing about whether something is open / released /
# announced TODAY — so they can never act as status authority.
#
# These helpers are deterministic, use ONLY metadata the evidence already
# carries, and never touch the retrieval layer. They add a status/currentness
# lens to the existing intelligent path; ordinary evidence behavior is
# unchanged for non-status modes.

# UniversityDocument doc_type values that are announcement-shaped (they can
# communicate a current state) as opposed to evergreen reference material
# ('other_official_document' regulations / schemes / syllabi).
_STATUS_AUTHORITY_DOC_TYPES = frozenset({"official_notification"})


def _item_status_subjects(item: EvidenceItem) -> frozenset[str]:
    """Status-capable subjects an evidence item can establish status for,
    derived from its own provenance (title / text / source label)."""
    from app.orchestrator.current_status import status_subjects_of

    hay = " ".join((
        item.title or "",
        item.text or "",
        item.source_label or "",
    ))
    return status_subjects_of(hay)


def is_status_authority(
    item: EvidenceItem,
    status_subjects: frozenset[str] | None = None,
) -> bool:
    """True when this evidence item is capable of establishing current status.

    Deterministic rules:
      * verified/published university NOTICES are announcement-shaped by
        construction -> authority (P2-B: requires ``verified``).
      * official UniversityDocuments are authority ONLY when they are dated
        AND classified ``official_notification`` (an actual announcement);
        evergreen regulations / schemes (``other_official_document``) are NOT.
      * verified official WebsitePages are authority only when they carry a
        sync date (a dated, currently-known page).
      * structured programme facts and general RAG knowledge are NEVER status
        authority (they may be true background but cannot prove an open /
        released / announced state).

    ``status_subjects`` is the P2-B subject-alignment lens: when provided (and
    non-empty), an item is authority ONLY when it concerns the SAME subject the
    question asks about. An exam-fee notice therefore can never establish that
    ``admission`` is open, and vice versa. When ``None``/empty (the P0/P1
    direct-call contract) alignment is skipped and the pre-P2 behavior is
    preserved exactly.
    """
    if status_subjects and not status_subjects & _item_status_subjects(item):
        return False
    if item.source == SourceType.NOTICES:
        return bool(item.verified)
    if item.source == SourceType.DOCUMENTS:
        return bool(item.issued_at) and item.source_label in _STATUS_AUTHORITY_DOC_TYPES
    if item.source == SourceType.WEBSITE:
        return bool(item.issued_at) and bool(item.verified)
    return False


def filter_status_evidence(
    items: Sequence[EvidenceItem],
    kind: str = "status",
    status_subjects: frozenset[str] | None = None,
) -> tuple[list[EvidenceItem], list[EvidenceItem]]:
    """Partition evidence into status-authoritative vs context-only items.

    In ANY non-status mode this is a no-op (``(all_items, [])``) so ordinary
    intelligent behavior is preserved exactly. In status mode it returns
    ``(authority, context_only)`` where only ``authority`` may ground a
    current-state claim; ``context_only`` (RAG snippets, programme-profile
    facts, undated reference documents) is still supplied to the synthesis as
    background with an explicit "not a current-status source" label.

    ``status_subjects`` (P2-B) aligns every authority check to the subjects the
    question actually asks about; pass ``None`` to keep the pure P0/P1
    partitioning.
    """
    if kind != "status":
        return list(items), []
    authority: list[EvidenceItem] = []
    context_only: list[EvidenceItem] = []
    for item in items:
        (authority if is_status_authority(item, status_subjects) else context_only).append(item)
    return authority, context_only


def dated_notice_conflicts(pool: EvidencePool) -> list[str]:
    """Sub-questions backed by ≥2 DIFFERENT dated official sources.

    Scoped to announcement-shaped, DATED evidence only (verified notices and
    official-notification documents) so a schedule row plus its notice frame
    can never produce a false conflict. Returns the sub-question texts whose
    dated official sources disagree, so the synthesis prompt can surface the
    conflict instead of silently picking a winner.
    """
    by_sub: dict[str, set[tuple[str, str, str]]] = {}
    for item in pool.items:
        if item.source not in (SourceType.NOTICES, SourceType.DOCUMENTS):
            continue
        if item.source == SourceType.DOCUMENTS and item.source_label not in _STATUS_AUTHORITY_DOC_TYPES:
            continue
        if not (item.issued_at and item.title):
            continue
        by_sub.setdefault(item.sub_question, set()).add(
            (item.title, item.issued_at, _norm(item.text))
        )
    return [sub for sub, rows in by_sub.items() if len(rows) >= 2]


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
    # P2-D/P2-B subject alignment: a status or deadline sub-question may be
    # grounded only in official notices about the SAME subject. An exam backlog
    # notice can never establish an admission deadline; when no notice matches
    # the subject, NO notice evidence is returned (the honest fallback) instead
    # of a wrong-subject answer. Deadline sub-questions without an explicit
    # subject keep the programme-scoped list.
    from app.multi_source.decompose import is_deadline_text
    from app.orchestrator.current_status import status_subjects_of

    qsubs = frozenset(status_subjects_of(sub.text))
    if qsubs or is_deadline_text(sub.text):
        if qsubs:
            aligned = [
                n for n in notices
                if qsubs & status_subjects_of(
                    f"{getattr(n, 'title', '') or ''} {getattr(n, 'notice_type', '') or ''}"
                )
            ]
            if not aligned:
                return []
            notices = aligned
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

    lines: list[str] = []
    for row in (rows or [])[:10]:
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

    items: list[EvidenceItem] = []
    if lines:
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
    # Notice-frame evidence: the newest verified + published notice title/date.
    # Additive only — it never replaces schedule rows and never invents a URL
    # (notices are attached files, not https pages). This is what lets
    # current-status questions ("has the notification been issued?") be answered
    # from the authoritative notice metadata even with no schedule rows.
    items.extend(_notice_frame_items(sub, notices))
    return items


def _notice_frame_items(sub: SubQuery, notices: Sequence[Any]) -> list[EvidenceItem]:
    """Notice-level framing evidence from verified + published notices."""
    if not notices:
        return []

    def _key(n: Any):
        for attr in ("published_at", "notification_date", "created_at"):
            value = getattr(n, attr, None)
            if value is not None:
                return value
        return None

    newest = None
    for n in notices:
        key = _key(n)
        if key is None:
            continue
        if newest is None or key > _key(newest):
            newest = n
    if newest is None:
        newest = notices[0]

    title = str(getattr(newest, "title", None) or "").strip()
    if not title:
        return []
    published = ""
    for attr in ("published_at", "notification_date"):
        value = getattr(newest, attr, None)
        if value is not None:
            published = value.isoformat()
            break
    text = f"Latest published university notice: {title}"
    if published:
        text += f" (published {published})"
    return [EvidenceItem(
        sub_question=sub.text,
        source=SourceType.NOTICES,
        text=text[:1400],
        title=title,
        source_id=str(getattr(newest, "id", "") or ""),
        issued_at=published,
        verified=bool(getattr(newest, "is_verified", False)),
        source_label=(getattr(newest, "notice_type", None) or "") if getattr(newest, "notice_type", None) else "",
    )]


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
# Structured collectors (DOCUMENTS / WEBSITE)
# ---------------------------------------------------------------------------

_DOC_EVIDENCE_LIMIT = 3
_WEBSITE_EVIDENCE_LIMIT = 3
_WEBSITE_PROBE_CANDIDATES = 30


def _evidence_from_university_documents(
    sub: SubQuery,
    db: Any,
    entities: Any,
    ctx: Any,
) -> list[EvidenceItem]:
    """Verified + published official UniversityDocument records as evidence.

    The canonical repository keeps NO extracted body text — only provenance
    (title, doc_type, programme, published date and, for crawler-sourced rows,
    the verified source URL). The collector therefore surfaces the document
    RECORD itself: which official notification / other official document
    exists, when it was published and where it lives. Document content is left
    to the RAG sub-fragment which searches the knowledge base.
    """
    if db is None:
        return []
    from app.university_documents.service import list_published_documents

    programme = _norm_programme(
        (getattr(entities, "programme", None) if entities is not None else None)
        or getattr(ctx, "programme", None)
    )
    sig = _sig_tokens(sub.text)

    try:
        rows = list_published_documents(
            db,
            q=None,
            programme=programme,
            limit=_DOC_EVIDENCE_LIMIT * 4,
        )
    except Exception:
        return []
    if not rows:
        return []
    # list_published_documents offers only a whole-phrase title LIKE (no
    # tokenized search), so the candidate list is probed locally by significant
    # token overlap. Order is preserved from the newest-first repository query.
    scored: list[tuple[int, Any]] = []
    for d in rows:
        hay = (
            f"{d.title or ''} {d.programme_name or ''} "
            f"{d.programme_id or ''}".lower()
        )
        score = sum(hay.count(tok) for tok in sig)
        if score:
            scored.append((score, d))
    if not scored:
        return []
    scored.sort(key=lambda pair: pair[0], reverse=True)
    rows = [d for _score, d in scored[:_DOC_EVIDENCE_LIMIT]]

    items: list[EvidenceItem] = []
    for d in rows:
        parts = [str(d.title or "").strip()]
        if d.doc_type:
            parts.append(f"[{d.doc_type}]")
        published = d.published_at.isoformat() if d.published_at else None
        if published:
            parts.append(f"published {published}")
        text = " — ".join(p for p in parts if p)
        if not text:
            continue
        items.append(EvidenceItem(
            sub_question=sub.text,
            source=SourceType.DOCUMENTS,
            text=text[:1400],
            title=str(d.title or d.doc_type or "University document"),
            source_id=str(d.id),
            url=_https_only(d.source_url),
            issued_at=published or "",
            verified=bool(d.is_verified),
            source_label=(d.doc_type or "") if d.doc_type else "",
            programme=str(d.programme_id or "").strip() if (d.programme_id or "").strip() else "",
            semester=(d.semester or "") if d.semester else "",
            batch=(d.batch or "") if d.batch else "",
            doc_id=str(d.id),
        ))
    return items


def _evidence_from_website_pages(
    sub: SubQuery,
    db: Any,
    entities: Any,
    ctx: Any,
) -> list[EvidenceItem]:
    """Verified WebsitePage snippets as evidence, newest-first and keyword-
    probed. Only pages the administrator classified ``verified`` with a healthy
    crawl result count; the URL is the verified page URL (https only)."""
    if db is None:
        return []
    from sqlalchemy import or_

    from app.models.website_sync import WebsitePage

    sig = _sig_tokens(sub.text)
    qry = db.query(WebsitePage).filter(
        WebsitePage.classification_status == "verified",
        WebsitePage.status.in_(("new", "unchanged", "updated")),
        WebsitePage.content.isnot(None),
        WebsitePage.content != "",
        or_(
            WebsitePage.http_status.is_(None),
            WebsitePage.http_status.between(200, 399),
        ),
    )
    if sig:
        # Narrow the candidate window with a title/category probe on the two
        # most significant tokens (cheap; the bounded candidate cap stays).
        probes = sig[:2]
        qry = qry.filter(or_(*[
            or_(
                WebsitePage.title.ilike(f"%{p}%"),
                WebsitePage.category.ilike(f"%{p}%"),
            )
            for p in probes
        ]))
    try:
        rows = qry.order_by(
            WebsitePage.last_synced.is_(None),
            WebsitePage.last_synced.desc(),
        ).limit(_WEBSITE_PROBE_CANDIDATES).all()
    except Exception:
        return []
    if not rows:
        return []

    scored: list[tuple[int, WebsitePage]] = []
    for page in rows:
        hay = f"{page.title or ''} {page.category or ''} {page.content or ''}".lower()
        score = sum(hay.count(tok) for tok in sig)
        if score:
            scored.append((score, page))
    scored.sort(key=lambda pair: pair[0], reverse=True)

    items: list[EvidenceItem] = []
    for _score, page in scored[:_WEBSITE_EVIDENCE_LIMIT]:
        content = (page.content or "").strip()
        if not content:
            continue
        items.append(EvidenceItem(
            sub_question=sub.text,
            source=SourceType.WEBSITE,
            text=content[:900] if len(content) > 900 else content,
            title=str(page.title or page.category or "CUS website page"),
            source_id=str(page.id),
            url=_https_only(page.url),
            issued_at=page.last_synced.isoformat() if page.last_synced else "",
            last_synced=page.last_synced.isoformat() if page.last_synced else "",
            verified=True,
            source_label=(page.category or "") if page.category else "",
            programme="",
            semester="",
            batch="",
            doc_id=str(page.id),
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
        elif sub.source == SourceType.DOCUMENTS:
            pool.add_many(_evidence_from_university_documents(sub, db, entities, ctx))
        elif sub.source == SourceType.WEBSITE:
            pool.add_many(_evidence_from_website_pages(sub, db, entities, ctx))
        elif sub.source == SourceType.RAG:
            try:
                items = await asyncio.to_thread(_evidence_from_rag, sub, rag_ctx or {})
            except Exception:
                items = []
            pool.add_many(items)
    return pool