"""
backend/app/knowledge_sync/document_classifier.py

Phase 1 "Intelligent Website Document Ingestion" classification core.

Every crawled resource (HTML page or binary document) is classified into:

    doc_type: "knowledge" | "official" | "ambiguous"
    category: one of the official document categories for official docs, one
              of the existing knowledge categories for knowledge pages, or
              "ambiguous".

Official categories (Phase 1 destinations are NOT wired — label + review):
    date-sheet, model-paper, official-notification, other-official-document

Rules (deterministic, no LLM in the hot path):

  * HTML pages keep their existing knowledge classification (classify_page in
    web_classifier.py remains the legacy compatible API).
  * Binary documents are sniffed by magic bytes, then matched against the
    official phrase lists with weighted title/URL/text scoring. Date-sheet
    markers are additionally matched against the document text (real CUS date
    sheets carry "Date Sheet for ..." only in the body) and require the marker
    to be repeated (>= 2 distinct occurrences) to rule out stray mentions.
    Notification markers stay title/URL-only because "notice"/"order" appear
    inside the body of nearly every official document.
  * Model-paper has a hard exclusion list (previous year / PYQ / entrance
    paper / question-paper-pattern / syllabus / BoS / date sheet / notice /
    circular / marking scheme ...). When an exclusion is present the document
    is NEVER labeled model-paper.
  * Confidence: high / medium / low with a deterministic 0-100 score.
  * A document that cannot be confidently placed is "ambiguous".

Confidence band thresholds:
    high   score >= 70
    medium score >= 40
    low    otherwise

The classification result contract:

    {
      "doc_type": "knowledge" | "official" | "ambiguous",
      "category": <str>,
      "confidence": {"band": "high"|"medium"|"low", "score": 0..100},
      "signals": [<str>, ...],
    }
"""

from __future__ import annotations

import io
import re
import zipfile
from typing import Any

from app.knowledge_sync.web_classifier import classify_page

OFFICIAL_CATEGORIES = [
    "date-sheet",
    "model-paper",
    "official-notification",
    "other-official-document",
]

# ---- phrase lists (regex fragments, case-insensitive at match time) ----

DATE_SHEET_MARKERS = [
    r"date\s?sheet", r"datesheet", r"date[ -]?sheet",
    r"exam\s*schedule", r"examination\s*schedule",
    r"exam\s*time[ -]?table", r"examination\s*time[ -]?table",
    r"exam\s*timetable", r"examination\s*timetable",
]

MODEL_PAPER_MARKERS = [
    r"model\s*(question\s*)?papers?",
    r"model[ -]?papers?",
    r"modelpapers?",
]

# Applied to model-paper only, and they win over any model-paper marker.
MODEL_PAPER_EXCLUSIONS = [
    r"previous\s*years?", r"past\s*papers?", r"prev\.?\s*(year|paper)",
    r"\bpyq\b", r"entrance\s*(exam|test|paper)",
    r"question\s*paper\s*pattern", r"paper\s*pattern",
    r"syllabus", r"syllabi", r"synopsis",
    r"board\s*of\s*studies", r"\bbos\b",
    r"date\s?sheet", r"datesheet", r"notice", r"notification", r"circular",
    r"marking\s*(scheme|criteria)",
]

NOTIFICATION_MARKERS = [
    r"notice", r"notification", r"circular", r"public\s*notice",
    r"announcement", r"\border\b",
]

# Confident "other official document" markers (admission policies, syllabi,
# ordinances, statutes, regulations/guidelines, schedules published by the
# university, ...).
OTHER_OFFICIAL_MARKERS = [
    r"syllabus", r"syllabi", r"ordinance", r"statutes?", r"\bact\b",
    r"prospectus", r"admission\s*polic", r"academic\s*regulations",
    r"regulations?", r"guidelines?", r"academic\s*calendar",
    r"curriculum", r"course\s*scheme",
]

