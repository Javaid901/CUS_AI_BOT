"""
backend/tests/test_document_trust.py - Phase 1 review-lifecycle mapping.

classification_state_for rules:
  * HTML knowledge pages        -> verified (nothing to review)
  * KNOWLEDGE binary document   -> pending_review
  * OFFICIAL / AMBIGUOUS        -> pending_review
  * model-paper (always)        -> pending_review  (hold for review)

Run:  python tests/test_document_trust.py   (or pytest tests/test_document_trust.py)
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.knowledge_sync.document_classifier import classification_state_for

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  PASS  {name}" + (f"  {detail}" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")


def _result(doc_type: str, category: str) -> dict:
    return {
        "doc_type": doc_type,
        "category": category,
        "confidence": {"band": "high", "score": 90},
        "signals": [],
    }


def test_state_mapping() -> None:
    print("-- classification_state_for mapping --")
    html_knowledge = _result("knowledge", "admissions")
    doc_knowledge = _result("knowledge", "downloads")
    date_sheet = _result("official", "date-sheet")
    model_paper = _result("official", "model-paper")
    ambiguous = _result("ambiguous", "ambiguous")

    check("html knowledge -> verified", classification_state_for(html_knowledge, is_document=False) == "verified")
    check("binary knowledge document -> pending_review", classification_state_for(doc_knowledge, is_document=True) == "pending_review")
    check("date-sheet -> pending_review", classification_state_for(date_sheet, is_document=True) == "pending_review")
    check("model-paper -> pending_review (label+hold)", classification_state_for(model_paper, is_document=True) == "pending_review")
    check("ambiguous -> pending_review", classification_state_for(ambiguous, is_document=True) == "pending_review")


def test_model_paper_never_verified_automatically() -> None:
    print("-- model paper even at HIGH confidence stays pending_review --")
    for is_doc in (True, False):
        state = classification_state_for(_result("official", "model-paper"), is_document=is_doc)
        check(f"model-paper is_document={is_doc} -> pending_review", state == "pending_review", state)


def test_knowledge_document_requires_review() -> None:
    print("-- knowledge-typed binary must still be reviewed --")
    for category in ("downloads", "notices", "policies"):
        state = classification_state_for(_result("knowledge", category), is_document=True)
        check(f"knowledge doc {category} -> pending_review", state == "pending_review", state)


def main() -> None:
    test_state_mapping()
    test_model_paper_never_verified_automatically()
    test_knowledge_document_requires_review()
    print()
    print("SUMMARY")
    print(f"  Passed: {len(PASS)}/{len(PASS) + len(FAIL)}")
    print(f"  Failed: {len(FAIL)}/{len(PASS) + len(FAIL)}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()