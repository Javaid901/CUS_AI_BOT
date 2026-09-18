"""
backend/tests/test_p0_bm25_empty_refresh.py

P0 regression coverage: the BM25 index must never keep serving a previously
built (stale) index after the authoritative chunk source becomes empty.

Contract under test:
  * A non-empty refresh builds a searchable index.
  * When the source returns no chunks, refresh() clears the in-memory state
    and drops back to the unready state instead of retaining stale chunks.
  * search() then yields no hits.

These tests are fully isolated: the source read is monkeypatched, so neither
Chroma nor the database is touched.

Run (from backend/):  python -m pytest tests/test_p0_bm25_empty_refresh.py -q
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


from app.ingest import retriever  # noqa: E402
from app.ingest.retriever import BM25Index  # noqa: E402


def _chunks() -> list[dict]:
    return [
        {"id": "d1_0", "content": "admission eligibility criteria for bca programme", "document_title": "Prospectus"},
        {"id": "d1_1", "content": "examination fee structure for the current session", "document_title": "Prospectus"},
    ]


def test_bm25_refresh_with_non_empty_source_is_functional(monkeypatch):
    idx = BM25Index()
    monkeypatch.setattr(retriever, "get_all_chunks", lambda limit=20000: _chunks())
    idx.refresh()
    assert idx._ready is True
    assert len(idx._chunks) == 2
    hits = idx.search("admission eligibility", top_k=5)
    assert hits, "expected BM25 hits for a present term"


def test_bm25_empty_source_clears_stale_state(monkeypatch):
    idx = BM25Index()
    monkeypatch.setattr(retriever, "get_all_chunks", lambda limit=20000: _chunks())
    idx.refresh()
    assert idx._ready is True and len(idx._chunks) == 2

    # Source is now empty (e.g. every vector removed). The interval gate must
    # not preserve stale data.
    idx._last_refresh = 0.0
    monkeypatch.setattr(retriever, "get_all_chunks", lambda limit=20000: [])
    idx.refresh()

    assert idx._ready is False
    assert idx._chunks == []
    assert idx._term_freqs == []
    assert idx._doc_lens == []
    assert idx._idf == {}


def test_bm25_search_returns_nothing_after_source_empties(monkeypatch):
    idx = BM25Index()
    monkeypatch.setattr(retriever, "get_all_chunks", lambda limit=20000: _chunks())
    idx.refresh()
    assert idx.search("admission", top_k=5)

    idx._last_refresh = 0.0
    monkeypatch.setattr(retriever, "get_all_chunks", lambda limit=20000: [])
    idx.refresh()
    assert idx.search("admission", top_k=5) == []


def test_bm25_refresh_recovers_after_source_repopulates(monkeypatch):
    idx = BM25Index()
    monkeypatch.setattr(retriever, "get_all_chunks", lambda limit=20000: [])
    idx.refresh()
    assert idx._ready is False

    monkeypatch.setattr(retriever, "get_all_chunks", lambda limit=20000: _chunks())
    idx.refresh()
    assert idx._ready is True
    assert len(idx._chunks) == 2
    assert idx.search("fee", top_k=5)
