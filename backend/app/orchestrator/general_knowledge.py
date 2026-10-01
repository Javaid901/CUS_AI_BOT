"""
backend/app/orchestrator/general_knowledge.py

Curated, deterministic GENERAL UNIVERSITY KNOWLEDGE layer.

This module answers the evergreen admission questions that a RAG retriever
cannot answer reliably and that the programme catalogue must not absorb:
"which website do I apply on", "which documents do I need for registration",
"what is the PG application fee", "who do I contact about admission".

Design contract (this layer is a fast path, never a second brain):

  * Records are curated, static and source-bound. Every ``answer_facts`` entry
    is transcribed from an official source declared in the data file's
    ``sources`` map (official Cluster University of Srinagar notification, or a
    directly applicable Government of Jammu and Kashmir / Higher Education
    Department source named inside a verified notification). Nothing is
    generated, inferred or re-derived here.
  * Resolution is pure matching over those records. No embedding, no vector
    store, no BM25, no crawl, no re-rank, no database and no LLM call. The file
    is parsed once per process and cached in memory.
  * Answer generation is at most ONE LLM call for the matched records, and only
    then is the existing guarded generator used. A generation failure degrades
    to the untouched RAG path.
  * The resolver is CONSERVATIVE and returns ``None`` on any doubt. ``None``
    means: run the existing planner flow exactly as before.

Deliberate exclusions (returning ``None`` is the whole point):

  * current-status questions ("is admission open?", "what is the last date?")
    stay on the existing current-status / notices path, which is the only
    path allowed to speak about the live state of the university;
  * programme-specific questions (BCA, MCA, ...) stay on the structured
    academic catalogue, which owns per-programme eligibility, fee, duration
    and subjects;
  * document-comparison / notice-freshness questions stay on the existing
    intelligent evidence path.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

_DATA_FILE = Path(__file__).resolve().parent / "data" / "general_university_knowledge.json"

# A record must reach this score to be returned. Tuned so a single strong
# phrase ("admission portal", "which documents") is enough, while two weak
# generic words ("admission fee") are not.
_MATCH_THRESHOLD = 3.0
# A level-less question ("what is the admission procedure?") may see every
# level, but only on a clearly stronger match, so an incidental word can never
# pull in the wrong level's procedure.
_LEVEL_LESS_THRESHOLD = 4.0
_PHRASE_WEIGHT = 3.0
_TERM_WEIGHT = 1.0
# A record needs at least one phrase hit, or at least this many distinct term
# hits, before its score counts. This stops a single incidental word from
# dragging in an unrelated record.
_MIN_TERM_HITS_WITHOUT_PHRASE = 2
_MAX_RECORDS = 3

# Level markers. A query that names a level only ever sees records for that
# level (plus "general" ones); UG and PG records therefore never bleed into
# each other.
_LEVEL_UG_RE = re.compile(
    r"\b(ug|u\.g\.|undergraduate|under graduate|undergrad|graduation|"
    r"bachelor(?:s|ial)?|fyugp|four\s*-?\s*year\s+(?:undergraduate|ug)|"
    r"integrated\s*pg|integrated\s*post\s*graduate|dyd|design\s+your\s+degree)\b",
    re.IGNORECASE,
)
_LEVEL_PG_RE = re.compile(
    r"\b(pg|p\.g\.|postgraduate|post graduate|postgrad|master(?:s|ial|ing)?|"
    r"m\.?sc\.?|m\.?s\.?|m\.?a\.?|m\.?com\.?|mca|mba|med|cluet|"
    r"integrated\s*b\.?ed|ig)\b",
    re.IGNORECASE,
)
# Programme mentions belong to the catalogue, not to this layer.
_PROGRAMME_RE = re.compile(
    r"\b(bca|bba|bsc|b\.?sc|bcom|b\.?com|bsem|bsw|ba|bcch|bed|b\.?ed|bedmed|"
    r"msc|m\.?sc|ma|m\.?a|mcom|mca|mba|med|phd|ph\.?d|int\.?bca|ibca|"
    r"mithibca|maenglish|ma hindi|ma urdu|ma sociology|ma political)\b",
    re.IGNORECASE,
)
# Phrases that mark a request as a document/notice freshness comparison, which
# the existing intelligent evidence path owns.
_COMPARISON_RE = re.compile(
    r"\b(newer|newest|latest|superseded|supersede|superseding|revised|"
    r"withdrawn|cancelled|canceled)\b",
    re.IGNORECASE,
)
# Admission announcement ARTEFACTS that the shared status-subject list does not
# cover. "Has the merit list been declared?" is a state claim, not evergreen
# procedure, so it must not reach the curated records even though the shared
# detector classifies it as neither status nor knowledge. Paired with the
# SHARED status-marker vocabulary (never a second marker list), this closes
# the only remaining route by which a published schedule could be presented as
# a current state.
_STATUS_ARTIFACT_RE = re.compile(
    r"\b(merit\s+lists?|spot\s+round|preference\s+filling|"
    r"admission\s+(?:notice|notification)|first\s+merit|second\s+merit)\b",
    re.IGNORECASE,
)
_HELLO_OR_MENU_RE = re.compile(
    r"^\s*(hi|hey|hello|namaste|menu|home|back|next|yes|no|ok|okay|thanks|"
    r"thank\s*you|start\s*over|stop|help|options)\b[\s!.,]*$",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GeneralKnowledgeRecord:
    """One curated, source-bound answer unit."""

    id: str
    domain: str
    topic: str
    level: str
    scope: str
    cycle: str
    volatile: bool
    answer_facts: tuple[str, ...]
    phrases: tuple[str, ...]
    terms: frozenset[str]
    sources: tuple[str, ...]
    verification: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "domain": self.domain,
            "topic": self.topic,
            "level": self.level,
            "scope": self.scope,
            "cycle": self.cycle,
            "volatile": self.volatile,
            "answer_facts": list(self.answer_facts),
            "sources": list(self.sources),
            "verification": self.verification,
        }


@dataclass(frozen=True)
class GeneralKnowledgeAnswer:
    """The resolver result handed to the engine."""

    records: tuple[GeneralKnowledgeRecord, ...]
    sources: tuple[dict[str, Any], ...] = field(default=())

    def to_dict(self) -> dict[str, Any]:
        return {
            "records": [r.to_dict() for r in self.records],
            "sources": [dict(s) for s in self.sources],
        }


# ---------------------------------------------------------------------------
# Data loading (parse once per process)
# ---------------------------------------------------------------------------

_lock = threading.Lock()
_loaded = False
_records: tuple[GeneralKnowledgeRecord, ...] = ()
_sources: dict[str, dict[str, Any]] = {}
_load_error: str | None = None


def _record_from_dict(raw: dict[str, Any]) -> GeneralKnowledgeRecord | None:
    rid = str(raw.get("id") or "").strip()
    facts = tuple(str(f).strip() for f in (raw.get("answer_facts") or []) if str(f).strip())
    if not rid or not facts:
        return None
    return GeneralKnowledgeRecord(
        id=rid,
        domain=str(raw.get("domain") or "admissions"),
        topic=str(raw.get("topic") or "general"),
        level=str(raw.get("level") or "general"),
        scope=str(raw.get("scope") or "cluster_university_of_srinagar"),
        cycle=str(raw.get("cycle") or ""),
        volatile=bool(raw.get("volatile")),
        answer_facts=facts,
        phrases=tuple(
            str(p).strip().lower()
            for p in (raw.get("phrases") or [])
            if str(p).strip()
        ),
        terms=frozenset(
            str(t).strip().lower()
            for t in (raw.get("terms") or [])
            if str(t).strip()
        ),
        sources=tuple(
            str(s).strip() for s in (raw.get("sources") or []) if str(s).strip()
        ),
        verification=str(raw.get("verification") or "official_verified"),
    )


def _load() -> None:
    global _loaded, _records, _sources, _load_error
    with _lock:
        if _loaded:
            return
        _loaded = True  # a failed load must not retry on every request
        try:
            raw = json.loads(_DATA_FILE.read_text(encoding="utf-8"))
            sources = raw.get("sources") or {}
            if not isinstance(sources, dict):
                sources = {}
            _sources = {str(k): dict(v) for k, v in sources.items() if isinstance(v, dict)}
            parsed = [_record_from_dict(r) for r in (raw.get("records") or [])]
            # A record whose declared source is missing or unverified is
            # dropped rather than served: the source binding is the safety
            # property of this layer.
            kept: list[GeneralKnowledgeRecord] = []
            for rec in parsed:
                if rec is None:
                    continue
                if not rec.sources:
                    continue
                if not all(
                    _sources.get(sid, {}).get("verified") is True
                    and str(_sources.get(sid, {}).get("url") or "").startswith("https://")
                    for sid in rec.sources
                ):
                    continue
                kept.append(rec)
            _records = tuple(kept)
        except Exception as exc:  # noqa: BLE001 - a broken file must never break chat
            _load_error = f"{type(exc).__name__}: {exc}"
            _records = ()
            _sources = {}


def knowledge_records() -> tuple[GeneralKnowledgeRecord, ...]:
    _load()
    return _records


def knowledge_sources() -> dict[str, dict[str, Any]]:
    _load()
    return _sources


def knowledge_load_error() -> str | None:
    _load()
    return _load_error


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")


def _normalise(text: str) -> str:
    low = str(text or "").lower()
    low = low.replace("&", " and ")
    low = re.sub(r"[^a-z0-9./]+", " ", low)
    return _WS_RE.sub(" ", low).strip()


# Maximum length of a bare topic fragment that the planner's Rule 10b
# deliberately turns into a "which programme?" slot-fill question. The curated
# layer must not take those: the existing targeted clarification is the better
# answer for a short, programme-less attribute request.
_SLOT_FILL_MAX_WORDS = 4


def _mentions_programme(query: str, context: Any) -> bool:
    if _PROGRAMME_RE.search(query):
        return True
    for attr in ("programme", "programmes"):
        value = getattr(context, attr, None)
        if value:
            return True
    return False


def _keeps_slot_fill(query: str, context: Any, entities: Any) -> bool:
    """True when planner Rule 10b would own this message as a slot-fill.

    Mirrors the planner's own condition (a concrete topic with no programme,
    college or domain context) and adds its length bound, so the curated
    layer never steals a short "which programme?" clarification.
    """
    if not getattr(entities, "topic", None):
        return False
    if _mentions_programme(query, context):
        return False
    for attr in ("programme", "college", "domain"):
        if getattr(context, attr, None):
            return False
    word_count = getattr(entities, "word_count", None)
    if word_count is None:
        word_count = len(query.split())
    return word_count <= _SLOT_FILL_MAX_WORDS


def _record_level_allows(record: GeneralKnowledgeRecord, want: set[str]) -> bool:
    if record.level == "general":
        return True
    return record.level in want


def _score(record: GeneralKnowledgeRecord, query_norm: str) -> float:
    score = 0.0
    for phrase in record.phrases:
        if phrase and phrase in query_norm:
            score += _PHRASE_WEIGHT
    hits = 0
    for term in record.terms:
        if term and term in query_norm:
            hits += 1
    if hits:
        score += hits * _TERM_WEIGHT
    phrase_hit = score >= _PHRASE_WEIGHT
    if not phrase_hit and hits < _MIN_TERM_HITS_WITHOUT_PHRASE:
        return 0.0
    return score


def resolve_general_knowledge(
    query: str,
    context: Any = None,
    *,
    entities: Any = None,
) -> GeneralKnowledgeAnswer | None:
    """Resolve ``query`` against the curated general-knowledge records.

    Returns a :class:`GeneralKnowledgeAnswer` with at most ``_MAX_RECORDS``
    records, or ``None`` when the question must keep its existing route. A
    ``None`` result is the normal, expected outcome for the majority of
    traffic and must never be treated as an error.
    """
    raw = str(query or "").strip()
    if not raw or len(raw) > 400:
        return None
    if _HELLO_OR_MENU_RE.match(raw):
        return None

    # A bare menu chip / option selection is navigation, not knowledge.
    word_count = len(raw.split())
    if word_count <= 2:
        return None

    # Current-status, deadline and portal-state questions are owned by the
    # existing current-status / notices path. Reuse its detector so the two
    # layers can never disagree about what counts as "current".
    from app.orchestrator.current_status import _STATUS_MARKER_RE, classify_current_status

    status_marker = _STATUS_MARKER_RE

    if classify_current_status(raw):
        return None
    if _COMPARISON_RE.search(raw):
        return None

    # "Has the merit list been declared?" carries a shared status marker but no
    # subject the shared detector recognises. The marker is still a claim about
    # the current state, so keep the curated layer out of it entirely.
    if status_marker.search(raw) and _STATUS_ARTIFACT_RE.search(raw):
        return None

    # Programme-specific questions stay on the structured catalogue.
    if _mentions_programme(raw, context) or _mentions_programme(raw, entities):
        return None

    # A short, programme-less attribute fragment belongs to the planner's
    # targeted "which programme?" slot-fill, not to this layer.
    if _keeps_slot_fill(raw, context, entities):
        return None

    # Exam / result / date-sheet vocabulary stays on its protected routes.
    if re.search(
        r"\b(exam|examination|result|date\s?sheet|timetable|marks?\s?card|"
        r"admit\s?card|admission\s?form\s?for\s?exam|model\s?paper|division\s?"
        r"improvement)\b",
        raw,
        re.IGNORECASE,
    ):
        return None

    query_norm = _normalise(raw)
    if not query_norm:
        return None

    want_levels: set[str] = set()
    threshold = _MATCH_THRESHOLD
    if _LEVEL_UG_RE.search(raw):
        want_levels.add("undergraduate")
    if _LEVEL_PG_RE.search(raw):
        want_levels.add("postgraduate")
    if not want_levels:
        # No level named: the question is genuinely cross-level, so every
        # level is eligible but only on a clearly stronger match. The records
        # stay separate — a UG procedure fact is never served as a PG one.
        want_levels = {"general", "undergraduate", "postgraduate"}
        threshold = _LEVEL_LESS_THRESHOLD

    records = knowledge_records()
    if not records:
        return None

    scored: list[tuple[float, GeneralKnowledgeRecord]] = []
    for record in records:
        if not _record_level_allows(record, want_levels):
            continue
        # A published schedule is a record of what a notice announced for one
        # cycle. It must never back an answer about the CURRENT state, so any
        # status wording in the question disqualifies the volatile records
        # even when the shared status detector did not classify the question.
        if record.volatile and status_marker.search(raw):
            continue
        score = _score(record, query_norm)
        if score >= threshold:
            scored.append((score, record))
    if not scored:
        return None

    # Highest score first; ties broken by record id so the order is stable
    # across processes.
    scored.sort(key=lambda pair: (-pair[0], pair[1].id))
    chosen = tuple(rec for _score_value, rec in scored[:_MAX_RECORDS])

    source_map = knowledge_sources()
    seen: set[str] = set()
    sources: list[dict[str, Any]] = []
    for rec in chosen:
        for sid in rec.sources:
            if sid in seen:
                continue
            seen.add(sid)
            src = source_map.get(sid)
            if src:
                sources.append(src)

    return GeneralKnowledgeAnswer(records=chosen, sources=tuple(sources))


def answer_from_record_ids(record_ids: Iterable[str]) -> GeneralKnowledgeAnswer | None:
    """Assemble an answer from an explicit set of curated record ids.

    Used for a compound question whose clauses were each resolved against the
    curated records separately: the caller unions those per-clause record ids
    so one context covers every clause, instead of letting a single whole-
    message match drop the weaker clause. Every id is re-validated against the
    loaded dataset, so this can never widen the curated surface.
    """
    if not knowledge_records():
        return None
    by_id = {rec.id: rec for rec in knowledge_records()}
    wanted = [rid for rid in dict.fromkeys(record_ids) if rid in by_id]
    if not wanted:
        return None
    chosen = tuple(by_id[rid] for rid in wanted)

    source_map = knowledge_sources()
    seen: set[str] = set()
    sources: list[dict[str, Any]] = []
    for rec in chosen:
        for sid in rec.sources:
            if sid in seen:
                continue
            seen.add(sid)
            src = source_map.get(sid)
            if src:
                sources.append(src)
    return GeneralKnowledgeAnswer(records=chosen, sources=tuple(sources))



# ---------------------------------------------------------------------------
# Answer generation prompt
# ---------------------------------------------------------------------------

GENERAL_KNOWLEDGE_SYSTEM_PROMPT = (
    "You are CUS AI Assistant, the official help desk for Cluster University of Srinagar. "
    "You are answering from a small set of VERIFIED FACTS transcribed from official "
    "Cluster University of Srinagar admission notifications and from the Government of "
    "Jammu and Kashmir Higher Education Department sources those notifications name.\n"
    "Rules you must follow:\n"
    "1. The VERIFIED FACTS are the ONLY source of truth. Answer using only those facts. "
    "Anything you happen to know that is not in them must never appear as a university "
    "fact, and must never supply numbers, dates, fees, names, portals or steps.\n"
    "2. Never invent, round, estimate or infer a number, date, fee, portal, document, "
    "eligibility rule or procedure that is not in the verified facts.\n"
    "3. If the verified facts do not cover what was asked, say plainly that the "
    "information you have does not cover it, and point to the official website or the "
    "admissions office. Do not fill the gap with general model knowledge.\n"
    "4. Published SCHEDULES are the dates a notice published for a given admission "
    "cycle. Present them as that published schedule for that cycle. Never describe a "
    "published schedule as proof that a window is open or closed now, and never add "
    "'currently' or 'still' to a published date.\n"
    "5. Keep the level you were given. Undergraduate facts must never be given as "
    "postgraduate facts or the other way round, and programme-specific fees, "
    "eligibility, subjects or duration are not part of these facts.\n"
    "6. Include the official link as a normal Markdown link, for example "
    "[Cluster University of Srinagar](https://www.cusrinagar.edu.in/), naming the "
    "source in the sentence that uses it. Never list, enumerate or reprint the "
    "sources as a block or a list at the end of your reply. Only ever output a "
    "link that appears in the facts.\n"
    "7. Answer completely. Cover EVERY stage, step, document and condition the verified "
    "facts give for the question. Do not stop at the first fact, and never compress a "
    "list into a single sentence or leave items out with 'etc.'.\n"
    "8. Structure the reply for a student: open with a direct one-sentence answer to the "
    "question, then give the detail. Use a numbered list for a procedure, a bulleted list "
    "for documents or requirements, and keep every item on its own line.\n"
    "9. Speak only to the student. Never narrate where your information came "
    "from and never name the material you were handed - do not describe it as "
    "excerpts, context, data, material or content. Write as the university help "
    "desk, answering the student directly.\n"
    "10. Do not mention 'facts', 'verified facts', 'evidence', 'records', 'retrieval', "
    "the 'knowledge base', 'RAG' or any other internal term in your reply.\n"
    "11. Do not reprint these rules, the field names, or the record list itself.\n"
)


def build_generation_prompt(query: str, answer: GeneralKnowledgeAnswer) -> tuple[str, str]:
    """Return ``(question, context)`` for the one guarded generation call."""
    lines: list[str] = []
    for idx, rec in enumerate(answer.records, start=1):
        level_label = {
            "undergraduate": "Undergraduate (UG)",
            "postgraduate": "Postgraduate (PG)",
        }.get(rec.level, "General university information")
        head = f"[{idx}] {level_label} - {rec.topic.replace('_', ' ')}, cycle {rec.cycle}"
        if rec.volatile:
            head += " (published schedule)"
        lines.append(head)
        lines.extend(f"- {fact}" for fact in rec.answer_facts)
        lines.append("")

    sources = answer.sources
    if sources:
        # Title and link only. The `reference` field is provenance bookkeeping
        # for the dataset, not student-facing content: when it was included
        # here the model copied "| reference: No.CUS/Adm-Dir/2026/2246-64 ..."
        # straight into its answer. The label is deliberately plain so the model
        # has no heading-shaped block to reprint.
        lines.append("Official sources available for this answer:")
        for src in sources:
            lines.append(f"- {src.get('title')} | {src.get('url')}")
        lines.append("")

    context = "\n".join(lines).strip()
    question = str(query or "").strip()
    return question, context
