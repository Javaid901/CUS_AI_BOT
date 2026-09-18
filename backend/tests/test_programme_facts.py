"""
backend/tests/test_programme_facts.py

Phase 3A — ProgrammeFacts read layer tests.

Verifies:
  1. complete PG enumeration (count, order, no duplicates)
  2. level / scheme filtering
  3. single programme retrieval (all key fields present)
  4. subject integration (subject_name, semester, category present)
  5. unknown programme → no substitution (None / empty)
  6. multi-attribute coexistence (fee + eligibility + duration + credits)
  7. deduplication by programme_id
  8. feature flag OFF compatibility (existing legacy path unchanged)

Run:  python -m pytest tests/test_programme_facts.py -q
"""

from __future__ import annotations

import asyncio
import json
import uuid

import pytest

from app.database import SessionLocal, create_all
from app.catalogue.seed import seed_catalogue
from app.catalogue.facts import ProgrammeFactsSet, get_programme_facts, list_programme_facts, filter_programme_facts
from app.orchestrator.context import ConversationContext
from app.orchestrator.extractor import extract_entities

create_all()

# ---------------------------------------------------------------------------
# Module-scoped seed (idempotent, runs once per test module load).
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module", autouse=True)
def _seed():
    db = SessionLocal()
    try:
        seed_catalogue(db)
        db.commit()
    finally:
        db.close()


# ---------------------------------------------------------------------------
# TEST 1 — Complete PG enumeration (count, deterministic order, no dupes)
# ---------------------------------------------------------------------------

def test_list_all_pg_programmes():
    result = list_programme_facts(level="pg")

    assert isinstance(result, ProgrammeFactsSet)
    assert result.mode == "enumerate"
    assert result.complete is True
    # Demo catalogue has exactly three PG programmes.
    assert len(result.items) == 3
    codes = [p.code for p in result.items]
    # Ordered by full programme name (list_programmes orders by Programme.name).
    assert codes == ["MBA", "M.Com", "MCA"], f"unexpected order: {codes}"
    ids = [p.programme_id for p in result.items]
    assert len(ids) == len(set(ids)), "duplicate programme_id in result"


# ---------------------------------------------------------------------------
# TEST 2 — Level and scheme filtering
# ---------------------------------------------------------------------------

def test_filter_by_level_ug():
    codes = [p.code for p in list_programme_facts(level="ug").items]
    assert len(codes) == 5
    assert set(codes) == {"BCA", "BBA", "BA English", "B.Com", "B.Sc"}


def test_filter_by_scheme_nep():
    result = list_programme_facts(scheme="nep2020")
    assert result.complete is True
    codes = {p.code for p in result.items}
    # Demo: BCA, BBA, BA English, B.Com, MCA, M.Com, PhD CS
    assert codes == {"BCA", "BBA", "BA English", "B.Com", "MCA", "M.Com", "PhD CS"}
    assert len(result.items) == 7


def test_filter_by_scheme_traditional():
    result = list_programme_facts(scheme="traditional")
    codes = {p.code for p in result.items}
    assert codes == {"B.Sc", "MBA"}


# ---------------------------------------------------------------------------
# TEST 3 — Single programme retrieval (all key fields)
# ---------------------------------------------------------------------------

def test_get_single_programme_mca():
    mca = get_programme_facts("MCA")
    assert mca is not None
    assert mca.name == "Master of Computer Applications"
    assert mca.code == "MCA"
    assert mca.level == "pg"
    assert mca.duration_years == 2
    assert mca.total_credits == 120
    assert mca.eligibility is not None
    assert "Mathematics" in mca.eligibility
    assert len(mca.fee_structure) == 3  # Admission + Tuition + Examination
    assert mca.fee_structure[0]["value"] == "Rs. 6,000"


def test_get_single_programme_by_uuid():
    all_items = list_programme_facts().items
    bca = get_programme_facts(all_items[0].programme_id)
    assert bca is not None
    assert bca.programme_id == all_items[0].programme_id
    assert bca.name == all_items[0].name


def test_get_single_programme_by_alias():
    # The alias "mca" (lowercase code) resolves deterministically.
    mca = get_programme_facts("mca")
    assert mca is not None
    assert mca.code == "MCA"


# ---------------------------------------------------------------------------
# TEST 4 — Subject integration
# ---------------------------------------------------------------------------

def test_subjects_present_for_bsc():
    bsc = get_programme_facts("B.Sc")
    assert bsc is not None
    assert bsc.subject_count >= 1
    assert len(bsc.subjects) == bsc.subject_count
    for subj in bsc.subjects:
        assert "subject_name" in subj and subj["subject_name"]
        assert "semester" in subj and isinstance(subj["semester"], int)
        assert subj["category"] in {"major", "minor", "vac", "sec", "aec", "generic"}


# ---------------------------------------------------------------------------
# TEST 5 — Unknown programme → no substitution
# ---------------------------------------------------------------------------

def test_unknown_programme_returns_none():
    assert get_programme_facts("MSc") is None
    assert get_programme_facts("Quantum Computing") is None
    assert get_programme_facts("XYZ-UNKNOWN-123") is None


def test_unknown_programme_filter_returns_empty():
    result = filter_programme_facts(programme="MSc")
    assert isinstance(result, ProgrammeFactsSet)
    assert len(result.items) == 0
    assert result.mode == "filter"
    assert result.complete is False


# ---------------------------------------------------------------------------
# TEST 6 — Multi-attribute coexistence (fee + eligibility + duration + credits)
# ---------------------------------------------------------------------------

def test_multi_attribute_coexistence():
    bca = get_programme_facts("BCA")
    d = bca.as_dict()
    assert isinstance(d["fee_structure"], list) and len(d["fee_structure"]) >= 1
    assert isinstance(d["eligibility"], str) and len(d["eligibility"]) > 10
    assert isinstance(d["duration_years"], int) and d["duration_years"] >= 1
    assert isinstance(d["total_credits"], int) and d["total_credits"] >= 1
    # JSON-safe round-trip.
    json.dumps(d)


# ---------------------------------------------------------------------------
# TEST 7 — Deduplication
# ---------------------------------------------------------------------------

def test_deduplication_by_programme_id():
    result = list_programme_facts()
    assert len(result.items) == 9  # full demo catalogue
    codes = [p.code for p in result.items]
    assert len(codes) == len(set(codes))
    ids = [p.programme_id for p in result.items]
    assert len(ids) == len(set(ids))


# ---------------------------------------------------------------------------
# TEST 8 — Feature flag OFF compatibility
# ---------------------------------------------------------------------------

def test_flag_off_legacy_path():
    from app.config import settings

    assert settings.INTELLIGENCE_ENABLED is False

    from app.orchestrator.planner import plan

    # Catalogue query still routes to the existing catalogue action (legacy path).
    ctx = ConversationContext()
    p = plan("fee structure of BCA", ctx, "facts-off-fee", extract_entities("fee structure of BCA"))
    assert p.action == "catalogue"
    assert "intelligence" not in (p.extra or {})

    # Navigation fallback unchanged.
    p2 = plan("back", ctx, "facts-off-back", extract_entities("back"))
    assert p2.action in ("navigation", "welcome")
    assert "intelligence" not in (p2.extra or {})
