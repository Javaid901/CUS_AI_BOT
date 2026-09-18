"""
backend/app/multi_source/

Intelligent Multi-Source Answering — pragmatic extension to the existing
orchestrator pipeline.

Core idea: when a user question spans multiple university knowledge sources
(e.g. programme facts + examination fees + date sheets), decompose it into
targeted sub-queries, retrieve evidence from each relevant source independently,
validate completeness, and synthesise one grounded answer via the existing LLM.

Architecture:
  decompose  →  evidence collection  →  validation  →  LLM synthesis

Everything here is additive.  The feature is gated by settings.MULTI_SOURCE_ENABLED
(default OFF).  When off the application behaves exactly as before.
"""

from app.multi_source.decompose import decompose_query, SubQuery, SourceType
from app.multi_source.evidence import collect_evidence, EvidenceItem, EvidencePool
from app.multi_source.synthesize import synthesize_answer

__all__ = [
    "decompose_query",
    "SubQuery",
    "SourceType",
    "collect_evidence",
    "EvidenceItem",
    "EvidencePool",
    "synthesize_answer",
]
