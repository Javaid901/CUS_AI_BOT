"""
backend/tests/test_backend_connectivity.py

Connectivity / startup-lifecycle regression tests for the backend serving the
frontend on the port the browser actually requests.

Regression covered by this module:

  1. The backend must be reachable where the frontend looks for it.
     frontend/js/config.js resolves the default API to ``http://<host>:8001``,
     while the old ``settings.PORT`` default was 8000 and the root README
     started the app with a bare ``uvicorn app.main:app`` (which binds uvicorn
     CLI default 8000).  A user following that documented start ran a healthy
     backend on 8000 while the browser could not connect on 8001.  The config
     default and the documented start now both use 8001, matching
     .env(.example), the backend README and the frontend.

  2. The FastAPI application startup/shutdown lifecycle still runs correctly,
     including the shared async Ollama streaming client installed by the
     startup handler (``app.ingest.generator.install_async_client``) and closed
     by the shutdown handler (``close_async_client``).

Run:  python -m pytest tests/test_backend_connectivity.py -q
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.ingest import generator
from app.main import app

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
BACKEND_ROOT = Path(__file__).resolve().parent.parent
FRONTEND_CONFIG_JS = REPO_ROOT / "frontend" / "js" / "config.js"
ROOT_README = REPO_ROOT / "README.md"


def test_settings_default_port_is_the_frontend_port() -> None:
    assert Settings.model_fields["PORT"].default == 8001
    assert Settings().PORT == 8001


def test_frontend_config_defaults_to_the_configured_backend_port() -> None:
    if not FRONTEND_CONFIG_JS.exists():
        pytest.skip("frontend/js/config.js not present in this checkout")
    text = FRONTEND_CONFIG_JS.read_text(encoding="utf-8")
    assert re.search(r"DEFAULT_PORT\s*=\s*8001", text)


def test_readme_documented_start_binds_the_frontend_port() -> None:
    if not ROOT_README.exists():
        pytest.skip("root README not present in this checkout")
    text = ROOT_README.read_text(encoding="utf-8")
    startup_step = text.split("Start backend", 1)[1].splitlines()[0]
    assert "8001" in startup_step or "python run.py" in startup_step


@pytest.fixture(scope="module")
def client():
    c = TestClient(app)
    yield c
    c.close()


def test_root_url_reachable(client: TestClient) -> None:
    resp = client.get("/")
    assert resp.status_code == 200


def test_health_endpoint_reachable(client: TestClient) -> None:
    resp = client.get("/api/health")
    assert resp.status_code == 200
    body = resp.json()
    assert body.get("status") == "ok"


def test_chat_route_registered_not_missing(client: TestClient) -> None:
    # Auth-gated: 401 proves the route exists and is reachable; a 404 would
    # mean /api/chat/ask is not registered at all.
    resp = client.post("/api/chat/ask", json={})
    assert resp.status_code == 401


def test_startup_and_shutdown_install_and_close_shared_client() -> None:
    assert generator._ASYNC_CLIENT is None

    with TestClient(app) as c:
        assert generator._ASYNC_CLIENT is not None
        assert c.get("/api/health").status_code == 200

    assert generator._ASYNC_CLIENT is None

    with TestClient(app) as c2:
        assert generator._ASYNC_CLIENT is not None
        assert c2.get("/api/health").status_code == 200

    assert generator._ASYNC_CLIENT is None


# ---------------------------------------------------------------------------
# config.js cache-busting regression
#
# Browser symptom fixed here: "Cannot connect to backend (http://localhost:8001)".
# The corrected frontend/js/config.js (same-origin passthrough on the backend
# port + deterministic IPv4 loopback otherwise) only reaches the browser if the
# browser fetches a fresh copy.  The page HTML references the file through a
# cache-busting query (config.js?v=...) which is normally bumped whenever the
# file changes.  A stale cached config.js?v=2 (the pre-fix copy that still
# rebuilt "http://localhost:8001" from the ambiguous ``hostname`` and therefore
# depended on IPv6/IPv4 resolution the backend does not provide) kept being
# served from cache because the query never changed.  These tests pin the
# mechanism: the version token is a single value shared by config.js and every
# page that loads it, it is bumped past every value ever shipped (?v=0..3), and
# the served copy byte-equals the fixed file on disk so a bumped include always
# fetches the corrected bytes.
# ---------------------------------------------------------------------------
FRONTEND_PAGES = REPO_ROOT / "frontend" / "pages"
CONFIG_INCLUDE_RE = re.compile(r'config\.js\?v=(\d+)')
VERSION_CONST_RE = re.compile(r'CUS_CONFIG_VERSION\s*=\s*"(\d+)"')
# the previous shipped cache-busting tokens (old UNFIXED config.js bodies were
# served at ?v=0 .. ?v=3); a token strictly greater than every one of these is
# guaranteed to bypass any browser that already cached the broken copy.
PREV_SHIPPED_TOKENS = (1, 2, 3)


def _config_version() -> str:
    text = FRONTEND_CONFIG_JS.read_text(encoding="utf-8")
    m = VERSION_CONST_RE.search(text)
    assert m, "config.js must declare CUS_CONFIG_VERSION = \"<n>\""
    return m.group(1)


def test_config_cache_bust_token_is_bumped_past_every_published_copy() -> None:
    version = int(_config_version())
    assert version > max(PREV_SHIPPED_TOKENS)


def test_every_page_loads_config_js_with_the_current_single_token() -> None:
    version = _config_version()
    pages = sorted(FRONTEND_PAGES.glob("*.html"))
    assert pages, "no frontend pages found to check"
    for page in pages:
        text = page.read_text(encoding="utf-8")
        includes = CONFIG_INCLUDE_RE.findall(text)
        assert includes, f"{page.name} must load config.js with a ?v= cache-busting token"
        assert len(set(includes)) == 1, (
            f"{page.name} has inconsistent config.js version tokens: {sorted(set(includes))}"
        )
        assert includes[0] == version, (
            f"{page.name} loads config.js?v={includes[0]} but config.js declares "
            f'CUS_CONFIG_VERSION="{version}" — bump the page include, not the constant.'
        )


def test_no_page_loads_unversioned_config_js() -> None:
    for page in FRONTEND_PAGES.glob("*.html"):
        text = page.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), start=1):
            if "config.js" in line and "?v=" not in line:
                raise AssertionError(
                    f"{page.name}:{lineno} loads config.js WITHOUT a cache-busting "
                    "version query — the browser would keep the stale copy"
                )


def test_served_config_js_equals_the_fixed_disk_copy(client: TestClient) -> None:
    disk = FRONTEND_CONFIG_JS.read_bytes()
    served = client.get("/js/config.js")
    assert served.status_code == 200
    assert served.content == disk, (
        "the backend must serve the exact fixed config.js; if disk and served "
        "diverge, a browser bumping ?v= would still download the broken copy"
    )
    assert b'window.location.origin' in served.content, (
        "fixed config.js must keep the same-origin passthrough; a rebuilt "
        '"http://<hostname>:8001" reintroduces the IPv6/IPv4 localhost ambiguity'
    )


# ---------------------------------------------------------------------------
# chatbot.js SSE-stream state-scope regression
#
# Browser symptom fixed here: "Cannot connect to backend (http://localhost:8001)"
# immediately after the backend finished streaming a perfectly healthy 200
# response (planning ran, `options` + `done` events were emitted, the stream
# terminated cleanly).  The frontend's SSE loop in doChat() declared its
# per-stream state (lastActivity / stallWarned / renderScheduled) with ``var``
# INSIDE the ``.then(function (resp) {...})`` callback, while ``finish()`` — a
# sibling function in doChat() scope called from the ``done`` handler —
# executed ``renderScheduled = false;``.  Because ``var`` was function-scoped to
# the callback, ``finish()`` hit a ReferenceError, which escaped to the outer
# ``.catch`` and printed the misleading "Cannot connect to backend" banner even
# though the network and backend were healthy.  These tests pin the fix: the
# three flags must be declared at doChat() scope (single ``var`` at the function
# top), never re-declared with ``var`` inside the fetch callback, and the served
# copy must byte-equal the fixed file on disk so a cache-busted include always
# fetches the corrected bytes.
# ---------------------------------------------------------------------------
CHATBOT_JS = REPO_ROOT / "frontend" / "js" / "chatbot.js"


def test_chatbot_sse_state_declared_at_dochatch_scope() -> None:
    if not CHATBOT_JS.exists():
        pytest.skip("frontend/js/chatbot.js not present in this checkout")
    text = CHATBOT_JS.read_text(encoding="utf-8")
    fn = text.find("function doChat(text, isRetry)")
    assert fn != -1, "doChat() must exist"
    head = text[fn:fn + 2000]
    # Single var declaration at doChat() function scope (indent 4)
    assert re.search(
        r"var lastActivity = 0; var stallWarned = false; var renderScheduled = false;",
        head,
    ), (
        "lastActivity/stallWarned/renderScheduled must be declared once at doChat() "
        "scope so finish() (also in doChat() scope) can reset them; declaring them "
        "inside the .then() callback caused the ReferenceError behind the "
        "'Cannot connect to backend' banner"
    )
    # No shadowing re-declaration inside the fetch stream callback (indent 8)
    assert not re.search(r"\n {8}var lastActivity =", head), (
        "the .then() callback must not re-declare lastActivity with var — that "
        "shadows the doChat()-scope copy finish() depends on"
    )


def test_served_chatbot_js_equals_the_fixed_disk_copy(client: TestClient) -> None:
    if not CHATBOT_JS.exists():
        pytest.skip("frontend/js/chatbot.js not present in this checkout")
    disk = CHATBOT_JS.read_bytes()
    served = client.get("/js/chatbot.js")
    assert served.status_code == 200
    assert served.content == disk, (
        "the backend must serve the exact fixed chatbot.js; if disk and served "
        "diverge, a browser bumping ?v= would still download the broken copy"
    )