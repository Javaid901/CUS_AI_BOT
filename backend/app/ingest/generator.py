"""
backend/app/ingest/generator.py

LLM generation via Ollama, with streaming token output.

The generator is strictly grounded: it receives retrieved context and the system
prompt that forbids hallucination. If no context is retrieved, the caller should
short-circuit with the fallback message (see chat service) so the LLM is never
asked to invent an answer.
"""

from __future__ import annotations

import asyncio
import json
import threading

import httpx
from app.config import settings
from app.ingest.prompts import CONTEXT_TEMPLATE, SYSTEM_PROMPT
from app.utils.logging import log

_GEN_TIMEOUT = 180.0
_HTTP_CLIENT: httpx.Client | None = None
_HTTP_LOCK = threading.Lock()

# Shared async streaming client for the SSE path. Installed once at FastAPI
# startup so every generation reuses a warm connection pool to Ollama instead
# of paying connection setup per call (the previous per-call AsyncClient). The
# client is bound to the event loop that first uses it; calls from a different
# loop (standalone scripts, isolated tests) fall back to a private per-call
# client so the shared one can never be used across loops.
_ASYNC_CLIENT: httpx.AsyncClient | None = None
_ASYNC_CLIENT_LOOP: asyncio.AbstractEventLoop | None = None
_ASYNC_LOCK = threading.Lock()


class GenerationError(Exception):
    pass


def _get_client() -> httpx.Client:
    global _HTTP_CLIENT
    if _HTTP_CLIENT is None:
        with _HTTP_LOCK:
            if _HTTP_CLIENT is None:
                _HTTP_CLIENT = httpx.Client(timeout=_GEN_TIMEOUT)
    return _HTTP_CLIENT


def install_async_client() -> None:
    """Create (once) the shared async streaming client.

    FastAPI startup calls this so every subsequent SSE generation reuses a warm
    connection pool to Ollama. Idempotent: later calls are no-ops. The client
    is bound to the event loop that first uses it; see ``_shared_async_client``.
    """
    global _ASYNC_CLIENT, _ASYNC_CLIENT_LOOP
    if _ASYNC_CLIENT is None:
        with _ASYNC_LOCK:
            if _ASYNC_CLIENT is None:
                _ASYNC_CLIENT = httpx.AsyncClient(timeout=_GEN_TIMEOUT)
    # Don't reserve the loop here — the first stream decides (lazy binding).


async def close_async_client() -> None:
    """Close the shared async client created by install_async_client.

    Only closes when called from the owning loop (or when the owner is
    unknown) so a client installed for FastAPI's loop is never closed by a
    different or already-dead loop. Safe to call repeatedly.
    """
    global _ASYNC_CLIENT, _ASYNC_CLIENT_LOOP
    client = _ASYNC_CLIENT
    if client is None:
        return
    owner = _ASYNC_CLIENT_LOOP
    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        current = None
    if owner is not None and current is not None and current is not owner:
        return  # wrong loop — leave installed for its owner
    _ASYNC_CLIENT = None
    _ASYNC_CLIENT_LOOP = None
    try:
        await client.aclose()
    except Exception:  # pragma: no cover - netiface teardown on shutdown
        pass


def _shared_async_client() -> httpx.AsyncClient | None:
    """Return the shared client when usable from the current loop, else None.

    Returns None when the client is not installed or the call originates on a
    different loop (scripts, isolated test portals) so callers fall back to a
    per-call client. Lazy-binds the loop on first use.
    """
    global _ASYNC_CLIENT_LOOP
    client = _ASYNC_CLIENT
    if client is None:
        return None
    try:
        current = asyncio.get_running_loop()
    except RuntimeError:
        return None
    owner = _ASYNC_CLIENT_LOOP
    if owner is None:
        with _ASYNC_LOCK:
            if _ASYNC_CLIENT_LOOP is None:
                _ASYNC_CLIENT_LOOP = current
                return client
            owner = _ASYNC_CLIENT_LOOP
    return client if owner is current else None


def _build_payload(question: str, context: str, system: str | None = None) -> dict:
    prompt = CONTEXT_TEMPLATE.format(context=context, question=question)
    return {
        "model": settings.LLM_MODEL,
        "prompt": prompt,
        "system": system or SYSTEM_PROMPT,
        "stream": True,
        "keep_alive": f"{settings.OLLAMA_KEEP_ALIVE}s",
        "options": {
            "temperature": settings.LLM_TEMPERATURE,
            "top_p": settings.LLM_TOP_P,
            "num_predict": settings.LLM_MAX_TOKENS,
        },
    }


def stream_answer(question: str, context: str, system: str | None = None):
    """
    Yield tokens (strings) from the Ollama streaming endpoint.
    Raises GenerationError on connection/HTTP failure.

    Sync variant — for CLI/standalone use. The async SSE path must use
    stream_answer_async so per-token network reads never block the event loop.

    ``system`` optionally overrides the system prompt (multi-source synthesis).
    """
    payload = _build_payload(question, context, system=system)
    client = _get_client()
    try:
        with client.stream(
            "POST", f"{settings.OLLAMA_BASE_URL}/api/generate", json=payload
        ) as resp:
            if resp.status_code != 200:
                raise GenerationError(f"Ollama returned HTTP {resp.status_code}")
            for line in resp.iter_lines():
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("done"):
                    return
                token = obj.get("response")
                if token:
                    yield token
    except httpx.HTTPError as exc:
        raise GenerationError(f"Ollama request failed: {exc}") from exc


async def stream_answer_async(question: str, context: str, system: str | None = None):
    """
    Async twin of stream_answer — yields tokens without blocking the loop.

    Reuses the shared client installed by install_async_client when the call
    runs on the client's event loop; otherwise (standalone scripts, isolated
    test portals) falls back to a private per-call AsyncClient. Both paths are
    safe to use concurrently and clean up after themselves.
    Raises GenerationError on connection/HTTP failure.

    ``system`` optionally overrides the system prompt (multi-source synthesis).
    """
    payload = _build_payload(question, context, system=system)
    shared = _shared_async_client()
    try:
        if shared is not None:
            async with shared.stream(
                "POST", f"{settings.OLLAMA_BASE_URL}/api/generate", json=payload
            ) as resp:
                async for line in _iter_olama_lines(resp):
                    yield line
        else:
            async with httpx.AsyncClient(timeout=_GEN_TIMEOUT) as client:
                async with client.stream(
                    "POST", f"{settings.OLLAMA_BASE_URL}/api/generate", json=payload
                ) as resp:
                    async for line in _iter_olama_lines(resp):
                        yield line
    except httpx.HTTPError as exc:
        raise GenerationError(f"Ollama request failed: {exc}") from exc


async def _iter_olama_lines(resp: httpx.Response):
    """Stream parsed tokens from an open /api/generate response."""
    if resp.status_code != 200:
        raise GenerationError(f"Ollama returned HTTP {resp.status_code}")
    async for raw in resp.aiter_lines():
        if not raw:
            continue
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if obj.get("done"):
            return
        token = obj.get("response")
        if token:
            yield token


def is_ollama_available() -> bool:
    try:
        client = _get_client()
        r = client.get(f"{settings.OLLAMA_BASE_URL}/api/tags")
        return r.status_code == 200
    except Exception as exc:
        log.warning("Ollama availability check failed: %s", exc)
        return False


def list_models() -> list[str]:
    try:
        client = _get_client()
        r = client.get(f"{settings.OLLAMA_BASE_URL}/api/tags")
        if r.status_code == 200:
            return [m["name"] for m in r.json().get("models", [])]
    except Exception:
        pass
    return []
