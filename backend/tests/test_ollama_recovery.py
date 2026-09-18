"""
backend/tests/test_ollama_recovery.py

Regression tests for the Ollama availability fix:

  * `_warmup_when_ready` retries with bounded backoff until Ollama is
    reachable, then runs the warmup exactly once.
  * A warmup that fails transiently is retried, never abandoned after a
    single attempt.
  * An Ollama that never becomes ready yields (non-fatal) — the app never
    blocks forever and never fakes success.
  * `_ollama_probe` returns False when Ollama is unreachable.
  * The recovery watchdog is skipped under the test runner and starts a
    single daemon thread otherwise.

All tests are deterministic and require no live Ollama instance.
"""

import threading

from app import main


def test_warmup_when_ready_retries_until_reachable():
    calls = {"probe": 0, "warm": 0}

    def probe():
        calls["probe"] += 1
        return calls["probe"] >= 3

    def warm():
        calls["warm"] += 1

    ok = main._warmup_when_ready(
        warm,
        "Test",
        probe_fn=probe,
        retry_budget=1.0,
        retry_base=0.001,
        retry_max=0.001,
    )
    assert ok is True
    assert calls["probe"] == 3
    assert calls["warm"] == 1


def test_warmup_when_ready_gives_up_gracefully_when_never_reachable():
    def probe():
        return False

    def warm():
        raise AssertionError("warmup must not run while Ollama is unreachable")

    ok = main._warmup_when_ready(
        warm,
        "Never",
        probe_fn=probe,
        retry_budget=0.0,
        retry_base=0.001,
        retry_max=0.001,
    )
    assert ok is False


def test_warmup_when_ready_retries_after_warmup_failure():
    calls = {"n": 0}

    def warm():
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient failure")

    ok = main._warmup_when_ready(
        warm,
        "Flake",
        probe_fn=lambda: True,
        retry_budget=1.0,
        retry_base=0.001,
        retry_max=0.001,
    )
    assert ok is True
    assert calls["n"] == 2


def test_ollama_probe_returns_false_when_unreachable(monkeypatch):
    monkeypatch.setattr(main.settings, "OLLAMA_BASE_URL", "http://127.0.0.1:1")
    assert main._ollama_probe() is False


def test_watchdog_skipped_under_test_runner(monkeypatch):
    monkeypatch.setattr(main, "_is_testing", lambda: True)
    before = {t.name for t in threading.enumerate()}
    assert main._start_ollama_watchdog() is None
    after = {t.name for t in threading.enumerate()}
    assert "ollama-watchdog" not in after - before


def test_watchdog_starts_daemon_thread_when_not_testing(monkeypatch):
    monkeypatch.setattr(main, "_is_testing", lambda: False)
    monkeypatch.setattr(main, "_ollama_probe", lambda: False)
    monkeypatch.setattr(main, "_OLLAMA_WATCH_INTERVAL", 0.05)

    main._start_ollama_watchdog()

    watchdogs = [t for t in threading.enumerate() if t.name == "ollama-watchdog"]
    assert len(watchdogs) == 1
    assert watchdogs[0].daemon is True

    # Neutralize the long-lived daemon before monkeypatch teardown: the loop
    # reads the interval at module scope each iteration, so the next wake-up
    # will sleep 3600s instead of probing every 50ms for the rest of the suite.
    monkeypatch.setattr(main, "_OLLAMA_WATCH_INTERVAL", 3600.0)