"""
backend/app/student/logout.py

Deterministic Student Services chat-logout command detection.

Design contract:
  - Detection is pure text matching against an exact, closed command set. It
    runs BEFORE the Admission Controller and the LLM, so logout is immediate,
    cheap, and can never be mis-routed through intent classification.
  - Matching is FULL string equality on a normalised message (lowercase,
    punctuation collapsed, whitespace flattened). There is no substring
    matching, so open-ended phrases like "what does logout mean?", "how do I
    log out?" or "tell me about signing out" never trigger a logout.
  - The command set mirrors the approved examples: logout / log out / sign
    out / sign me out / log me out, each optionally wrapped in "please" or a
    trailing "from student services" / "of student services".
"""

from __future__ import annotations

import re

# Exact commands (post-normalisation). Insertion of extra words yields no match.
_COMMANDS: frozenset[str] = frozenset({
    "logout",
    "log out",
    "sign out",
    "sign me out",
    "log me out",
})

# Optional trailing qualifiers removed before the membership test.
_TRAILING_QUALIFIERS: tuple[str, ...] = (
    "from student services",
    "of student services",
    "from student service",
)

# Optional leading politeness words removed before the membership test.
_LEADING_QUALIFIERS: tuple[str, ...] = (
    "please",
    "kindly",
)

# Collapse runs of punctuation into a single space, then flatten whitespace.
_WS_RE = re.compile(r"\s+")
_PUNCT_RE = re.compile(r"[^\w\s]+")


def _normalize(message: str) -> str:
    text = (message or "").strip().lower()
    text = _PUNCT_RE.sub(" ", text)          # "log out!" -> "log out "
    text = _WS_RE.sub(" ", text).strip()     # "log   out" -> "log out"
    return text


def detect_logout_command(message: str | None) -> bool:
    """True iff `message` is an unambiguous Student Services logout command."""
    text = _normalize(message or "")
    if not text:
        return False
    for trailing in _TRAILING_QUALIFIERS:
        if text.endswith(trailing):
            text = text[: -len(trailing)].strip()
            break
    for leading in _LEADING_QUALIFIERS:
        if text.startswith(leading):
            text = text[len(leading):].strip()
            break
    return text in _COMMANDS