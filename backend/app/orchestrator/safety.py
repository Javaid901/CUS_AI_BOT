"""
backend/app/orchestrator/safety.py

P2-A — blocked-manipulation pre-filter.

Deterministic, retrieval-free guard that stops requests whose ONLY purpose is
to bypass, forge, falsify or tamper with university records / systems (marks,
grades, results, attendance, portals, databases, ...). It runs at the very top
of the planner — BEFORE any university reasoning, student service, retrieval or
LLM path — and never touches the database or the embedding layer.

Deliberately narrow so legitimate help is never blocked:

  * HARD verbs (hack / bypass / forge / fabricate / tamper / manipulate /
    inject / crack / spoof / fake / ...) combined with a protected target
    (marks / grades / result / attendance / portal / system / ...) are always
    blocked.
  * SOFTER change verbs (change / edit / modify / alter / update / remove /
    delete) combined with a protected target are blocked ONLY when the message
    is not already framed as a legitimate remedy (a correction, a wrong/inaccurate
    record, a grievance, appeal, recheck, re-evaluation...). "My marks are
    incorrect, how do I correct them?" is a legitimate remedy and is NEVER
    blocked; "how do I hack the portal and change my grades?" is blocked.
"""

from __future__ import annotations

import re

# Hard manipulation verbs — an explicit intent to break into / alter records.
_HARD_VERB_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bhack(ed|ing|s)?\b", re.IGNORECASE),
    re.compile(r"\bbypass(es|ed|ing)?\b", re.IGNORECASE),
    re.compile(r"\bforge(s|d)?\b|\bforging\b", re.IGNORECASE),
    re.compile(r"\bfabricate(s|d)?\b|\bfabrication\b", re.IGNORECASE),
    re.compile(r"\btamper(s|ed|ing)?\b", re.IGNORECASE),
    re.compile(r"\bmanipulat(e|es|ed|ing|ion)?\b", re.IGNORECASE),
    re.compile(r"\binject(s|ed|ing)?\b", re.IGNORECASE),
    re.compile(r"\bcrack(s|ed|ing)?\b", re.IGNORECASE),
    re.compile(r"\bexploit(s|ed|ing)?\b", re.IGNORECASE),
    re.compile(r"\bspoof(s|ed|ing)?\b", re.IGNORECASE),
    re.compile(r"\bfake(s|d)?\b|\bfaking\b", re.IGNORECASE),
    re.compile(r"\bfalsif(y|ies|ied|ying|ication)\b", re.IGNORECASE),
    re.compile(r"\bcheat(s|ing)?\b|\brig(s|ging|ged)?\b", re.IGNORECASE),
)

# Softer verbs that only block when aimed at a protected target without a
# legitimate-remedy frame.
_SOFT_VERB_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\bchange(s|d|ing)?\b", re.IGNORECASE),
    re.compile(r"\bedit(s|ed|ing)?\b", re.IGNORECASE),
    re.compile(r"\bmodif(y|ies|ied|ying|ication)\b", re.IGNORECASE),
    re.compile(r"\balter(s|ed|ing)?\b", re.IGNORECASE),
    re.compile(r"\bupdat(e|es|ed|ing)\b", re.IGNORECASE),
    re.compile(r"\bremov(e|es|ed|ing)\b", re.IGNORECASE),
    re.compile(r"\bdelet(e|es|ed|ing)\b", re.IGNORECASE),
)

# Real academic records / systems a student cannot contact the chatbot to
# rewrite. Legitimate exam processes (clearing a backlog by re-appearing) are
# deliberately NOT targets.
_TARGET_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(mark|marks)s?\b", re.IGNORECASE),
    re.compile(r"\bgrades?\b", re.IGNORECASE),
    re.compile(r"\b(results?|result card)\b", re.IGNORECASE),
    re.compile(r"\bcgpa\b|\bsgpa\b|\bgpa\b", re.IGNORECASE),
    re.compile(r"\bmarksheet\b|\bmark sheet\b|\bmarks card\b|\bmark card\b", re.IGNORECASE),
    re.compile(r"\btranscript(s)?\b", re.IGNORECASE),
    re.compile(r"\battendance\b", re.IGNORECASE),
    re.compile(r"\binternal marks?\b", re.IGNORECASE),
    re.compile(r"\banswer (sheet|sheets|scripts?)\b", re.IGNORECASE),
    re.compile(r"\badmit cards?\b|\bhall tickets?\b", re.IGNORECASE),
    re.compile(r"\br?e?cord(s)?\b", re.IGNORECASE),
    re.compile(r"\bportals?\b", re.IGNORECASE),
    re.compile(r"\bsystems?\b", re.IGNORECASE),
    re.compile(r"\b(website|web ?site)(s)?\b", re.IGNORECASE),
    re.compile(r"\bdatabases?\b", re.IGNORECASE),
    re.compile(r"\bservers?\b", re.IGNORECASE),
)

# Legitimate-remedy framing that rescues a SOFT-verb + target message (a
# correction / appeal / dispute of a WRONG record). Hard verbs are never
# rescued by this list.
_REMEDY_RE = re.compile(
    r"\b(correct|corrected|correcting|correction|wrongly|wrong|incorrect|"
    r"inaccurate|mistake|error|grievance|appeal|recheck|re-evaluation|"
    r"revaluation|recount|re-issue|reissue|re-visit|revisit|issued in |not mine|"
    r"dispute|revise)\b",
    re.IGNORECASE,
)


def _matches_any(patterns: tuple[re.Pattern[str], ...], text: str) -> bool:
    return any(p.search(text) for p in patterns)


def detect_blocked_manipulation(text: str) -> str | None:
    """Return a short reason when the message is a blocked manipulation
    attempt, else ``None``.

    ``text`` may be the raw or lowercased message (every rule is case-
    insensitive).
    """
    low = str(text or "").strip().lower()
    if not low:
        return None

    has_hard = _matches_any(_HARD_VERB_RES, low)
    has_soft = _matches_any(_SOFT_VERB_RES, low)
    has_target = _matches_any(_TARGET_RES, low)
    if not (has_hard or has_soft) or not has_target:
        return None

    if has_hard:
        return "hard manipulation verb + protected target"
    if not _REMEDY_RE.search(low):
        return "unauthorized change of a protected record"
    return None