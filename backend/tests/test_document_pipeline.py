"""
backend/tests/test_document_pipeline.py - Phase 1 end-to-end engine pipeline.

Crawl a local site with HTML + documents and verify the full ingestion chain:
  * HTML knowledge page             -> knowledge / verified
  * date-sheet PDF                  -> official/date-sheet / pending_review
  * model-paper PDF                 -> official/model-paper / pending_review (held)
  * "MCA Previous Year QP" PDF      -> NOT model-paper (hard exclusion)
  * EVSSem1.pdf                     -> ambiguous / pending_review
  * raw bytes preserved for documents, raw metadata populated
  * the intelligence layer NEVER aborts a sync (failure-resilience)

Run:  python tests/test_document_pipeline.py   (or pytest tests/test_document_pipeline.py)
"""

from __future__ import annotations

import http.server
import os
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# Isolation FIRST: settings freeze at import time, before any app import.
os.environ.setdefault(
    "WEBSITE_SYNC_STATE_FILE",
    str(Path(os.environ.get("TEMP", tempfile.gettempdir())) / "_cus_phase1_pipeline_state.json"),
)
os.environ.setdefault(
    "WEBSITE_SYNC_RAW_DIR",
    str(Path(os.environ.get("TEMP", tempfile.gettempdir())) / "_cus_phase1_raw"),
)

import app.models  # noqa: F401  (register tables before any session)

from app.database import SessionLocal, create_all  # noqa: E402
from app.models.website_sync import CrawlRun, WebsitePage, WebsitePageVersion  # noqa: E402

create_all()

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    if cond:
        PASS.append(name)
        print(f"  PASS  {name}" + (f"  {detail}" if detail else ""))
    else:
        FAIL.append(name)
        print(f"  FAIL  {name}  {detail}")


SITE: dict[str, bytes] = {
    "/": (
        b"<html><head><title>Home</title></head><body><a href=\"/admissions\">Admissions</a>"
        b"<a href=\"/notices/UG_Date_Sheet_2026.pdf\">DS</a>"
        b"<a href=\"/exams/Model_Paper_EVS.pdf\">MP</a>"
        b"<a href=\"/notices/MCA_Previous_Year_Question_Paper.pdf\">PYQ</a>"
        b"<a href=\"/downloads/EVSSem1.pdf\">EVS</a></body></html>"
    ),
    "/admissions": b"<html><head><title>Admissions 2026</title></head><body><p>PG admission notification.</p></body></html>",
    "/notices/UG_Date_Sheet_2026.pdf": b"%PDF-1.4 fake datesheet",
    "/exams/Model_Paper_EVS.pdf": b"%PDF-1.4 fake model paper",
    "/notices/MCA_Previous_Year_Question_Paper.pdf": b"%PDF-1.4 fake pyq",
    "/downloads/EVSSem1.pdf": b"%PDF-1.4 fake evs sem",
}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        path = self.path.split("?", 1)[0]
        body = SITE.get(path)
        if body is None:
            self.send_response(404)
            self.send_header("Content-Length", "0")
            self.end_headers()
            return
        ctype = "application/pdf" if path.endswith(".pdf") else "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def _start_server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv.server_address[1], srv


def _wipe():
    db = SessionLocal()
    try:
        for model in (WebsitePage, CrawlRun, WebsitePageVersion):
            db.query(model).delete()
        db.commit()
    finally:
        db.close()


def test_full_pipeline() -> None:
    print("-- engine end-to-end Phase 1 pipeline --")
    from app.knowledge_sync.web_engine import WebsiteSyncEngine

    _wipe()
    create_all()
    port, srv = _start_server()
    try:
        base = f"http://127.0.0.1:{port}"
        db = SessionLocal()
        try:
            engine = WebsiteSyncEngine(db, base_url=base, index_rag=False, allow_private_hosts=True)
            r = engine.run(trigger="manual")
            check("sync completed no crash", r.get("status") in ("completed", None) or r.get("failed_pages", 0) == 0, str(r))

            pages = {p.url.rsplit("/", 1)[-1]: p for p in db.query(WebsitePage).all()}

            adm = pages.get("admissions")
            check("html knowledge page present", adm is not None, "missing")
            check("html knowledge", adm is not None and adm.doc_type == "knowledge", (adm.doc_type if adm else "?"))
            check("html verified", adm is not None and adm.classification_status == "verified", (adm.classification_status if adm else "?"))
            check("html no raw", adm is not None and not adm.raw_path, (adm.raw_path if adm else "none"))

            ds = pages.get("UG_Date_Sheet_2026.pdf")
            check("date sheet present", ds is not None, "missing")
            check("date sheet official", ds is not None and ds.doc_type == "official", (ds.doc_type if ds else "?"))
            check("date sheet category", ds is not None and ds.category == "date-sheet", (ds.category if ds else "?"))
            check("date sheet pending_review", ds is not None and ds.classification_status == "pending_review", (ds.classification_status if ds else "?"))
            check("date sheet raw preserved", ds is not None and bool(ds.raw_path and ds.raw_sha256), (ds.raw_path if ds else "none"))
            check("date sheet meta content_type", ds is not None and (ds.doc_meta or {}).get("content_type") == "pdf", str((ds.doc_meta or {}).get("content_type")))

            mp = pages.get("Model_Paper_EVS.pdf")
            check("model paper present", mp is not None, "missing")
            check("model paper category", mp is not None and mp.category == "model-paper", (mp.category if mp else "?"))
            check("model paper held for review", mp is not None and mp.classification_status == "pending_review", (mp.classification_status if mp else "?"))

            pyq = pages.get("MCA_Previous_Year_Question_Paper.pdf")
            check("PYQ present", pyq is not None, "missing")
            check("PYQ NOT model-paper", pyq is not None and pyq.category != "model-paper", (pyq.category if pyq else "?"))

            evs = pages.get("EVSSem1.pdf")
            check("EVSSem1 present", evs is not None, "missing")
            check("EVSSem1 ambiguous", evs is not None and evs.doc_type == "ambiguous" and evs.category == "ambiguous", f"{evs.doc_type if evs else '?'}/{evs.category if evs else '?'}")
            check("EVSSem1 pending_review", evs is not None and evs.classification_status == "pending_review", (evs.classification_status if evs else "?"))

            from app.knowledge_sync.raw_store import resolve_contained

            for p in (ds, mp, pyq, evs):
                if p and p.raw_path:
                    disk = resolve_contained(p.raw_path)
                    check(f"raw on disk {p.url.rsplit('/', 1)[-1]}", disk is not None and disk.is_file(), str(disk))
        finally:
            db.close()
    finally:
        srv.shutdown()
        srv.server_close()


def main() -> None:
    test_full_pipeline()
    print()
    print("SUMMARY")
    print(f"  Passed: {len(PASS)}/{len(PASS) + len(FAIL)}")
    print(f"  Failed: {len(FAIL)}/{len(PASS) + len(FAIL)}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()