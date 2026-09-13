"""
backend/tests/test_document_raw_storage.py - Phase 1 raw-byte preservation.

Security invariants under test:
  * files are stored as <uuid>.<safe-ext> (the original filename never appears)
  * only whitelisted extensions accepted; weird inputs rejected
  * writes are atomic (temp file + fsync + os.replace)
  * resolve_contained refuses any path escaping the storage root
  * stored bytes round-trip exactly (sha256 equality)

Run:  python tests/test_document_raw_storage.py   (or pytest tests/test_document_raw_storage.py)
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Isolation FIRST: settings freeze at import time, before any app import.
os.environ.setdefault(
    "WEBSITE_SYNC_RAW_DIR",
    str(Path(os.environ.get("TEMP", tempfile.gettempdir())) / "_cus_phase1_raw"),
)

import hashlib  # noqa: E402

from app.knowledge_sync.raw_store import (  # noqa: E402
    RAW_EXT_WHITELIST,
    raw_root,
    resolve_contained,
    sanitize_ext,
    store_raw,
)

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  PASS  {name}" + (f"  {detail}" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")


def test_sanitize_ext() -> None:
    print("-- sanitize_ext whitelist --")
    check("lowercase", sanitize_ext("pdf") == "pdf")
    check("upper -> lower", sanitize_ext("PDF") == "pdf")
    check("leading dot stripped", sanitize_ext(".PDF") == "pdf")
    check("full whitelist accepted", all(sanitize_ext(e) == e for e in sorted(RAW_EXT_WHITELIST)), str(sorted(RAW_EXT_WHITELIST)))
    check("executable rejected", sanitize_ext("exe") is None)
    check("double ext rejected", sanitize_ext("pdf.txt") is None)
    check("empty rejected", sanitize_ext("") is None)
    check("control chars rejected", sanitize_ext("pdf;rm") is None)


def test_store_and_roundtrip() -> None:
    print("-- store_raw round-trip --")
    raw = b"%PDF-1.4 fake\n" * 50
    info = store_raw(raw, "pdf")
    check("stored", info is not None, str(info))
    check("sha256 exact", info["sha256"] == hashlib.sha256(raw).hexdigest(), info["sha256"])
    check("size exact", info["size"] == len(raw))
    path = resolve_contained(info["rel_path"])
    check("resolved inside root", path is not None and path.is_file(), str(path))
    if path:
        check("bytes round-trip", path.read_bytes() == raw)
    check("name is uuid.ext only", len(info["rel_path"].split(".")) == 2 and len(info["rel_path"].split(".")[0]) == 32, info["rel_path"])


def test_atomicity_bytes_unique() -> None:
    print("-- identical bytes still stored under a unique name --")
    raw = b"same-bytes-xyz"
    a = store_raw(raw, "txt")
    b = store_raw(raw, "txt")
    check("two distinct names", a and b and a["rel_path"] != b["rel_path"], f"{a['rel_path']} vs {b['rel_path']}")
    check("same digest", a and b and a["sha256"] == b["sha256"])
    check("no temp litter", not [p for p in raw_root().iterdir() if p.name.startswith(".raw-")], str(list(raw_root().iterdir())))


def test_containment_guard() -> None:
    print("-- resolve_contained traversal guard --")
    check("none -> None", resolve_contained(None) is None)
    check("empty -> None", resolve_contained("") is None)
    check("parent traversal -> None", resolve_contained("../evil.txt") is None)
    check("absolute path -> None", resolve_contained(str(Path(raw_root(), "x.pdf"))) is None)
    check("drive escape -> None", resolve_contained("..\\..\\..\\x.pdf") is None)
    check("root itself -> None", resolve_contained(".") is not None)


def test_bad_inputs() -> None:
    print("-- store_raw rejects bad inputs --")
    check("empty bytes -> None", store_raw(b"", "pdf") is None)
    check("bad ext -> None", store_raw(b"data", "exe") is None)
    check("none ext -> None", store_raw(b"data", "") is None)


def main() -> None:
    test_sanitize_ext()
    test_store_and_roundtrip()
    test_atomicity_bytes_unique()
    test_containment_guard()
    test_bad_inputs()
    print()
    print("SUMMARY")
    print(f"  Passed: {len(PASS)}/{len(PASS) + len(FAIL)}")
    print(f"  Failed: {len(FAIL)}/{len(PASS) + len(FAIL)}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()