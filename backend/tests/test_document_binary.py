"""
backend/tests/test_document_binary.py — Phase 1 binary magic-byte sniffing.

  * PDF, DOCX, XLSX, PPTX identified from magic bytes / zip structure
  * unknown / empty / garbage blobs -> None (never a filename guess)

Run:  python tests/test_document_binary.py   (or pytest tests/test_document_binary.py)
"""

from __future__ import annotations

import io
import sys
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.knowledge_sync.document_classifier import detect_binary_ext

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  PASS  {name}" + (f"  {detail}" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")


def _zip_with(entries: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name in entries:
            zf.writestr(name, "<x/>")
    return buf.getvalue()[:1000000]


def test_pdf() -> None:
    print("-- PDF sniffing --")
    check("pdf magic", detect_binary_ext(b"%PDF-1.4\n...") == "pdf")
    check("pdf with binary header", detect_binary_ext(b"%PDF-1.7" + b"\x00" * 10) == "pdf")
    check("pdf offset tolerated", detect_binary_ext(b"\x00\x00%PDF-1.4") == "pdf")


def test_office_zip_formats() -> None:
    print("-- DOCX / XLSX / PPTX via zip container structure --")
    check("docx", detect_binary_ext(_zip_with(["word/document.xml", "[Content_Types].xml"])) == "docx")
    check("xlsx", detect_binary_ext(_zip_with(["xl/workbook.xml", "[Content_Types].xml"])) == "xlsx")
    check("pptx", detect_binary_ext(_zip_with(["ppt/presentation.xml", "[Content_Types].xml"])) == "pptx")
    check("plain zip is not office", detect_binary_ext(_zip_with(["random/entry.txt"])) is None)


def test_unknown_and_empty() -> None:
    print("-- unknown / empty / garbage --")
    check("none -> None", detect_binary_ext(None) is None)
    check("empty -> None", detect_binary_ext(b"") is None)
    check("junk -> None", detect_binary_ext(b"\x00\x01\x02\x03 not a real file") is None)
    check("truncated zip -> None", detect_binary_ext(b"PK\x03\x04\x00XJUNK") is None)


def main() -> None:
    test_pdf()
    test_office_zip_formats()
    test_unknown_and_empty()
    print()
    print("SUMMARY")
    print(f"  Passed: {len(PASS)}/{len(PASS) + len(FAIL)}")
    print(f"  Failed: {len(FAIL)}/{len(PASS) + len(FAIL)}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()