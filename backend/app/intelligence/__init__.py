"""
backend/app/intelligence/

Phase 1 — Safe Intelligence Foundation.

Feature-flagged (INTELLIGENCE_ENABLED, default OFF) foundation for a future
Intelligent Answering layer. Everything here is additive and inert while the
flag is off:

  - taxonomy:      deterministic high-level query-kind classification. It sits
                   ABOVE the existing intent system (planner / intent_classifier
                   / extractor) and never replaces it — no LLM, no embeddings,
                   no network calls, no database.
  - contract_ext:  minimal internal contract capturing information relevant to
                   future intelligent answering. It extends — never replaces —
                   the existing QueryContract and ConversationContext.

This package exports only lightweight, side-effect-free code. It must never
import the LLM / embedding / retrieval stack and must never become a
dependency of existing services.
"""

from app.intelligence.contract_ext import (
    IntelligenceContract,
    build_intelligence_contract,
)
from app.intelligence.taxonomy import QueryKind, classify, requires_aggregation

__all__ = [
    "IntelligenceContract",
    "build_intelligence_contract",
    "QueryKind",
    "classify",
    "requires_aggregation",
]