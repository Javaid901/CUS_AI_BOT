"""
backend/app/catalogue/facts.py

Phase 3A — ProgrammeFacts: a deterministic, READ-ONLY knowledge abstraction over
the existing academic catalogue.

Purpose
-------
Let future intelligence components ask:

    "What facts are known about this programme?"
    "What programmes match these structured constraints?"

without knowing the internal database implementation.

The catalogue (`app.catalogue`) stays the single source of truth. This module
only *re-reads* the existing  `programmes` / `programme_categories` /
`academic_schemes` / `programme_subjects` / `minor_disciplines` /
`learning_outcomes` / `curriculum_documents` relations through the existing
catalogue service functions. It is:

  * deterministic  — identical inputs always yield identical output
  * serialisable   — every result exposes ``as_dict()`` (JSON-safe)
  * read-only      — never inserts / updates / deletes anything
  * source-aware   — provenance carried where the underlying data provides it,
                     represented as *unavailable* where it does not
  * LLM/RAG/Chroma-free — no embeddings, no retrieval, no model calls, no
                     network access, no N+1 on the chat hot path

No new database. No new schema. No second source of truth.
"""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy.orm import Session

from app.catalogue.models import (
    CurriculumDocument,
    LearningOutcome,
    MinorDiscipline,
    ProgrammeSubject,
)
from app.catalogue.service import (
    curriculum_view,
    get_curriculum_documents,
    get_learning_outcomes,
    get_minor_disciplines,
    get_subjects,
    list_programmes,
    minor_view,
    resolve_programme,
    subject_view,
)
from app.database import SessionLocal

# Explicit provenance statement. Catalogue programme attributes are maintained
# directly in the programmes row; the system records no per-attribute source
# document or URL for them. We must NOT pretend otherwise (Phase 2 principle:
# provenance that cannot be proven is represented as unavailable).
_PROVENANCE_NOTE = (
    "Catalogue programme attributes (eligibility, fee structure, duration, "
    "credits, major disciplines) are administratively maintained in the "
    "`programmes` table; no per-attribute source document or URL is recorded. "
    "Linked curriculum documents below are the only verifiable sources "
    "currently available for this programme."
)


def _session(db: Session | None) -> tuple[Session, bool]:
    own = db is None
    return (db if db is not None else SessionLocal()), own


def _close(session: Session, own: bool) -> None:
    if own and session is not None:
        session.close()


def _as_uuids(ids: list[str]) -> list[uuid.UUID]:
    out: list[uuid.UUID] = []
    for raw in ids:
        try:
            out.append(uuid.UUID(str(raw)))
        except (ValueError, TypeError):
            continue
    return out


# ---------------------------------------------------------------------------
# ProgrammeFacts — one programme's structured fact card
# ---------------------------------------------------------------------------


@dataclass(frozen=True, eq=True)
class ProgrammeFacts:
    """Deterministic read-only fact card for a single catalogue programme.

    Only fields that the existing catalogue can populate reliably are exposed.
    No field is invented; missing values are ``None`` / empty tuples.
    """

    programme_id: str
    name: str
    code: str
    level: str | None = None
    degree_level: str | None = None
    scheme_id: str | None = None
    scheme_name: str | None = None
    scheme_code: str | None = None
    eligibility: str | None = None
    fee_structure: tuple[dict[str, str], ...] = ()
    duration_years: int | None = None
    total_credits: int | None = None
    major_disciplines: tuple[str, ...] = ()
    subject_count: int = 0
    subjects: tuple[dict[str, Any], ...] = ()
    learning_outcomes: tuple[str, ...] = ()
    minor_disciplines: tuple[dict[str, Any], ...] = ()
    linked_documents: tuple[dict[str, Any], ...] = ()
    provenance_note: str = field(default=_PROVENANCE_NOTE)

    def as_dict(self) -> dict[str, Any]:
        scheme = None
        if self.scheme_id:
            scheme = {
                "id": self.scheme_id,
                "name": self.scheme_name,
                "code": self.scheme_code,
            }
        d = asdict(self)
        d["scheme"] = scheme
        d.pop("scheme_name", None)
        d.pop("scheme_code", None)
        d.pop("scheme_id", None)
        # Convert tuple fields to lists for JSON safety.
        for key in (
            "fee_structure", "major_disciplines", "subjects",
            "learning_outcomes", "minor_disciplines", "linked_documents",
        ):
            if key in d and isinstance(d[key], tuple):
                d[key] = list(d[key])
        return d


