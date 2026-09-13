"""
backend/tests/test_document_versioning.py - Phase 1 raw preservation on update.

Drive the real engine against a local site serving a .txt document, change its
content between runs, and verify:
  * the pre-update raw bytes are snapshotted into a WebsitePageVersion
  * the page gets a fresh raw file + sha256 for the new content
  * both raw files remain on disk (old raw is preserved, never deleted)
  * classification fields persist through the update

Run:  python tests/test_document_versioning.py   (or pytest tests/test_document_versioning.py)
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
    str(Path(os.environ.get("TEMP", tempfile.gettempdir())) / "_cus_phase1_test_state.json"),
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


V1 = b"UG End Semester Date Sheet 2026\nMonday 20 April 2026\n"
V2 = b"UG End Semester Date Sheet 2026 REVISED\nMonday 27 April 2026\n"

SITE: dict[str, bytes] = {
    "/": b"<html><head><title>Home</title></head><body><a href=\"/notices/datesheet.txt\">Sheet</a></body></html>",
    "/notices/datesheet.txt": V1,
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
        if path.endswith(".txt"):
            body = b"text/plain; charset=utf-8".join([b""])  # noqa: F841
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "text/plain; charset=utf-8" if path.endswith(".txt") else "text/html; charset=utf-8",
        )
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


def test_raw_preserved_across_update() -> None:
    print("-- raw preserved across content update --")
    from app.knowledge_sync.web_engine import WebsiteSyncEngine

    _wipe()
    create_all()
    port, srv = _start_server()
    try:
        base = f"http://127.0.0.1:{port}"
        db = SessionLocal()
        try:
            engine = WebsiteSyncEngine(db, base_url=base, index_rag=False, allow_private_hosts=True)
            r1 = engine.run(trigger="manual")
            page = db.query(WebsitePage).filter(WebsitePage.url.endswith("datesheet.txt")).first()
            check("doc created", page is not None and page.status == "new", (page.status if page else "missing"))
            check("classified date-sheet", page is not None and page.category == "date-sheet", (page.category if page else "?"))
            check("doc_type official", page is not None and page.doc_type == "official", (page.doc_type if page else "?"))
            check("pending_review", page is not None and page.classification_status == "pending_review", (page.classification_status if page else "?"))
            check("raw stored round 1", page is not None and page.raw_path and page.raw_sha256, (page.raw_path if page else "none"))
            old_rel = page.raw_path if page else None
            old_sha = page.raw_sha256 if page else None

            SITE["/notices/datesheet.txt"] = V2
            r2 = engine.run(trigger="manual")
            fresh = db.get(WebsitePage, page.id) if page else None
            check("updated counted", r2.get("updated_pages", 0) >= 1, str(r2.get("updated_pages")))
            check("version bumped", fresh is not None and (fresh.version or 1) == 2, f"v={fresh.version if fresh else None}")
            check("new raw differs", fresh is not None and fresh.raw_path != old_rel, f"{fresh.raw_path if fresh else None} vs {old_rel}")
            check("new sha differs", fresh is not None and fresh.raw_sha256 != old_sha, f"{fresh.raw_sha256 if fresh else None} vs {old_sha}")

            versions = (
                db.query(WebsitePageVersion)
                .filter(WebsitePageVersion.page_id == page.id)
                .order_by(WebsitePageVersion.version.asc())
                .all()
                if page
                else []
            )
            check("snapshot archived", any(v.version == 1 for v in versions), f"v={[v.version for v in versions]}")
            old_version = next((v for v in versions if v.version == 1), None)
            check("snapshot keeps raw_path", old_version is not None and bool(old_version.raw_path and old_version.raw_sha256), (old_version.raw_path if old_version else "none"))
            check("snapshot sha matches round1", old_version is not None and old_version.raw_sha256 == old_sha, (old_version.raw_sha256 if old_version else "none"))

            from app.knowledge_sync.raw_store import resolve_contained

            if old_version and old_version.raw_path:
                old_disk = resolve_contained(old_version.raw_path)
                check("old raw file preserved on disk", old_disk is not None and old_disk.is_file(), str(old_disk))
            if fresh and fresh.raw_path:
                new_disk = resolve_contained(fresh.raw_path)
                check("new raw file exists", new_disk is not None and new_disk.is_file(), str(new_disk))
        finally:
            db.close()
    finally:
        srv.shutdown()
        srv.server_close()


def main() -> None:
    test_raw_preserved_across_update()
    print()
    print("SUMMARY")
    print(f"  Passed: {len(PASS)}/{len(PASS) + len(FAIL)}")
    print(f"  Failed: {len(FAIL)}/{len(PASS) + len(FAIL)}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()