# Content-only structural evidence. December 2025 defect: real CUS model
# papers carry "Maximum Marks", "Time Allowed", and enumerated questions only
# in the BODY with opaque filenames; the same body shapes must be matched
# against the extracted text so content (not the URL path) decides.
MODEL_TEXT_STRUCTURE_MARKERS = [
    r"max(?:imum)?\s*marks", r"full\s*marks",
    r"time\s*allowed", r"allowed\s*time",
    r"\b[123]\s*(?:hrs?\.?|hours)\b",
    r"instructions?\s*to\s*candidates",
    r"attempt\s*(?:all|any)\s",
    r"paper\s*code",
]

_QUESTION_LINE_RE = re.compile(r"(?im)^\s{0,8}(?:q\.?\s*)?\d{1,3}\s*[.)]\s+\S")
_DAY_RE = re.compile(r"\b(?:mon|tue|wed|thu|fri|sat|sun)\w*\b", re.I)
_TIME_RE = re.compile(r"\d{1,2}\s*:\s*\d{2}\s*(?:am|pm)\b", re.I)

# Canonical repository doc_type (university_documents.doc_type).
CATEGORY_TO_DOC_TYPE = {
    "date-sheet": "date_sheet",
    "model-paper": "model_paper",
    "official-notification": "official_notification",
    "other-official-document": "other_official_document",
}


def canonical_doc_type_for(classification: dict[str, Any]) -> str:
    """Map a Phase 1 classification contract to the canonical doc_type."""
    doc_type = (classification.get("doc_type") or "").strip().lower()
    category = (classification.get("category") or "").strip().lower()
    if doc_type == "official":
        return CATEGORY_TO_DOC_TYPE.get(category, "official_notification")
    if doc_type == "knowledge":
        return "knowledge"
    return "needs_review"


def canonical_doc_type(category: str) -> str:
    """Map a category string (official categories) to canonical doc_type."""
    cat = (category or "").strip().lower()
    if cat in CATEGORY_TO_DOC_TYPE:
        return CATEGORY_TO_DOC_TYPE[cat]
    if cat == "ambiguous":
        return "needs_review"
    return "knowledge"


def _content_structure_votes(text: str) -> dict[str, Any]:
    """Derive deterministic content-structure evidence from extracted text."""
    if not text:
        return {
            "question_lines": 0,
            "structure_hits": [],
            "model_structure": False,
            "model_phrase": [],
            "date_table_rows": 0,
            "meaningful_length": 0,
        }
    text = text.strip()
    starts = {m.start() for m in _QUESTION_LINE_RE.finditer(text)}
    question_lines = len(starts)
    structure_hits = _hit_fragments(
        text, _patterns("model-structure", MODEL_TEXT_STRUCTURE_MARKERS)
    )
    day_starts = {m.start() for m in _DAY_RE.finditer(text)}
    date_rows = 0
    for line in text.splitlines():
        if not line.strip():
            continue
        if _DAY_RE.search(line) and _TIME_RE.search(line):
            date_rows += 1
    meaningful_length = len(text)
    return {
        "question_lines": question_lines,
        "structure_hits": structure_hits,
        "model_structure": bool(question_lines >= 4 and structure_hits),
        "model_phrase": _hit_fragments(
            text, _patterns("model-phrase", MODEL_PAPER_MARKERS)
        ),
        "date_table_rows": date_rows,
        "meaningful_length": meaningful_length,
    }

# Binary magic-byte sniffers: (extension, test(raw) -> bool).
_BINARY_SNIFFERS: list[tuple[str, Any]] = []


def _pdf_sniff(raw: bytes) -> bool:
    return raw[:5] == b"%PDF-"


def _zip_sniff(raw: bytes) -> bool:
    return raw[:2] == b"PK\x03\x04" or raw[:4] in (b"PK\x03\x04", b"PK\x05\x06")


def _docx_sniff(raw: bytes) -> bool:
    if not _zip_sniff(raw):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError):
        return False
    return any(n.startswith("word/") for n in names)


def _xlsx_sniff(raw: bytes) -> bool:
    if not _zip_sniff(raw):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError):
        return False
    return any(n.startswith("xl/") for n in names)