@dataclass(frozen=True, eq=True)
class ProgrammeFactsSet:
    """A collection of ProgrammeFacts plus a completeness statement.

    ``mode`` distinguishes the query class the intelligence layer will later
    rely on:

      * ``"enumerate"`` — the source returned the complete matching set.
      * ``"single"``    — one programme resolved from an entity reference.
      * ``"filter"``    — a constrained request; any row returned is exact,
                          but completeness is only as strong as the filter.

    ``complete`` is ``True`` only when the underlying catalogue query can
    establish that all matching rows were returned (i.e. it queried the full
    programmes table, optionally filtered by the reliable level/scheme fields).
    """

    mode: str
    complete: bool
    items: tuple[ProgrammeFacts, ...] = ()

    def as_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "complete": self.complete,
            "total": len(self.items),
            "programmes": [p.as_dict() for p in self.items],
        }


# ---------------------------------------------------------------------------
# Fact construction
# ---------------------------------------------------------------------------


def _facts_from(view: dict[str, Any], subjects, outcomes, minors, documents) -> ProgrammeFacts:
    return ProgrammeFacts(
        programme_id=view["id"],
        name=view["name"],
        code=view["code"],
        level=view.get("level"),
        degree_level=view.get("degree_level"),
        scheme_id=view.get("scheme_id"),
        scheme_name=view.get("scheme_name"),
        scheme_code=view.get("scheme_code"),
        eligibility=view.get("eligibility"),
        fee_structure=tuple(dict(e) for e in (view.get("fee_structure") or [])),
        duration_years=view.get("duration_years"),
        total_credits=view.get("total_credits"),
        major_disciplines=tuple(view.get("major_disciplines") or []),
        subject_count=len(subjects) if subjects else (view.get("subject_count") or 0),
        subjects=tuple(subjects),
        learning_outcomes=tuple(outcomes),
        minor_disciplines=tuple(minors),
        linked_documents=tuple(documents),
    )


def _dedup(items: list[ProgrammeFacts]) -> list[ProgrammeFacts]:
    """Stable dedup by the unique programme id (never by approximate name)."""
    seen: set[str] = set()
    out: list[ProgrammeFacts] = []
    for item in items:
        if item.programme_id in seen:
            continue
        seen.add(item.programme_id)
        out.append(item)
    return out


