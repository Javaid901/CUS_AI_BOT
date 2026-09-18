"""
backend/app/intelligence/contract_ext.py

Minimal internal contract for future Intelligent Answering.

This is an EXTENSION/ADAPTER over the real existing architecture — it does NOT
replace the existing QueryContract (app.orchestrator.contract), the
ConversationContext, or planner outputs. It captures, in one lightweight
dataclass, the information later intelligence phases will need (query,
normalized query, kind, entities, programme, semester, topic, source
preferences, confidence, existing intent/action).

It is pure (stdlib dataclasses + the deterministic taxonomy): no LLM, no
embeddings, no network, no database, and reading it never mutates any existing
object it is given.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from app.intelligence.taxonomy import QueryKind, classify, requires_aggregation


@dataclass(frozen=True)
class IntelligenceContract:
    """Advisory metadata describing the KIND of a request for future phases."""

    query: str = ""
    normalized_query: str = ""
    query_kind: QueryKind = QueryKind.UNKNOWN
    aggregation_requested: bool = False
    entities: dict[str, Any] = field(default_factory=dict)
    programme: str | None = None
    semester: str | None = None
    topic: str | None = None
    source_preferences: dict[str, Any] = field(default_factory=dict)
    confidence: float = 0.0
    existing_intent: str | None = None
    existing_action: str | None = None

    def as_dict(self) -> dict[str, Any]:
        out = asdict(self)
        out["query_kind"] = (
            self.query_kind.value
            if isinstance(self.query_kind, QueryKind)
            else str(self.query_kind)
        )
        return out


# Advisory source-hint preferences per query kind — pure metadata for later
# phases, never invoked today. Keys follow the actual existing source modules.
_SOURCE_PREFERENCES: dict[QueryKind, tuple[str, ...]] = {
    QueryKind.STRUCTURED_FACT: ("catalogue",),
    QueryKind.DEDICATED_SERVICE: ("examination", "notices", "authority", "college"),
    QueryKind.DOCUMENT_LOOKUP: ("rag", "website"),
    QueryKind.AGGREGATION: ("catalogue", "rag"),
    QueryKind.ANALYTICAL: ("catalogue", "rag", "notices"),
    QueryKind.EXPLORATORY: ("rag", "catalogue", "website", "notices"),
    QueryKind.PERSONAL_SERVICE: ("student_services",),
    QueryKind.NAVIGATION: (),
    QueryKind.GENERAL_INFORMATION: (),
    QueryKind.UNKNOWN: (),
}

# Entities captured from the existing ExtractedEntities (a small allowlist —
# never a new extraction pipeline).
_ENTITY_FIELDS: tuple[str, ...] = (
    "programme", "programmes", "level", "topic", "domain", "service",
    "semester", "scheme", "word_count", "is_back", "is_reset", "confidence",
)


def build_intelligence_contract(
    message: str,
    entities: Any = None,
    *,
    ctx: Any | None = None,
    plan: Any | None = None,
) -> IntelligenceContract:
    """Build a lightweight intelligence contract from real existing signals.

    Reuses whatever the existing pipeline already resolved:
      - entities: caller-supplied ExtractedEntities (never re-extracted here
        unless the caller omitted them entirely)
      - ctx:       the existing ConversationContext (normalized query, resolved
        programme / semester / topic)
      - plan:      the existing planner Plan (action / confidence) — the
        authoritative routing decision the taxonomy labels.
    Nothing here replaces or mutates those existing objects.
    """
    query = (message or "").strip()

    # Reuse the existing pipeline's normalization instead of recomputing it.
    normalized = query
    if ctx is not None:
        clean = getattr(ctx, "query_clean", None)
        if clean:
            normalized = clean

    # Capture resolved entities / conversational fields.
    ent: dict[str, Any] = {}
    programme: str | None = None
    semester: Any = None
    topic: str | None = None
    for _field in _ENTITY_FIELDS:
        value = getattr(entities, _field, None) if entities is not None else None
        if value is None and ctx is not None:
            # Fall back to already-resolved conversational context.
            value = {
                "programme": getattr(ctx, "programme", None),
                "semester": getattr(ctx, "semester", None),
                "topic": getattr(ctx, "topic", None),
            }.get(_field)
        if value is None:
            continue
        if isinstance(value, list):
            value = list(value)
        ent[_field] = value
    programme = ent.get("programme") or None
    semester = ent.get("semester") or None
    topic = ent.get("topic") or None

    existing_action = getattr(plan, "action", None) if plan is not None else None
    existing_intent = None
    if plan is not None:
        existing_intent = ((plan.extra or {}).get("semantic_intent")) if getattr(plan, "extra", None) else None
    if existing_intent is None and ctx is not None:
        existing_intent = getattr(ctx, "last_intent", None)

    confidence = 0.0
    if plan is not None:
        try:
            confidence = float(getattr(plan, "confidence", 0.0) or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0

    query_kind = classify(query, entities=entities, existing_action=existing_action)

    return IntelligenceContract(
        query=query,
        normalized_query=normalized,
        query_kind=query_kind,
        aggregation_requested=requires_aggregation(query, entities),
        entities=ent,
        programme=programme,
        semester=semester,
        topic=topic,
        source_preferences={"sources": list(_SOURCE_PREFERENCES.get(query_kind, ()))},
        confidence=confidence,
        existing_intent=existing_intent,
        existing_action=existing_action,
    )