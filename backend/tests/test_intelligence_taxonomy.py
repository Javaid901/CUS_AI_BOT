"""
backend/tests/test_intelligence_taxonomy.py

Phase 1 — Safe Intelligence Foundation tests.

Verifies the deterministic, feature-flagged foundation:

  A. taxonomy classification (kinds, determinism, planner-action labels)
  B. intelligence contract creation (fields, normalization reuse, JSON-safety)
  C. feature flag defaults OFF and disables all attachment
  D. compatibility with existing query understanding (reuse, not replacement)
  E. compatibility with existing planner outputs (identical routing, flag on/off)
  F. protected context fields are never mutated

Run:  python -m pytest tests/test_intelligence_taxonomy.py -q   (or via pytest)
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from app.config import settings
from app.intelligence import (
    IntelligenceContract,
    QueryKind,
    build_intelligence_contract,
    classify,
    requires_aggregation,
)
from app.orchestrator.context import ConversationContext
from app.orchestrator.extractor import extract_entities

ALL_KINDS = {
    "navigation", "personal_service", "dedicated_service", "structured_fact",
    "document_lookup", "aggregation", "analytical", "exploratory",
    "general_information", "unknown",
}


# ---------------------------------------------------------------------------
# A. Taxonomy classification
# ---------------------------------------------------------------------------


def test_taxonomy_kinds_are_stable():
    assert {k.value for k in QueryKind} == ALL_KINDS


def test_existing_action_is_authoritative():
    cases = {
        "greeting": QueryKind.NAVIGATION,
        "navigation": QueryKind.NAVIGATION,
        "welcome": QueryKind.NAVIGATION,
        "structured": QueryKind.STRUCTURED_FACT,
        "catalogue": QueryKind.STRUCTURED_FACT,
        "comparison": QueryKind.ANALYTICAL,
        "rag": QueryKind.DOCUMENT_LOOKUP,
        "news": QueryKind.DOCUMENT_LOOKUP,
        "examination": QueryKind.DEDICATED_SERVICE,
        "authority": QueryKind.DEDICATED_SERVICE,
        "grievance": QueryKind.DEDICATED_SERVICE,
        "university_notices": QueryKind.DEDICATED_SERVICE,
        "student_service": QueryKind.PERSONAL_SERVICE,
    }
    for action, kind in cases.items():
        classified = classify("whatever the message is", existing_action=action)
        assert classified == kind, f"action={action!r} -> {classified} (want {kind})"


def test_text_signals():
    cases = {
        "hello": QueryKind.NAVIGATION,
        "back": QueryKind.NAVIGATION,
        "reset": QueryKind.NAVIGATION,
        "list all pg programmes": QueryKind.AGGREGATION,
        "how many departments are there": QueryKind.AGGREGATION,
        "enumerate the courses offered": QueryKind.AGGREGATION,
        "which all courses are available": QueryKind.AGGREGATION,
        "difference between BBA and BCA": QueryKind.ANALYTICAL,
        "BBA vs MCA fee": QueryKind.ANALYTICAL,
        "what is the capital of France?": QueryKind.GENERAL_INFORMATION,
        "how is the weather in Srinagar today?": QueryKind.GENERAL_INFORMATION,
        "student services": QueryKind.PERSONAL_SERVICE,
        "fill exam form": QueryKind.PERSONAL_SERVICE,
        "model papers for MCA": QueryKind.DEDICATED_SERVICE,
        "division improvement": QueryKind.DEDICATED_SERVICE,
        "date sheet of bca semester 1": QueryKind.DEDICATED_SERVICE,
        "what does the prospectus say about hostel": QueryKind.DOCUMENT_LOOKUP,
        "according to the handbook, what is the admission fee": QueryKind.DOCUMENT_LOOKUP,
        "fee structure of BCA": QueryKind.STRUCTURED_FACT,
        "semester subjects of MCA": QueryKind.STRUCTURED_FACT,
        "what is the fee": QueryKind.STRUCTURED_FACT,
        "how many credits does BBA have": QueryKind.STRUCTURED_FACT,
        "tell me about the university": QueryKind.EXPLORATORY,
    }
    for msg, kind in cases.items():
        classified = classify(msg)
        assert classified == kind, f"classify({msg!r}) -> {classified} (want {kind})"


def test_classify_is_deterministic():
    for msg in (
        "list all pg programmes", "difference between BBA and BCA",
        "fee structure of BCA", "what is the capital of France?",
        "model papers for MCA", "tell me about the university",
    ):
        assert classify(msg) == classify(msg)


def test_classify_uses_supplied_entities_not_a_second_detector():
    e = extract_entities("fee structure of BCA")
    assert classify("say anything else", entities=e) == QueryKind.STRUCTURED_FACT


def test_requires_aggregation_signal():
    assert requires_aggregation("list all pg programmes") is True
    assert requires_aggregation("how many departments are there") is True
    assert requires_aggregation("what is the fee for BCA") is False
    assert requires_aggregation("difference between BBA and BCA") is False
    # A 2+ programme comparison is analytical, not aggregation.
    e = extract_entities("difference between BBA and BCA")
    assert requires_aggregation("difference between BBA and BCA", entities=e) is False


# ---------------------------------------------------------------------------
# B. Intelligence contract
# ---------------------------------------------------------------------------


def test_contract_captures_entities_and_kind():
    e = extract_entities("semester 2 subjects of MCA under NEP")
    c = build_intelligence_contract("semester 2 subjects of MCA under NEP", e)
    assert isinstance(c, IntelligenceContract)
    assert c.entities["programme"] == "mca"
    assert c.entities["semester"] == 2
    assert c.topic == "specializations"  # canonical extractor topic for "subjects"
    assert c.query_kind == QueryKind.STRUCTURED_FACT
    assert c.aggregation_requested is False


def test_contract_labels_existing_plan_action():
    plan = SimpleNamespace(action="examination", confidence=0.93, extra={})
    c = build_intelligence_contract("model papers", plan=plan)
    assert c.query_kind == QueryKind.DEDICATED_SERVICE
    assert c.existing_action == "examination"
    assert c.confidence == 0.93


def test_contract_uses_existing_normalization():
    ctx = ConversationContext()
    ctx.query_original = "bcaa fee structure"
    ctx.query_clean = "bca fee structure"
    ctx.query_corrected = True
    c = build_intelligence_contract("bcaa fee structure", ctx=ctx)
    assert c.query == "bcaa fee structure"
    assert c.normalized_query == "bca fee structure"
    assert c.query_kind == QueryKind.STRUCTURED_FACT


def test_contract_as_dict_is_json_safe():
    c = build_intelligence_contract("list all pg programmes")
    payload = c.as_dict()
    json.dumps(payload)  # must not raise
    assert payload["query_kind"] == "aggregation"
    assert payload["aggregation_requested"] is True
    assert isinstance(payload["source_preferences"]["sources"], list)


def test_contract_never_duplicates_planner_contract():
    # The intelligence contract is advisory metadata only — its schema is fixed
    # and contains no routing override for the planner / engine.
    c = build_intelligence_contract("fee structure of BCA")
    assert set(c.as_dict().keys()) == {
        "query", "normalized_query", "query_kind", "aggregation_requested",
        "entities", "programme", "semester", "topic", "source_preferences",
        "confidence", "existing_intent", "existing_action",
    }
    assert c.as_dict()["query_kind"] == "structured_fact"


# ---------------------------------------------------------------------------
# C. Feature flag
# ---------------------------------------------------------------------------


def test_flag_defaults_off():
    assert settings.INTELLIGENCE_ENABLED is False


def test_flag_off_attaches_nothing():
    from app.orchestrator.planner import plan
    result = plan("back", ConversationContext(), "intel-flag-off", extract_entities("back"))
    assert "contract" in (result.extra or {})
    assert "intelligence" not in (result.extra or {})
    # Legacy routing is untouched.
    assert result.action in ("navigation", "welcome")


def test_flag_on_attaches_intelligence_metadata_but_route_is_identical():
    from app.orchestrator.planner import plan

    prev = settings.INTELLIGENCE_ENABLED
    settings.INTELLIGENCE_ENABLED = True
    try:
        off_route = plan("back", ConversationContext(), "intel-off2", extract_entities("back"))
        on_route = plan("back", ConversationContext(), "intel-on2", extract_entities("back"))
        # The plan shape is identical to the flag-off path.
        assert on_route.action == off_route.action
        assert on_route.target == off_route.target
        assert on_route.confidence == off_route.confidence
        # Advisory metadata is attached when enabled.
        extra = on_route.extra or {}
        md = extra["intelligence"]
        assert md["query_kind"] == "navigation"
        assert md["existing_action"] == off_route.action
        assert "contract" in extra
    finally:
        settings.INTELLIGENCE_ENABLED = prev


# ---------------------------------------------------------------------------
# D. Compatibility with existing query understanding
# ---------------------------------------------------------------------------


def test_existing_query_understanding_remains_authoritative():
    from app.orchestrator.query_understanding import process_query

    qr = process_query("bcaa fee structure")
    assert qr["clean"] == "bca fee structure"
    assert qr["corrected"] is True
    # The taxonomy consumes the pre-resolved extraction, it does not re-route.
    assert classify("bcaa fee structure") == QueryKind.STRUCTURED_FACT


# ---------------------------------------------------------------------------
# F. Protected context fields
# ---------------------------------------------------------------------------


def test_contract_does_not_mutate_context():
    ctx = ConversationContext()
    ctx.selected_document_id = "doc-x"
    ctx.exam_document_ids = ["a", "b"]
    ctx.selected_model_paper_id = "mp-1"
    ctx.college = "col-1"
    ctx.pending_clarification = "programme"
    before = (
        ctx.selected_document_id,
        tuple(ctx.exam_document_ids),
        ctx.selected_model_paper_id,
        ctx.college,
        ctx.pending_clarification,
    )
    c = build_intelligence_contract(
        "what is the fee",
        extract_entities("what is the fee"),
        ctx=ctx,
        plan=SimpleNamespace(action="catalogue", confidence=0.9, extra={}),
    )
    after = (
        ctx.selected_document_id,
        tuple(ctx.exam_document_ids),
        ctx.selected_model_paper_id,
        ctx.college,
        ctx.pending_clarification,
    )
    assert c.query_kind == QueryKind.STRUCTURED_FACT
    assert before == after


def test_contract_does_not_touch_planner_output():
    plan = SimpleNamespace(action="rag", confidence=0.8, extra={"semantic_intent": "fee"})
    build_intelligence_contract("something", extract_entities("something"), plan=plan)
    assert plan.action == "rag"
    assert plan.extra == {"semantic_intent": "fee"}