def _subjects_map(session: Session, ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    if not ids:
        return out
    rows = (
        session.query(ProgrammeSubject)
        .filter(ProgrammeSubject.programme_id.in_(_as_uuids(ids)))
        .order_by(ProgrammeSubject.semester, ProgrammeSubject.subject_name)
        .all()
    )
    for s in rows:
        out.setdefault(str(s.programme_id), []).append(subject_view(s))
    return out


def _outcomes_map(session: Session, ids: list[str]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    if not ids:
        return out
    rows = (
        session.query(LearningOutcome)
        .filter(LearningOutcome.programme_id.in_(_as_uuids(ids)))
        .order_by(LearningOutcome.position)
        .all()
    )
    for r in rows:
        out.setdefault(str(r.programme_id), []).append(r.outcome_text)
    return out


def _minors_map(session: Session, ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    if not ids:
        return out
    rows = (
        session.query(MinorDiscipline)
        .filter(MinorDiscipline.programme_id.in_(_as_uuids(ids)))
        .order_by(MinorDiscipline.name)
        .all()
    )
    for m in rows:
        out.setdefault(str(m.programme_id), []).append(minor_view(m))
    return out


def _documents_map(session: Session, ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    if not ids:
        return out
    rows = (
        session.query(CurriculumDocument)
        .filter(CurriculumDocument.programme_id.in_(_as_uuids(ids)))
        .order_by(CurriculumDocument.uploaded_at.desc())
        .all()
    )
    doc_ids = [cd.document_id for cd in rows if cd.document_id is not None]
    docs: dict[str, Any] = {}
    if doc_ids:
        from app.models import Document

        try:
            doc_rows = session.query(Document).filter(Document.id.in_(doc_ids)).all()
            docs = {str(d.id): d for d in doc_rows}
        except Exception:
            docs = {}
    for cd in rows:
        out.setdefault(str(cd.programme_id), []).append(
            curriculum_view(cd, docs.get(str(cd.document_id))) if cd.document_id else curriculum_view(cd, None)
        )
    return out


def _build_many(
    session: Session,
    views: list[dict[str, Any]],
    include_subjects: bool,
    include_outcomes: bool,
    include_documents: bool,
) -> list[ProgrammeFacts]:
    ids = [v["id"] for v in views]
    subjects = _subjects_map(session, ids) if include_subjects else {}
    outcomes = _outcomes_map(session, ids) if include_outcomes else {}
    minors = _minors_map(session, ids) if include_subjects else {}
    documents = _documents_map(session, ids) if include_documents else {}
    items = [
        _facts_from(v, subjects.get(v["id"], []), outcomes.get(v["id"], []),
                    minors.get(v["id"], []), documents.get(v["id"], []))
        for v in views
    ]
    return _dedup(items)


# ---------------------------------------------------------------------------
# Public read API (deterministic, read-only, lightweight)
# ---------------------------------------------------------------------------


def get_programme_facts(
    identifier: str,
    db: Session | None = None,
    *,
    include_subjects: bool = True,
    include_outcomes: bool = True,
    include_documents: bool = True,
) -> ProgrammeFacts | None:
    """Single-programme lookup by UUID, name, code, alias or option label.

    Returns ``None`` for unknown references — never a substitute programme.
    """
    session, own = _session(db)
    try:
        view = resolve_programme(str(identifier), db=session)
        if view is None:
            return None
        subjects = get_subjects(programme_id=view["id"], db=session) if include_subjects else []
        outcomes = get_learning_outcomes(view["id"], db=session) if include_outcomes else []
        minors = get_minor_disciplines(view["id"], db=session) if include_subjects else []
        documents = get_curriculum_documents(view["id"], db=session) if include_documents else []
        return _facts_from(view, subjects, outcomes, minors, documents)
    finally:
        _close(session, own)


def list_programme_facts(
    db: Session | None = None,
    *,
    level: str | None = None,
    scheme: str | None = None,
    include_subjects: bool = False,
    include_outcomes: bool = False,
    include_documents: bool = False,
) -> ProgrammeFactsSet:
    """Complete deterministic enumeration of the programmes table.

    Optional filters use only the reliable level/scheme fields. The returned
    ``ProgrammeFactsSet(scheme`` is ``complete=True`` because the query covers
    every matching row in the source.
    """
    session, own = _session(db)
    try:
        views = list_programmes(level=level, scheme=scheme, db=session)
        items = _build_many(session, views, include_subjects, include_outcomes, include_documents)
        return ProgrammeFactsSet(mode="enumerate", complete=True, items=tuple(items))
    finally:
        _close(session, own)


def filter_programme_facts(
    db: Session | None = None,
    *,
    level: str | None = None,
    scheme: str | None = None,
    programme: str | None = None,
    code: str | None = None,
    include_subjects: bool = False,
    include_outcomes: bool = False,
    include_documents: bool = False,
) -> ProgrammeFactsSet:
    """Constrained deterministic read.

    * When ``programme``/``code`` resolves to exactly one programme the result
      is ``mode="single"`` (an entity lookup, not an enumeration).
    * Otherwise the level/scheme filters enumerate the full matching set.
    * Unknown references yield an explicit empty result — never a substitute.
    """
    session, own = _session(db)
    try:
        reference = programme or code
        if reference:
            view = resolve_programme(str(reference), db=session)
            if view is None:
                return ProgrammeFactsSet(mode="filter", complete=False, items=())
            subject_rows = get_subjects(programme_id=view["id"], db=session) if include_subjects else []
            outcome_rows = get_learning_outcomes(view["id"], db=session) if include_outcomes else []
            minor_rows = get_minor_disciplines(view["id"], db=session) if include_subjects else []
            doc_rows = get_curriculum_documents(view["id"], db=session) if include_documents else []
            fact = _facts_from(view, subject_rows, outcome_rows, minor_rows, doc_rows)
            return ProgrammeFactsSet(mode="single", complete=False, items=(fact,))

        views = list_programmes(level=level, scheme=scheme, db=session)
        items = _build_many(session, views, False, False, False)
        return ProgrammeFactsSet(mode="enumerate", complete=True, items=tuple(items))
    finally:
        _close(session, own)