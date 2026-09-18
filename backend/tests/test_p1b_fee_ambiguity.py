"""
backend/tests/test_p1b_fee_ambiguity.py

P1-B regression coverage: fee ambiguity detection and clarification.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.orchestrator.contract import QueryContract, build_contract, detect_fee_type
from app.orchestrator.context import ConversationContext
from app.orchestrator.extractor import extract_entities


def test_detect_fee_type_explicit_examination():
    """Explicit examination fee phrases should return 'examination'."""
    assert detect_fee_type("What is the examination fee?") == "examination"
    assert detect_fee_type("exam fee structure") == "examination"
    assert detect_fee_type("examination form fee") == "examination"
    assert detect_fee_type("exam charges") == "examination"
    assert detect_fee_type("fee for exam") == "examination"


def test_detect_fee_type_explicit_programme():
    """Explicit programme fee phrases should return 'programme'."""
    assert detect_fee_type("What is the programme fee?") == "programme"
    assert detect_fee_type("course fee for BCA") == "programme"
    assert detect_fee_type("tuition fee") == "programme"
    assert detect_fee_type("admission fee") == "programme"


def test_detect_fee_type_exam_plus_fee():
    """'exam' + 'fee' combination without programme fee phrases -> examination."""
    assert detect_fee_type("exam fee") == "examination"
    assert detect_fee_type("examination fee details") == "examination"


def test_detect_fee_type_context_inheritance():
    """Fee type should inherit from context for referential follow-ups."""
    ctx = ConversationContext()
    ctx.fee_type = "examination"
    entities = extract_entities("How much is it?")
    assert detect_fee_type("How much is it?", entities, ctx) == "examination"

    ctx.fee_type = "programme"
    assert detect_fee_type("What about the fee?", entities, ctx) == "programme"


def test_detect_fee_type_no_inheritance_for_unrelated():
    """Unrelated questions should not inherit fee_type."""
    ctx = ConversationContext()
    ctx.fee_type = "examination"
    entities = extract_entities("What is the capital of France?")
    assert detect_fee_type("What is the capital of France?", entities, ctx) is None


def test_contract_fee_type_explicit_examination():
    """Contract should capture explicit examination fee_type."""
    message = "What is the examination fee for MCA?"
    entities = extract_entities(message)
    ctx = ConversationContext()
    ctx.programme = "mca"
    contract = build_contract(message, entities, ctx=ctx)
    assert contract.fee_type == "examination"
    assert contract.programme == "mca"
    assert contract.topic == "fee"


def test_contract_fee_type_explicit_programme():
    """Contract should capture explicit programme fee_type."""
    message = "What is the MCA programme fee?"
    entities = extract_entities(message)
    ctx = ConversationContext()
    ctx.programme = "mca"
    contract = build_contract(message, entities, ctx=ctx)
    assert contract.fee_type == "programme"
    assert contract.programme == "mca"
    assert contract.topic == "fee"


def test_contract_fee_type_context_inheritance():
    """Contract should inherit fee_type from context for follow-ups."""
    ctx = ConversationContext()
    ctx.programme = "mca"
    ctx.fee_type = "examination"
    entities = extract_entities("How much is it?")
    contract = build_contract("How much is it?", entities, ctx=ctx)
    assert contract.fee_type == "examination"
    assert contract.programme == "mca"


def test_contract_fee_type_none_when_ambiguous():
    """Contract should have fee_type=None when ambiguous (no explicit, no context)."""
    entities = extract_entities("What is the fee?")
    contract = build_contract("What is the fee?", entities, ctx=None)
    assert contract.fee_type is None
    assert contract.topic == "fee"


def test_contract_fee_type_programme_context_no_fee_type():
    """Contract with programme context but no fee_type should have fee_type=None."""
    entities = extract_entities("What is the fee?")
    ctx = ConversationContext()
    ctx.programme = "mca"
    contract = build_contract("What is the fee?", entities, ctx=ctx)
    # fee_type is None because not explicitly mentioned and no context fee_type
    assert contract.fee_type is None
    assert contract.programme == "mca"
    assert contract.topic == "fee"


def test_fee_type_persists_in_contract_dict():
    """fee_type should be serializable in contract.as_dict()."""
    message = "What is the examination fee?"
    entities = extract_entities(message)
    contract = build_contract(message, entities, ctx=None)
    d = contract.as_dict()
    assert d["fee_type"] == "examination"


def test_examination_fee_overrides_programme_fee_in_same_message():
    """Explicit examination fee in message should override context programme fee_type."""
    ctx = ConversationContext()
    ctx.programme = "mca"
    ctx.fee_type = "programme"  # previous context
    entities = extract_entities("What is the examination fee?")
    contract = build_contract("What is the examination fee?", entities, ctx=ctx)
    # Explicit current message wins
    assert contract.fee_type == "examination"


def test_programme_fee_overrides_examination_in_context():
    """Explicit programme fee in message should override context examination fee_type."""
    ctx = ConversationContext()
    ctx.programme = "mca"
    ctx.fee_type = "examination"  # previous context
    entities = extract_entities("What is the programme fee?")
    contract = build_contract("What is the programme fee?", entities, ctx=ctx)
    # Explicit current message wins
    assert contract.fee_type == "programme"


def test_unrelated_query_does_not_get_fee_type():
    """Unrelated numeric queries should not get fee_type."""
    entities = extract_entities("How many credits does MCA have?")
    contract = build_contract("How many credits does MCA have?", entities, ctx=None)
    assert contract.topic != "fee"
    assert contract.fee_type is None

    entities = extract_entities("How long is MCA?")
    contract = build_contract("How long is MCA?", entities, ctx=None)
    assert contract.topic != "fee"
    assert contract.fee_type is None


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))