def _pptx_sniff(raw: bytes) -> bool:
    if not _zip_sniff(raw):
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(raw)) as zf:
            names = zf.namelist()
    except (zipfile.BadZipFile, OSError):
        return False
    return any(n.startswith("ppt/") for n in names)


def _build_binary_sniffers() -> None:
    global _BINARY_SNIFFERS
    if _BINARY_SNIFFERS:
        return
    # Order matters: DOCX/XLSX/PPTX must be tested before the generic zip check.
    _BINARY_SNIFFERS = [
        ("pdf", _pdf_sniff),
        ("docx", _docx_sniff),
        ("xlsx", _xlsx_sniff),
        ("pptx", _pptx_sniff),
    ]


def detect_binary_ext(raw: bytes | None) -> str | None:
    """Return the detected extension for a binary blob, or None if unknown.

    Never guesses from a filename — only magic bytes / container structure.
    """
    if not raw:
        return None
    _build_binary_sniffers()
    for ext, sniff in _BINARY_SNIFFERS:
        try:
            if sniff(raw):
                return ext
        except Exception:
            continue
    return None


_precompiled: dict[str, list[re.Pattern]] = {}


def _patterns(key: str, fragments: list[str]) -> list[re.Pattern]:
    compiled = _precompiled.get(key)
    if compiled is None:
        compiled = [re.compile(f, re.IGNORECASE) for f in fragments]
        _precompiled[key] = compiled
    return compiled


def _hit_fragments(text: str, patterns: list[re.Pattern]) -> list[str]:
    """Return the raw matched fragment texts for the given patterns."""
    if not text:
        return []
    hits: list[str] = []
    for pat in patterns:
        m = pat.search(text)
        if m and m.group(0):
            hits.append(m.group(0).strip())
    return hits


def _distinct_occurrences(text: str, patterns: list[re.Pattern]) -> int:
    """Count distinct physical matches of the given patterns across the text.

    Overlapping patterns (e.g. ``date sheet`` matched by ``date\\s?sheet`` and
    ``date[ -]?sheet``) each report a match, so matches are deduplicated by
    match start offset before counting.
    """
    if not text:
        return 0
    starts: set[int] = set()
    for pat in patterns:
        for m in pat.finditer(text):
            if m.group(0):
                starts.add(m.start())
    return len(starts)


def _weighted_score(title: str, slug: str, url: str, text: str, patterns: list[re.Pattern]) -> int:
    score = 0
    score += len(_hit_fragments(title, patterns)) * 4
    score += len(_hit_fragments(slug, patterns)) * 3
    score += len(_hit_fragments(url, patterns)) * 2
    score += len(_hit_fragments(text, patterns)) * 1
    return score


def _band(score: int) -> tuple[str, int]:
    bounded = min(100, score * 10)
    if bounded >= 70:
        return "high", bounded
    if bounded >= 40:
        return "medium", bounded
    return "low", bounded


def _where(fragments: list[str], label: str) -> list[str]:
    return [f"{label}: {f}" for f in fragments]


