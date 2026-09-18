"""
backend/tests/test_server_clean_exit.py

Regression battery for "the CUS AI server process must exit cleanly".

Root cause (proven against uvicorn 0.49.0): the app keeps PERMANENT SSE
streams open (/api/admin/jobs/events global generator, per-job generator,
chat heartbeat, ingest SSE). uvicorn's default ``timeout_graceful_shutdown``
is ``None``, so a graceful stop (Ctrl+C / SIGHUP / should_exit) waits
FOREVER in ``Server.shutdown()`` -> ``_wait_tasks_to_complete()`` while any
open SSE stream keeps ``server_state.connections``/``tasks`` non-empty. The
python process never exits, TCP port 8001 stays bound, and the next start
collides (Errno 10048) -> manual PID kill.

Fix: backend/start_server.ps1 now always passes
``--timeout-graceful-shutdown <N>`` to uvicorn, so a graceful stop is bounded:
hung SSE tasks are cancelled, lifespan shutdown runs, and the process exits
cleanly, freeing the port.

These tests exercise the REAL uvicorn server (real sockets, real event loop):
  1. the launcher always bounds graceful shutdown (`--timeout-graceful-shutdown`);
  2. with a bounded timeout, server.should_exit + an open admin SSE stream
     -> serve() returns quickly (clean exit);
  3. with the uvicorn DEFAULT (no timeout), the same scenario stays blocked
     (proves why the launcher flag is required).
"""

from __future__ import annotations

import json
import re
import socket
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

_BACKEND_DIR = Path(__file__).resolve().parents[1]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _ServerRunner(threading.Thread):
    """Boot the real app under a real uvicorn Server on an ephemeral port."""

    def __init__(self, port: int, graceful_timeout: int | None) -> None:
        super().__init__(daemon=True)
        self.port = port
        self.graceful_timeout = graceful_timeout
        self.server = None
        self.error: BaseException | None = None

    def run(self) -> None:
        import asyncio

        from uvicorn import Config, Server

        config = Config(
            "app.main:app",
            host="127.0.0.1",
            port=self.port,
            log_level="warning",
            access_log=False,
            timeout_graceful_shutdown=self.graceful_timeout,
        )
        self.server = Server(config)
        try:
            asyncio.run(self.server.serve())
        except BaseException as exc:  # noqa: BLE001  (surface any boot/shutdown error)
            self.error = exc

    def wait_until_started(self, timeout: float = 45.0) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.error is not None:
                raise RuntimeError(f"uvicorn boot failed: {self.error}")
            if self.server is not None and self.server.started:
                return
            time.sleep(0.05)
        raise TimeoutError(f"uvicorn did not start on port {self.port} within {timeout}s")


def _admin_token(port: int) -> str:
    from app.config import settings

    body = urllib.parse.urlencode(
        {"username": settings.SEED_ADMIN_USERNAME, "password": settings.SEED_ADMIN_PASSWORD}
    ).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{port}/api/auth/login", data=body)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.load(resp)["access_token"]


def _open_admin_sse(port: int, token: str):
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/admin/jobs/events",
        headers={"Authorization": f"Bearer {token}", "Accept": "text/event-stream"},
    )
    return urllib.request.urlopen(req, timeout=20)


def test_launcher_bounds_graceful_shutdown():
    script = (_BACKEND_DIR / "start_server.ps1").read_text(encoding="utf-8")
    assert "--timeout-graceful-shutdown" in script
    assert re.search(r"\[int\]\$GracefulSeconds\s*=\s*[1-9]\d*", script)
    assert re.search(r"--timeout-graceful-shutdown['\"].*\$GracefulSeconds", script)


def test_graceful_shutdown_completes_with_bounded_timeout():
    port = _free_port()
    runner = _ServerRunner(port, graceful_timeout=8)
    runner.start()
    try:
        runner.wait_until_started()
        token = _admin_token(port)
        sse = _open_admin_sse(port, token)
        try:
            assert sse.status == 200
            time.sleep(1.0)  # let uvicorn register the open connection/task
            runner.server.should_exit = True  # identical to a Ctrl+C handler
            start = time.monotonic()
            runner.join(timeout=30.0)
            elapsed = time.monotonic() - start
            assert not runner.is_alive(), "server did not exit within 30s"
            assert runner.error is None, f"server exited with error: {runner.error}"
            assert elapsed <= 15.0, f"bounded graceful shutdown took {elapsed:.1f}s"
        finally:
            sse.close()
    finally:
        runner.join(timeout=10.0)


def test_graceful_shutdown_hangs_without_timeout():
    port = _free_port()
    runner = _ServerRunner(port, graceful_timeout=None)  # uvicorn default
    runner.start()
    try:
        runner.wait_until_started()
        token = _admin_token(port)
        sse = _open_admin_sse(port, token)
        try:
            assert sse.status == 200
            time.sleep(1.0)  # let uvicorn register the open connection/task
            runner.server.should_exit = True
            runner.join(timeout=6.0)
            # Default config MUST stay blocked on the open SSE stream.
            assert runner.is_alive(), "default graceful shutdown unexpectedly exited"
        finally:
            sse.close()
            runner.server.should_exit = True
            runner.join(timeout=30.0)
    finally:
        runner.join(timeout=10.0)
        assert runner.error is None, f"server exited with error: {runner.error}"


if __name__ == "__main__":
    import sys

    sys.argv = [sys.argv[0], "-v", __file__]
    import pytest

    sys.exit(pytest.main())