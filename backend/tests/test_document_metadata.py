"""
backend/tests/test_document_metadata.py — Phase 1 metadata extraction.

  * exact size + sha256 from raw bytes
  * mime mapping incl. office overrides
  * content_type from magic bytes (never a filename guess)
  * PDF page_count via "/Type /Page" markers (best-effort, only PDF)
  * safe display title derived from the actual filename
  * table_like heuristic -> None when there is no text to judge
  * unknown values are None (nothing is fabricated)

Run:  python tests/test_document_metadata.py   (or pytest tests/test_document_metadata.py)
"""

from __future__ import annotations

import hashlib
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.knowledge_sync.web_metadata import extract_meta

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  PASS  {name}" + (f"  {detail}" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")


def test_pdf_metadata() -> None:
    print("-- PDF metadata --")
    raw = b"%PDF-1.4\n1 0 obj\n/Type /Page\nendobj\n2 0 obj\n/Type /Page\nendobj\n"
    r = extract_meta(filename="UG_Date_Sheet.pdf", url="https://x/notices/UG_Date_Sheet.pdf", ext=".PDF", raw=raw)
    check("content_type pdf", r["content_type"] == "pdf", str(r["content_type"]))
    check("mime application/pdf", r["mime"] == "application/pdf", str(r["mime"]))
    check("size exact", r["size_bytes"] == len(raw), str(r["size_bytes"]))
    check("sha256 exact", r["sha256"] == hashlib.sha256(raw).hexdigest())
    check("page_count via markers", r["page_count"] == 2, str(r["page_count"]))
    check("title = filename", r["title"] == "UG_Date_Sheet.pdf", str(r["title"]))


def test_office_mime_overrides() -> None:
    print("-- office mime overrides from detected magic --")
    for ext_name in ("docx", "xlsx", "pptx"):
        raw = b"\x00" * 0
    import io
    import zipfile

    def zipped(entry: str) -> bytes:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr(entry, "<x/>")
        return buf.getvalue()

    cases = {
        "docx": ("application/vnd.openxmlformats-officedocument.wordprocessingml.document", zipped("word/document.xml")),
        "xlsx": ("application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", zipped("xl/workbook.xml")),
        "pptx": ("application/vnd.openxmlformats-officedocument.presentationml.presentation", zipped("ppt/slides/slide1.xml")),
    }
    for ext, (mime, raw) in cases.items():
        r = extract_meta(filename=f"file.{ext}", url=f"https://x/{ext}/f.{ext}", ext=ext, raw=raw)
        check(f"{ext} content_type", r["content_type"] == ext, str(r["content_type"]))
        check(f"{ext} mime override", r["mime"] == mime, str(r["mime"]))
        check(f"{ext} size", r["size_bytes"] == len(raw))
        check(f"{ext} sha256", r["sha256"] == hashlib.sha256(raw).hexdigest())
        check(f"{ext} no fabricated page_count", r["page_count"] is None, str(r["page_count"]))


def test_table_like_heuristic() -> None:
    print("-- table_like heuristic --")
    tab = "A | B | C\n1 | 2 | 3\nx | y | z\n"
    r = extract_meta(filename="t.txt", url="https://x/t.txt", ext="txt", raw=b"x", extracted_text=tab)
    check("tabular text => True", r["table_like"] is True, str(r["table_like"]))
    prose = "This is a simple paragraph without any table structure at all."
    r2 = extract_meta(filename="t.txt", url="https://x/t.txt", ext="txt", raw=b"x", extracted_text=prose)
    check("prose text => False", r2["table_like"] is False, str(r2["table_like"]))
    r3 = extract_meta(filename="t.txt", url="https://x/t.txt", ext="txt", raw=b"x", extracted_text="")
    check("no text => None", r3["table_like"] is None, str(r3["table_like"]))


def test_unknowns_are_none() -> None:
    print("-- unknown values are None, never fabricated --")
    r = extract_meta(filename="", url="", ext="", raw=None, extracted_text="")
    check("content_type None", r["content_type"] is None, str(r["content_type"]))
    check("mime None", r["mime"] is None, str(r["mime"]))
    check("size None", r["size_bytes"] is None, str(r["size_bytes"]))
    check("sha256 None", r["sha256"] is None, str(r["sha256"]))
    check("title None", r["title"] is None, str(r["title"]))
    check("page_count None", r["page_count"] is None, str(r["page_count"]))


def test_safe_title() -> None:
    print("-- safe title derivation --")
    r = extract_meta(filename="../../evil/../Date_Sheet_2026.pdf", ext="pdf", raw=b"%PDF-", extracted_text="")
    check("title uses basename only", r["title"] == "Date_Sheet_2026.pdf", str(r["title"]))
    r2 = extract_meta(url="https://x/notices/notice.pdf?download=1", ext="pdf", raw=b"%PDF-", extracted_text="")
    check("query string stripped", r2["title"] == "notice.pdf", str(r2["title"]))


def main() -> None:
    test_pdf_metadata()
    test_office_mime_overrides()
    test_table_like_heuristic()
    test_unknowns_are_none()
    test_safe_title()
    print()
    print("SUMMARY")
    print(f"  Passed: {len(PASS)}/{len(PASS) + len(FAIL)}")
    print(f"  Failed: {len(FAIL)}/{len(PASS) + len(FAIL)}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()