def classify_document(
    *,
    title: str = "",
    url: str = "",
    text: str = "",
    content_type: str = "",
    raw: bytes | None = None,
) -> dict[str, Any]:
    """Classify a crawled resource (HTML page or binary document).

    Returns the Phase 1 classification contract described in the module doc.
    """
    title_l = title or ""
    url_l = url or ""
    text_l = (text or "")[:6000]
    slug = (url_l.rstrip("/").rsplit("/", 1)[-1] or "")

    signals: list[str] = []
    ext = detect_binary_ext(raw)
    binary = bool(ext) or (content_type or "").lower() not in ("", "html", "text/html")

    # ---- HTML pages keep the existing knowledge classification ----
    if not binary and (content_type or "").lower() in ("", "html", "text/html"):
        category = classify_page(title=title_l, url=url_l, text=text_l)
        if category == "unknown":
            return {
                "doc_type": "ambiguous",
                "category": "ambiguous",
                "confidence": {"band": "low", "score": 0},
                "signals": ["no classification signal"],
            }
        confidence, score = _band(8 if slug or title_l else 3)
        return {
            "doc_type": "knowledge",
            "category": category,
            "confidence": {"band": confidence, "score": score},
            "signals": [f"knowledge category '{category}' matched"],
        }

    # ---- Binary / document classification ----
    # Date sheets are detected from the DOCUMENT TEXT as well as title/URL:
    # real CUS date sheets from the Office of the Controller of Examinations
    # ("Date Sheet for ... Semester ...") carry the phrase only in the body,
    # never in the filename. A text-only detection additionally requires the
    # marker to be repeated (>= 2 distinct occurrences; real sheets repeat the
    # phrase across page headers, e.g. ug4thsemesternepbatch2024backlog.pdf has
    # a second occurrence on page 2) so a single stray mention cannot promote
    # an unrelated document (e.g. a CV that once says "date sheet"). The
    # occurrence scan is bounded to the first 20000 chars for cost safety.
    #
    # Notifications are deliberately title/URL-ONLY: words like "notice",
    # "order" appear inside the body of nearly every statutory document (e.g.
    # statutes.pdf has hundreds), so text-based detection there would corrupt
    # other-official-document classification. The asymmetry is intentional.
    date_pats = _patterns("date", DATE_SHEET_MARKERS)
    title_url_date_hits = _hit_fragments(f"{title_l}\n{url_l}", date_pats)
    date_text_repeated = _distinct_occurrences(text[:20000], date_pats) >= 2
    if title_url_date_hits or date_text_repeated:
        date_hits = _hit_fragments(f"{title_l}\n{url_l}\n{text_l}", date_pats)
    else:
        date_hits = []
    notif_hits = _hit_fragments(f"{title_l}\n{url_l}", _patterns("notif", NOTIFICATION_MARKERS))
    other_hits = _hit_fragments(
        f"{title_l}\n{url_l}", _patterns("other", OTHER_OFFICIAL_MARKERS)
    )

    # Model paper: markers subject to hard exclusions.
    model_text = _hit_fragments(
        f"{title_l}\n{url_l}\n{text_l}", _patterns("model", MODEL_PAPER_MARKERS)
    )
    excluded = _hit_fragments(
        f"{title_l}\n{url_l}\n{text_l}", _patterns("excl", MODEL_PAPER_EXCLUSIONS)
    )
    model_allowed = bool(model_text) and not excluded
    if model_text and not model_allowed:
        signals.append(f"model-paper excluded: {', '.join(excluded)}")

    # Content-structure evidence (body-first classification): enumerated
    # question lines + "Maximum Marks"/"Time Allowed"/"Instructions to
    # Candidates" strongly indicate a question paper; repeated weekday+time
    # rows strongly indicate a date sheet. Content evidence overrides noisy
    # title/URL hints (e.g. a model paper hiding under a /notices/ path). The
    # exclusion list is applied to the BODY for these content-driven paths —
    # an URL bearing "notice" or "notification" is hosting noise, not content.
    votes = _content_structure_votes(text_l)
    date_strong = bool(title_url_date_hits or date_text_repeated)
    date_table_structural = votes["date_table_rows"] >= 2
    content_model = bool(votes["model_phrase"]) or votes["model_structure"]
    text_excluded = _hit_fragments(text_l, _patterns("excl-body", MODEL_PAPER_EXCLUSIONS))

    if content_model and not text_excluded and not (
        date_strong or date_table_structural
    ):
        evidence = len(votes["model_phrase"]) * 2
        if votes["model_structure"]:
            evidence += 4 + min(votes["question_lines"], 4)
            signals.append(
                f"model-paper content structure: {votes['question_lines']} enumerated questions"
            )
        evidence += len(_hit_fragments(title_l, _patterns("model", MODEL_PAPER_MARKERS))) * 4
        evidence += len(_hit_fragments(url_l, _patterns("model", MODEL_PAPER_MARKERS))) * 2
        band, score = _band(evidence)
        signals.append("model paper: label + hold for review")
        return {
            "doc_type": "official",
            "category": "model-paper",
            "confidence": {"band": band, "score": score},
            "signals": signals,
        }

    # A bare question paper (enumerated questions + "Maximum Marks"/"Time
    # Allowed"/"Instructions to Candidates") with no exclusion phrase is a
    # model/question paper even when the filename/URL are silent about it.
    if votes["model_structure"] and not text_excluded and not (
        date_strong or date_table_structural
    ):
        evidence = 4 + min(votes["question_lines"], 4) + len(votes["structure_hits"])
        signals.append(
            f"model-paper content structure: {votes['question_lines']} enumerated questions"
        )
        signals.append("model paper: label + hold for review")
        band, score = _band(evidence)
        return {
            "doc_type": "official",
            "category": "model-paper",
            "confidence": {"band": band, "score": score},
            "signals": signals,
        }

    if date_table_structural and not date_strong and not content_model:
        winner_score = max(
            4, _weighted_score(title_l, slug, url_l, text_l, date_pats)
        )
        band, score = _band(winner_score)
        signals.append(
            f"date-sheet content structure: {votes['date_table_rows']} date rows"
        )
        signals.extend(_where(date_hits, "date-sheet"))
        return {
            "doc_type": "official",
            "category": "date-sheet",
            "confidence": {"band": band, "score": score},
            "signals": signals,
        }

    scores: dict[str, int] = {}
    if date_hits:
        scores["date-sheet"] = _weighted_score(
            title_l, slug, url_l, text_l, _patterns("date", DATE_SHEET_MARKERS)
        )
    if model_allowed:
        scores["model-paper"] = max(
            1,
            _weighted_score(title_l, slug, url_l, text_l, _patterns("model", MODEL_PAPER_MARKERS)),
        )
    if notif_hits:
        scores["official-notification"] = _weighted_score(
            title_l, slug, url_l, text_l, _patterns("notif", NOTIFICATION_MARKERS)
        )

    # Other official documents (syllabi, regulations, ordinances, ...) compete
    # with the specific categories instead of only being a fallback, so a
    # syllabus on a /notices/ path is still recognised as an official document.
    if other_hits:
        scores["other-official-document"] = _weighted_score(
            title_l, slug, url_l, text_l, _patterns("other", OTHER_OFFICIAL_MARKERS)
        )

    if not scores:
        return {
            "doc_type": "ambiguous",
            "category": "ambiguous",
            "confidence": {"band": "low", "score": 0},
            "signals": ["no confident official category"],
        }

    best = max(scores.items(), key=lambda kv: kv[1])
    winner, winner_score = best

    # Conflicting signals: two official categories within a close margin with
    # no dominant title/URL hit -> ambiguous rather than a wrong guess.
    ranked = sorted(scores.values(), reverse=True)
    conflicting = len(ranked) >= 2 and ranked[1] > 0 and ranked[1] >= ranked[0] * 0.5
    if conflicting and winner_score < 6:
        return {
            "doc_type": "ambiguous",
            "category": "ambiguous",
            "confidence": {"band": "low", "score": 0},
            "signals": [f"conflicting official signals: {', '.join(sorted(scores))}"],
        }

    band, score = _band(winner_score)
    if winner == "date-sheet":
        signals.extend(_where(date_hits, "date-sheet"))
    elif winner == "model-paper":
        signals.extend(_where(model_text, "model-paper"))
        signals.append("model paper: label + hold for review")
    elif winner == "official-notification":
        signals.extend(_where(notif_hits, "official-notification"))
    else:
        signals.extend(_where(other_hits, "other-official-document"))

    return {
        "doc_type": "official",
        "category": winner,
        "confidence": {"band": band, "score": score},
        "signals": signals,
    }


def classification_state_for(
    result: dict[str, Any],
    *,
    is_document: bool,
) -> str:
    """Map a classification result to the review lifecycle state.

    * HTML knowledge pages are considered verified (nothing to review).
    * EVERY binary document and anything ambiguous/official goes to
      pending_review — including model papers, which are always held.
    """
    if result.get("doc_type") == "knowledge" and not is_document:
        return "verified"
    return "pending_review"


def normalize_title_hash(normalized: str | None) -> str | None:
    """sha256 hex of a normalized title (title-similarity dedup)."""
    if not normalized:
        return None
    import hashlib

    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()