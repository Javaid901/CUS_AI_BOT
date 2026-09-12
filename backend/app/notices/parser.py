"""backend/app/notices/parser.py — determine-only date-sheet parser.

Deterministic, rule-based extraction of schedule rows from uploaded
university notice files (PDF text layers and DOCX paragraphs/tables).

Hard rule (zero fabrication): the parser NEVER invents data. Every field a
row carries is copied verbatim out of the source text and normalized only for
format (dates -> ISO, times -> 24h). Anything that cannot be confidently
located is left NULL and surfaced in `validation_flags` / `warnings` for
admin review — it is never guessed.

Recognized layouts (conservative, best-effort):
  A  programme-section headings + loose rows with dates/times
  B  single-programme sheets (rows inherit the sole detected programme)
  C  programme columns — rows carrying 2+ paper codes are flagged
     multi_column_row and left programme-unassigned for manual resolution
  D  "date | day | subject | code | time" table lines (PDF or DOCX tables)
  E  multi-page sheets with repeated headers (each page parsed independently)
Unsupported inputs (legacy .doc, scanned/OCR-less PDFs) yield
extraction_failed with a human-readable reason — never a fabricated table.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.orchestrator.context import PROGRAMME_ALIASES, PROGRAMME_PATTERN
from app.orchestrator.extractor import _extract_semester

_PAGE_TEXT_MIN = 5  # per-page char threshold below which a page looks scanned/empty

# ---------------------------------------------------------------------------
# Date-sheet / schedule topic keywords
# ---------------------------------------------------------------------------

_DATESHEET_KEYWORDS = re.compile(
    r"\b(date[- ]?sheet|time[- ]?table|time table|exam(?:ination)?[ ]?schedule|"
    r"schedule of (?:examination|exams|tests)|timetable|date and time schedule)\b",
    re.IGNORECASE,
)

_MONTHS = {
    "january": "01", "february": "02", "march": "03", "april": "04",
    "may": "05", "june": "06", "july": "07", "august": "08",
    "september": "09", "october": "10", "november": "11", "december": "12",
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "jun": "06",
    "jul": "07", "aug": "08", "sep": "09", "sept": "09", "oct": "10",
    "nov": "11", "dec": "12",
}


def _month_num(tok: str) -> str | None:
    return _MONTHS.get(tok.lower())


# day-first dates as used in Indian date sheets: 12-06-2026, 12/6/26,
# 12.06.2026, "12th June 2026", "12 June 2026". Never matches clock times
# (left separator excludes ':'; a preceding digit/':' blocks a match).
_DATELINE_RE = re.compile(
    r"(?<![\d:])(\d{1,2})(st|nd|rd|th)?\s*[-/.\t ]\s*([A-Za-z]{3,9}|1[0-2]|0?[1-9])\s*[-/.\t ]\s*(\d{2,4})(?![\d:])"
)

# ISO order (2026-06-12) shows up in some exported files.
_ISODATE_RE = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")


def _normalize_date(d: int, m_tok: str, y: int) -> tuple[str | None, list[str]]:
    flags: list[str] = []
    try:
        y_full = y if y >= 100 else (2000 + y if y < 50 else 1900 + y)
    except Exception:
        return None, flags  # pragma: no cover - defensive
    mm = _month_num(str(m_tok))
    if mm is None:
        try:
            mi = int(m_tok)
            mm = f"{mi:02d}"
        except (TypeError, ValueError):
            return None, flags
    if not (1 <= int(mm) <= 12) or not (1 <= d <= 31):
        flags.append("invalid_date")
        return None, flags
    if d <= 12 and int(mm) <= 12:
        # DD-MM and MM-DD are both plausible; keep day-first (Indian sheets)
        # but ask an admin to double-check the row.
        flags.append("ambiguous_day_month")
    iso = f"{y_full:04d}-{mm}-{d:02d}"
    try:
        return iso, flags
    except Exception:  # pragma: no cover
        return None, flags


def _find_date(line: str) -> tuple[str | None, list[str]]:
    flags: list[str] = []
    m = _ISODATE_RE.search(line)
    if m:
        iso = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        return iso, flags
    m = _DATELINE_RE.search(line)
    if not m:
        return None, flags
    d = int(m.group(1))
    y = int(m.group(4))
    return _normalize_date(d, m.group(3), y)


# ---------------------------------------------------------------------------
# Time tokens: "10:00 AM", "10:00", "10 AM" — never bare digits without : or
# an AM/PM marker (would collide with dates / paper codes).
# ---------------------------------------------------------------------------

_TIME_TOKEN_RE = re.compile(
    r"(?<!\d)([01]?\d|2[0-3]):([0-5]\d)\s*([AaPp]\.?[Mm]\.?)?"
    r"|(?<!\d)([01]?\d)\s*(?=[AaPp]\.?[Mm]\.?)([AaPp]\.?[Mm]\.?)(?!\d)"
)


def _normalize_time(h: str, minute: str | None, ampm: str | None) -> str | None:
    try:
        hour = int(h)
    except (TypeError, ValueError):
        return None  # pragma: no cover - defensive
    if not 0 <= hour <= 23:
        return None
    if ampm:
        pm = "p" in ampm.lower()
        if pm and hour != 12:
            hour += 12
        elif not pm and hour == 12:
            hour = 0
    mm = minute or "00"
    return f"{hour:02d}:{mm}"


def _find_times(line: str) -> list[str]:
    out: list[str] = []
    for m in _TIME_TOKEN_RE.finditer(line):
        token = _normalize_time(m.group(1), m.group(2), m.group(3))
        if token is None and m.group(5):
            token = _normalize_time(m.group(4), "00", m.group(5))
        if token:
            out.append(token)
    return out


# ---------------------------------------------------------------------------
# Paper codes, day names, venues
# ---------------------------------------------------------------------------

_PAPER_CODE_RE = re.compile(r"\b([A-Z]{2,5}[- ]?\d{3,4})(?!\d)\b", re.IGNORECASE)

_DAY_RE = re.compile(r"\b(Mon|Tues|Wednes|Thurs|Fri|Satur|Sun)day\b", re.IGNORECASE)

_VENUE_KEYWORDS = re.compile(
    r"\b(room\s+no\.?|room\s+number|block\s*[-–—]?\s*[a-z]|"
    r"exam(?:ination)?\s+(?:hall|centre|center)|venue|centre|center)\b",
    re.IGNORECASE,
)

_STREAM_PATTERNS = [
    ("cse", r"\bc\.?s\.?e\b|\bcomputer\s+science\b"),
    ("it", r"\binformation\s+technology\b"),
    ("ece", r"\be\.?c\.?e\b|\belectronics(?:\s*(?:&|and)\s*communication)?\b"),
    ("eee", r"\be\.?e\.?e\b|\belectrical\b"),
    ("mech", r"\bm\.?e\.?ch\b|\bmechanical\b"),
    ("civil", r"\bcivil\b"),
    ("ai", r"\bartificial\s+intelligence\b"),
    ("ds", r"\bdata\s+science\b"),
]
_STREAM_RE = re.compile("|".join(f"(?P<{n}>{p})" for n, p in _STREAM_PATTERNS), re.IGNORECASE)

_ROMAN = {"I": 1, "II": 2, "III": 3, "IV": 4, "V": 5, "VI": 6, "VII": 7,
          "VIII": 8, "IX": 9, "X": 10, "XI": 11, "XII": 12}
_ROMAN_RE = re.compile(r"\bsem(?:ester)?\s*[-–—]?\s*([IVX]{1,4})\b", re.IGNORECASE)


def _detect_stream(text: str) -> str | None:
    m = _STREAM_RE.search(text)
    if not m:
        return None
    for name, _ in _STREAM_PATTERNS:
        if m.group(name):
            return name
    return None


def _detect_semester(text: str) -> int | None:
    num, _word = _extract_semester(text or "")
    if num:
        return num
    for m in _ROMAN_RE.finditer(text or ""):
        rom = m.group(1).upper()
        if rom in _ROMAN:
            return _ROMAN[rom]
    return None


def _find_programmes(text: str) -> list[str]:
    found: set[str] = set()
    for m in PROGRAMME_PATTERN.finditer(text or ""):
        pid = PROGRAMME_ALIASES.get(m.group(0).lower())
        if pid:
            found.add(pid)
    return sorted(found)


def _distinct_programmes(text: str) -> list[str]:
    return _find_programmes(text)


_SEM_WORD_NUM = {
    "first": 1, "second": 2, "third": 3, "fourth": 4, "fifth": 5,
    "sixth": 6, "seventh": 7, "eighth": 8,
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8,
}


def _distinct_semesters(text: str) -> list[int]:
    sems = set()
    for m in _ROMAN_RE.finditer(text):
        rom = m.group(1).upper()
        if rom in _ROMAN:
            sems.add(_ROMAN[rom])
    for m in re.compile(r"\bsem(?:ester)?[ -]?(\d{1,2})\b|\b(\d{1,2})(?:st|nd|rd|th)\s+sem\b", re.IGNORECASE).finditer(text):
        g = m.group(1) or m.group(2)
        if g and 1 <= int(g) <= 12:
            sems.add(int(g))
    for word, num in _SEM_WORD_NUM.items():
        if re.search(rf"\b{word}\s+sem(?:ester)?\b", text, re.IGNORECASE):
            sems.add(num)
    return sorted(sems)


def _detect_exam_type(text: str) -> str | None:
    for label, pat in (
        ("supplementary", r"\bsupplementar\w+\b"),
        ("special", r"\bspecial\b"),
        ("reappear", r"\bre-?appear\w+\b|\bcompartment\b"),
        ("annual", r"\bannual\b"),
        ("semester", r"\bsemester\b"),
        ("regular", r"\bregular\b"),
    ):
        if re.search(pat, text, re.IGNORECASE):
            return label
    return None


_SESSION_RE = re.compile(
    r"\b(?:january|february|march|april|may|june|july|august|september|"
    r"october|november|december)\s+(20\d{2})\b",
    re.IGNORECASE,
)


def _detect_session_label(early_text: str) -> str | None:
    m = _SESSION_RE.search(early_text[:4000])
    if m:
        return m.group(0)
    return None


# ---------------------------------------------------------------------------
# Row building
# ---------------------------------------------------------------------------

@dataclass
class ExtractionResult:
    notice_type: str = "notice"                     # "date_sheet" | "notice"
    extraction_status: str = "pending_verification"  # pending_verification | extraction_failed
    extraction_error: str | None = None
    exam_type: str | None = None
    exam_session_label: str | None = None
    programme_ids: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _clean_subject(text: str, exclude_substrings: list[str]) -> str:
    cleaned = text
    for token in exclude_substrings:
        cleaned = cleaned.replace(token, " ")
    cleaned = re.sub(r"[|,+;]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .:-–—")
    # Drop a trailing connective left over from a venue/time boundary.
    cleaned = re.sub(r"\s+(?:to|and|&)\s*$", "", cleaned).strip(" .:-–—")
    return cleaned


def _extract_venue(text: str) -> str | None:
    m = _VENUE_KEYWORDS.search(text)
    if not m:
        return None
    chunk = text[m.start():]
    chunk = chunk.split("|")[0]
    bounds = [
        t.start()
        for t in list(_TIME_TOKEN_RE.finditer(text)) + list(_PAPER_CODE_RE.finditer(text))
        if t.start() > m.start()
    ]
    if bounds:
        chunk = chunk[: min(bounds) - m.start()]
    chunk = chunk.strip(" |:–—,-")
    return chunk[:60] or None


@dataclass
class _Ctx:
    prog: str | None = None
    sem: int | None = None
    stream: str | None = None


def _parse_row(text: str, page: int | None, section: str | None, ctx: _Ctx) -> dict:
    flags: list[str] = []
    iso, date_flags = _find_date(text)
    flags.extend(date_flags)

    row: dict = {
        "programme_id": None,
        "programme_name": None,
        "stream": None,
        "semester": None,
        "batch": None,
        "exam_type": None,
        "exam_date": iso,
        "day": None,
        "start_time": None,
        "end_time": None,
        "subject_code": None,
        "subject": None,
        "paper_code": None,
        "venue": None,
        "source_page": page,
        "source_section": section,
        "raw": text.strip(),
        "validation_flags": flags,
    }
    if not iso:
        row["validation_flags"].append("invalid_date")
        return row

    dm = _DAY_RE.search(text)
    if dm:
        row["day"] = dm.group(0)

    times = _find_times(text)
    if times:
        row["start_time"] = times[0]
        if len(times) >= 2:
            row["end_time"] = times[-1]
        else:
            row["validation_flags"].append("missing_end_time")
    else:
        row["validation_flags"].append("missing_time")

    codes = sorted({c for c in _PAPER_CODE_RE.findall(text)})
    multi_column = len(codes) > 1
    if multi_column:
        row["validation_flags"].append("multi_column_row")
    if len(codes) == 1:
        row["paper_code"] = codes[0]

    row["venue"] = _extract_venue(text)

    excludes: list[str] = []
    for m in _DATELINE_RE.finditer(text):
        excludes.append(m.group(0))
    for m in _ISODATE_RE.finditer(text):
        excludes.append(m.group(0))
    for m in _TIME_TOKEN_RE.finditer(text):
        excludes.append(m.group(0))
    if row["day"]:
        excludes.append(row["day"])
    if row["venue"]:
        excludes.append(row["venue"])
    excludes.extend(codes)
    subject = _clean_subject(text, excludes)
    if subject:
        row["subject"] = subject[:300]
    else:
        row["validation_flags"].append("missing_subject")

    if multi_column:
        # Programme-column layouts cannot be safely attributed row-by-row;
        # leave unassigned for admin resolution rather than guessing.
        row["validation_flags"].append("unassigned_programme")
    else:
        if ctx.prog:
            row["programme_id"] = ctx.prog
        else:
            row["validation_flags"].append("unassigned_programme")
        if ctx.sem:
            row["semester"] = str(ctx.sem)
        if ctx.stream:
            row["stream"] = ctx.stream
    return row


def _find_date_only_iso(line: str) -> str | None:
    iso, _ = _find_date(line)
    return iso


# ---------------------------------------------------------------------------
# Batch / family metadata (document-level, copied verbatim — never guessed)
# ---------------------------------------------------------------------------

_BATCH_RE = re.compile(
    r"\bbatch\w*\b[^0-9A-Za-z]{0,24}?(20\d{2}(?:\s*[-/]\s*\d{2,4})?)",
    re.IGNORECASE,
)


def _extract_batch(text: str) -> str | None:
    """First \"Batch ...\" year the document/cell states (e.g. \"Batch 2025\",
    \"(Batch 2024 only)\"). Returns the verbatim year string or None."""
    m = _BATCH_RE.search(text or "")
    if m:
        return m.group(1).strip()
    return None


def _detect_family_ids(text: str) -> set[str]:
    """Family-level ids ('pg' / 'ug') lifted from the header only when the
    sheet says PG/UG outright. Degree/discipline ids come from the programme
    alias table or the column labels instead."""
    fam: set[str] = set()
    if re.search(r"\bPG\b", text or "", re.IGNORECASE):
        fam.add("pg")
    if re.search(r"\bUG\b", text or "", re.IGNORECASE):
        fam.add("ug")
    return fam


# ---------------------------------------------------------------------------
# Multi-column table extraction (PDF word geometry) — Format E/F and the UG
# group-column sheets. Uses true x/y coordinates so each programme's subject
# cell is recovered per column instead of a flattened text blob.
# ---------------------------------------------------------------------------

_GEO_CLUSTER_TOL = 4.0       # words within ~4pt of a row share a visual line
_GEO_BAND_WINDOW = 22.0      # header band = lines within 22pt of the "Date" header word
_GEO_COL_TOL = 12.0          # a header word still belongs to a column 12pt away
_GEO_CELL_TOL = 6.0          # a data word's centre must fall this close to the column

_GEO_DATE_WORD_RE = re.compile(r"^\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}$|^20\d{2}-\d{2}-\d{2}$")
_GEO_DATE_LABEL = "Date"


def _geo_lines(words: list[dict]) -> list[dict]:
    """Cluster word boxes into visual lines by y0 proximity."""
    ordered = sorted(words or [], key=lambda w: w["y0"])
    lines: list[dict] = []
    for w in ordered:
        if lines and w["y0"] - lines[-1]["y"] <= _GEO_CLUSTER_TOL:
            lines[-1]["words"].append(w)
        else:
            lines.append({"y": w["y0"], "words": [w]})
    for ln in lines:
        ln["words"].sort(key=lambda w: w["x0"])
        ln["y"] = min(w["y0"] for w in ln["words"])
    return lines


def _line_text(ln: dict) -> str:
    return " ".join(w["text"] for w in ln["words"])


def _geo_build_columns(band_words: list[dict]) -> list[dict]:
    """Group header words into distinct column bands by x-overlap (wrapped
    labels like \"Artificial / Intelligence & Machine / Learning\" merge into
    one column; sibling headers keep separate bands)."""
    cols: list[dict] = []
    for w in sorted(band_words, key=lambda w: (w["y0"], w["x0"])):
        anchor = None
        for c in cols:
            if w["x0"] <= c["x1"] + _GEO_COL_TOL and w["x1"] >= c["x0"] - _GEO_COL_TOL:
                anchor = c
                break
        if anchor is None:
            cols.append({"x0": w["x0"], "x1": w["x1"], "label_words": [w]})
        else:
            anchor["x0"] = min(anchor["x0"], w["x0"])
            anchor["x1"] = max(anchor["x1"], w["x1"])
            anchor["label_words"].append(w)
    cols.sort(key=lambda c: c["x0"])
    return cols


def _geo_col_label(col: dict) -> str:
    return re.sub(r"\s+", " ", " ".join(w["text"] for w in col["label_words"])).strip()


# Programme-column header -> canonical programme id. Long/specific patterns are
# tested first; unknown labels return (None, label) so rows stay unassigned.
_COLUMN_PROGRAMME_MAP = [
    (r"bio\s*[-–—]?\s*chemistry|biochemistr\w*", "bio-chemistry"),
    (r"computer\s+applications?", "computer-applications"),
    (r"computer\s+science", "computer-science"),
    (r"business\s+administration", "business-administration"),
    (r"artificial\s+intelligence|machine\s+learning|ai\s*&\s*ml|ai\s+and\s+ml", "ai-ml"),
    (r"\bdata\s+science\b", "data-science"),
    (r"environmental\s+science", "environmental-science"),
    (r"political\s+science", "political-science"),
    (r"journalism", "journalism-mass-communication"),
    (r"home\s+science", "home-science"),
    (r"^physics\b", "physics"),
    (r"^chemistry\b", "chemistry"),
    (r"^botany\b", "botany"),
    (r"^zoology\b", "zoology"),
    (r"^history\b", "history"),
    (r"^economics\b", "economics"),
    (r"^geography\b", "geography"),
    (r"^english\b", "english"),
    (r"^music\b", "music"),
    (r"^education\b", "education"),
    (r"^\s*b\.?\s*\.?\s*\.?\s*b\.?\s*a\b", "bba"),
    (r"^\s*b\.?\s*\.?\s*\.?\s*c\.?\s*a\b", "bca"),
    (r"^\s*b\.?\s*\.?\s*\.?\s*tech\b", "btech"),
    (r"^\s*b\.?\s*\.?\s*a(?:\b|\.)", "ba"),
    (r"^\s*b\.?\s*\.?\s*s\.?\s*c(?:\b|\.)", "bsc"),
    (r"^\s*b\.?\s*\.?\s*\.?\s*com(?:\b|\.)", "bcom"),
    (r"^\s*m\.?\s*\.?\s*\.?\s*b\.?\s*a\b", "mba"),
    (r"^\s*m\.?\s*\.?\s*\.?\s*a(?:\b|\.)", "ma"),
    (r"^\s*m\.?\s*\.?\s*\.?\s*s\.?\s*c(?:\b|\.)", "msc"),
    (r"^\s*m\.?\s*\.?\s*\.?\s*com(?:\b|\.)", "mcom"),
]


def _column_programme(label: str) -> tuple[str | None, str]:
    lab = re.sub(r"\s+", " ", label or "").strip().strip(".:,;")
    if lab.lower() == _GEO_DATE_LABEL.lower():
        return None, lab
    for pattern, pid in _COLUMN_PROGRAMME_MAP:
        if re.search(pattern, lab, re.IGNORECASE):
            return pid, lab
    return None, lab


def _geo_cell_text(row_words: list[dict], col: dict) -> str:
    """Verbatim words inside one column band of a table row, reading order.
    Standalone '-' (an intentionally empty cell) collapses to empty text."""
    hit: list[dict] = []
    for w in row_words:
        cx = (w["x0"] + w["x1"]) / 2.0
        if col["x0"] - _GEO_CELL_TOL <= cx <= col["x1"] + _GEO_CELL_TOL:
            if w["text"].strip() == "-":
                continue
            hit.append(w)
    hit.sort(key=lambda w: (w["y0"], w["x0"]))
    text = re.sub(r"\s+", " ", " ".join(w["text"] for w in hit)).strip(" .:-–—")
    return text


_PAGE_FOOTER_RE = re.compile(
    r"^\s*note\b"
    r"|^\s*(?:no|ref(?:erence)?|dtd?|dated?)\s*[:.)]"
    r"|^\s*sd\b\s*/"
    r"|^\s*copy\s+(?:to|for)\b|^\s*copy\s*[:.]"
    r"|^\s*candidates?\b"
    r"|^\s*office\s+of\s+the\b"
    r"|^\s*\d+[.)]\s*(?:candidates?\b|admit\s+cards?\b|identity\s+cards?\b|download\b|\S*classwork\s+for\s+next\b|deans?\b|principals?\b|p\.?\s*s\.?\s+to\b|office\s+file\b|in-charge\b)"
    r"|controllers?\s+of\s+examinations?\b"
    r"|^the\s+controller\b",
    re.IGNORECASE,
)


def _is_page_footer_line(text: str) -> bool:
    return bool(_PAGE_FOOTER_RE.search(text or ""))


def _geo_extract_page(block: dict) -> tuple[list[dict], bool]:
    """Extract one multi-column table page into row dicts (no row_no).

    Returns ([], False) when the page has no usable multi-column header so the
    caller falls back to the line-based parser. When the page's drawn grid
    lines are available they define the true row bands; otherwise date-anchored
    bands are used.
    """
    words = block.get("words") or []
    page = block.get("page")
    lines = _geo_lines(words)
    band_ys = [
        ln["y"]
        for ln in lines
        if any(w["text"].strip() == _GEO_DATE_LABEL and 25 <= w["x0"] <= 130 for w in ln["words"])
    ]
    if not band_ys:
        return [], False
    anchors = [
        ln
        for ln in lines
        if any(_GEO_DATE_WORD_RE.fullmatch(w["text"].strip()) and w["x0"] <= 130 for w in ln["words"])
    ]
    if not anchors:
        return [], False

    band_cols: list[tuple[float, list[dict]]] = []
    for by in band_ys:
        band_words = [w for w in words if abs(w["y0"] - by) <= _GEO_BAND_WINDOW]
        cols = _geo_build_columns(band_words)
        programme_cols = [c for c in cols if _column_programme(_geo_col_label(c))[0] is not None]
        if len(programme_cols) < 2:
            return [], False
        band_cols.append((by, cols))

    # Drawn vertical grid lines give exact column extents; the header labels
    # can be narrower than their cells, so widen each column to its grid band
    # (still labelled by its header words).
    vlines = block.get("vlines")
    if vlines and len(vlines) >= 2:
        grid_bands = list(zip(vlines, vlines[1:]))
        for _by, cols in band_cols:
            for col in cols:
                cxs = [w for w in col["label_words"] if w["text"].strip() != _GEO_DATE_LABEL]
                cx = ((min(w["x0"] for w in cxs) + max(w["x1"] for w in cxs)) / 2.0) if cxs else None
                if cx is None:
                    continue
                for g0, g1 in grid_bands:
                    if g0 <= cx <= g1 and (g1 - g0) > (col["x1"] - col["x0"]):
                        col["x0"], col["x1"] = g0, g1
                        break

    def active_cols_for(y: float) -> list[dict] | None:
        result: list[dict] | None = None
        for by, cols in band_cols:
            if by < y:
                result = cols
        return result

    def make_row(date_word: dict, row_words: list[dict], cols: list[dict],
                 default_flags: list[str]) -> list[dict]:
        iso, date_flags = _find_date(date_word["text"].strip())
        flags = sorted(set(default_flags) | set(date_flags))
        out: list[dict] = []
        for col in cols:
            pid, pname = _column_programme(_geo_col_label(col))
            if pid is None:
                continue
            cell_text = _geo_cell_text(row_words, col)
            if not cell_text:
                continue
            out.append(
                {
                    "programme_id": pid,
                    "programme_name": pname or None,
                    "stream": None,
                    "semester": None,
                    "batch": None,
                    "exam_type": None,
                    "exam_date": iso,
                    "day": None,
                    "start_time": None,
                    "end_time": None,
                    "subject_code": None,
                    "subject": cell_text[:300],
                    "paper_code": None,
                    "venue": None,
                    "source_page": page,
                    "source_section": f"column:{pname}",
                    "raw": f"{date_word['text'].strip()} {cell_text}",
                    "validation_flags": flags,
                }
            )
        return out

    rows: list[dict] = []

    # Preferred path: drawn grid lines give exact row bands (words can spill
    # across the band a pure word-geometry pass would infer).
    hlines = block.get("hlines")
    if hlines and len(hlines) >= 2:
        date_words = [w for w in words if _GEO_DATE_WORD_RE.fullmatch(w["text"].strip()) and w["x0"] <= 130]
        for y_lo, y_hi in zip(hlines, hlines[1:]):
            if y_hi - y_lo > 80.0:
                continue  # a big gap is not a table row
            in_band = [w for w in date_words if y_lo <= w["y0"] < y_hi]
            if len(in_band) != 1:
                continue
            cols = active_cols_for(y_lo)
            if cols is None:
                continue
            row_words = [w for w in words if y_lo <= w["y0"] < y_hi]
            rows.extend(make_row(in_band[0], row_words, cols, []))
        if rows:
            return rows, True
        # no border bands produced rows (odd drawing) — fall back below

    # Fallback: date-anchored bands (word-extent heuristics).
    band_word_ids = {
        id(w)
        for by, _cols in band_cols
        for w in words
        if abs(w["y0"] - by) <= _GEO_BAND_WINDOW
    }
    page_max_y = max((w["y0"] for w in words), default=0.0)
    footer_y = min(
        (ln["y"] for ln in lines if _is_page_footer_line(_line_text(ln))),
        default=page_max_y + 200.0,
    )

    for i, anchor in enumerate(anchors):
        cols = active_cols_for(anchor["y"])
        if cols is None:
            continue
        if i == 0:
            y_lo = max(by for by, _cols in band_cols if by < anchor["y"]) - _GEO_BAND_WINDOW + 2.0
        else:
            y_lo = anchors[i - 1]["y"] + 2.0
        y_hi = anchors[i + 1]["y"] + 2.0 if i + 1 < len(anchors) else footer_y
        row_words = [w for w in words if y_lo <= w["y0"] < y_hi and id(w) not in band_word_ids]

        start_word = next(
            (
                w
                for w in anchor["words"]
                if _GEO_DATE_WORD_RE.fullmatch(w["text"].strip()) and w["x0"] <= 130
            ),
            None,
        )
        if start_word is None:
            continue
        rows.extend(make_row(start_word, row_words, cols, []))
    return rows, True


def _parse_column_pages(pages: list[dict]) -> list[dict]:
    """Column-mode extraction across pages; [] when no page qualifies."""
    doc_text = "\n".join((b.get("text") or "") for b in pages)
    # Semester/batch come from the header region (the page-footer note lines
    # elsewhere in the sheet would otherwise pollute them, e.g. "2nd Classwork
    # for Semester 3rd ...").
    head_text = doc_text[:900]
    doc_batch = _extract_batch(head_text)
    doc_sem = _detect_semester(head_text)
    rows_out: list[dict] = []
    used = False
    for block in pages:
        words = block.get("words")
        if not words:
            continue
        page_rows, ok = _geo_extract_page(block)
        if not ok:
            continue
        used = True
        for r in page_rows:
            if r["semester"] is None:
                r["semester"] = str(doc_sem) if doc_sem else None
            if r["batch"] is None:
                r["batch"] = _extract_batch(r["raw"]) or doc_batch
            rows_out.append(r)
    return rows_out if used else []


# ---------------------------------------------------------------------------
# Line-mode row expansion: slash-joined subjects and numbered subject lists
# under one date become separate rows (same date), everything else stays one.
# ---------------------------------------------------------------------------

_FOOTER_NOISE_RE = re.compile(
    r"^\s*note\b"
    r"|^\s*(?:no|ref(?:erence)?|dtd?|dated?)\s*[:.)]"
    r"|^\s*sd\b\s*/"
    r"|^\s*copy\s+(?:to|for)\b|^\s*copy\s*[:.]"
    r"|^\s*candidates?\b"
    r"|^\s*office\s+of\s+the\b"
    r"|^\s*\d+[.)]\s*(?:candidates?\b|admit\s+cards?\b|identity\s+cards?\b|download\b|\S*classwork\s+for\s+next\b|deans?\b|principals?\b|p\.?\s*s\.?\s+to\b|office\s+file\b|in-charge\b)"
    r"|^\s*(?:2nd\s+)?classwork\s+for\s+next\b"
    r"|controllers?\s+of\s+examinations?\b"
    r"|CUS/Exam/DS",
    re.IGNORECASE,
)


def _is_footer_noise(line: str) -> bool:
    return bool(_FOOTER_NOISE_RE.search(line or ""))


def _strip_number(item: str) -> str:
    return re.sub(r"^\s*\d+[.)]\s*", "", item).strip(" .:-–—")


def _subject_items(clean: list[str]) -> list[tuple[str, str]]:
    """Reduce row continuation lines to (item_text, raw_line) pairs.

    Every source line after the date line is one item; slash-separated
    subjects (\"A / B\") split into two items and numbered lists are one item
    per number. This reproduces stacked multi-subject rows verbatim. The
    caller merges a single remaining item back into the date line (one row).
    """
    items: list[tuple[str, str]] = []
    for ln in clean[1:]:
        if " / " in ln:
            for part in re.split(r"\s+/\s+", ln):
                it = part.strip(" .:،,;-") if part.strip() else ""
                if it:
                    items.append((_strip_number(it), ln))
        else:
            it = _strip_number(ln)
            if it and any(ch.isalnum() for ch in it):
                items.append((it, ln))
    return items


def _split_subject_items(lines: list[str]) -> list[tuple[str, str]]:
    """Return (text, raw) row fragments for one aggregate row."""
    clean = [ln.strip() for ln in lines if ln and ln.strip()]
    if not clean:
        return []
    base = clean[0]
    raw_all = "\n".join(clean)

    date_spans = sorted(
        list(_DATELINE_RE.finditer(base)) + list(_ISODATE_RE.finditer(base)),
        key=lambda m: m.start(),
    )
    date_text = "".join(m.group(0) for m in date_spans)
    subject_zone = base
    for m in reversed(date_spans):
        subject_zone = subject_zone[: m.start()] + " " + subject_zone[m.end():]

    items = _subject_items(clean)
    if len(items) >= 2:
        return [(f"{date_text} {item}".strip(), raw) for item, raw in items]

    # A date line that already carries an inline subject followed by one or
    # more continuation lines means distinct stacked subjects share the date
    # (e.g. "18-09-2026 Teaching of English" + "Teaching of Hindi").
    if len(items) == 1 and subject_zone.strip():
        return [
            (f"{date_text} {subject_zone.strip()}".strip(), clean[0]),
            (f"{date_text} {items[0][0]}".strip(), items[0][1]),
        ]

    if not items and date_spans:
        parts = [p.strip(" .:،,;-") for p in re.split(r"\s+/\s+", subject_zone) if p.strip()]
        if (
            len(parts) >= 2
            and not any(_TIME_TOKEN_RE.search(p) for p in parts)
            and not any(_PAPER_CODE_RE.search(p) for p in parts)
        ):
            return [(f"{date_text} {p}".strip(), raw_all) for p in parts]

    return [(raw_all, raw_all)]


_DOC_TIME_LABEL_RE = re.compile(
    r"\b(?:examination\s+time|time\s+of\s+examination|timings?\s+of\s+examination|exam(?:ination)?\s+timings?)\b",
    re.IGNORECASE,
)


def _document_times(lines: list[str]) -> tuple[str | None, str | None]:
    """A single doc-level uniform time header, e.g. \"Examination Time: 10:30 AM\".

    Some date sheets print one labelled examination time for the whole table and
    no per-row time column. When such a label is found on a line with a parsed
    time, the value is copied verbatim (start, plus the last token when the
    header carries a full range) for later application to rows — never invented.
    """
    for line in lines:
        if not _DOC_TIME_LABEL_RE.search(line):
            continue
        times = _find_times(line)
        if times:
            start = times[0]
            end = times[-1] if len(times) >= 2 else None
            return start, end
    return None, None


def _iter_lines(pages: list[dict]):
    for block in pages or []:
        rows = block.get("rows")
        if isinstance(rows, list):
            page = block.get("page")
            section = block.get("section")
            for cells in rows:
                vals = [(c or "").strip() for c in cells if (c or "").strip()]
                if vals:
                    yield page, section, " | ".join(vals)
        else:
            page = block.get("page")
            section = block.get("section")
            text = block.get("text", "")
            for raw in text.splitlines():
                ln = " ".join(raw.split())
                if ln:
                    yield page, section, ln


def _is_headline(line: str) -> bool:
    ll = line.strip()
    if not ll or len(ll) > 90:
        return False
    if _find_date_only_iso(ll):
        return False
    if _find_programmes(ll) or re.search(r"\bsem(?:ester)?\b", ll, re.IGNORECASE):
        return True
    return False


def parse_date_sheet(pages: list[dict]) -> ExtractionResult:
    """Parse a normalized document (list of page/text/table blocks).

    Never raises for layout that is not understood; it degrades to
    extraction_failed with a reason so an admin can do a manual entry.
    """
    lines = list(_iter_lines(pages))
    all_text = "\n".join(line for _pg, _sec, line in lines)
    head_text = all_text[:900]
    doc_start, doc_end = _document_times([line for _pg, _sec, line in lines])

    # Detect empty / scanned content before anything else.
    meaningful = re.sub(r"[\s|_]+", "", all_text)
    if len(meaningful) < _PAGE_TEXT_MIN:
        return ExtractionResult(
            notice_type="date_sheet" if _DATESHEET_KEYWORDS.search(all_text) else "notice",
            extraction_status="extraction_failed",
            extraction_error="No extractable text found (file looks empty or scanned without an OCR layer).",
        )

    is_datesheet = bool(_DATESHEET_KEYWORDS.search(all_text))
    exam_type = _detect_exam_type(all_text)
    session_label = _detect_session_label(all_text)

    ctx = _Ctx()
    rows_out: list[dict] = []
    pending: list[str] = []
    pending_meta: tuple[int | None, str | None] | None = None
    warnings: list[str] = []

    def apply_headline(line: str) -> None:
        progs = _find_programmes(line)
        if len(progs) == 1:
            ctx.prog = progs[0]
        sem = _detect_semester(line)
        if sem:
            ctx.sem = sem
        stream = _detect_stream(line)
        if stream:
            ctx.stream = stream

    def flush() -> None:
        nonlocal pending, pending_meta
        if not pending:
            return
        fragments = _split_subject_items(pending)
        for text, raw in fragments:
            row_ctx = _Ctx(**ctx.__dict__)
            row = _parse_row(text, pending_meta[0], pending_meta[1], row_ctx)
            row["raw"] = raw
            rows_out.append(row)
        pending = []
        pending_meta = None

    try:
        for page, section, line in lines:
            if _is_footer_noise(line):
                flush()
                continue
            if _find_date_only_iso(line):
                flush()
                pending = [line]
                pending_meta = (page, section)
                continue
            if pending:
                if _is_headline(line):
                    flush()
                    apply_headline(line)
                else:
                    pending.append(line)
            elif _is_headline(line):
                apply_headline(line)
        flush()
    except Exception as exc:  # pragma: no cover - defensive
        return ExtractionResult(
            notice_type="date_sheet",
            extraction_status="extraction_failed",
            extraction_error=f"Parser error while processing rows: {exc}",
            exam_type=exam_type,
            exam_session_label=session_label,
        )

    distinct_progs = _distinct_programmes(head_text)
    distinct_sems = _distinct_semesters(head_text)
    doc_batch = _extract_batch(head_text)
    family_ids = _detect_family_ids(head_text)

    # Multi-column pages (Format E/F group sheets) — column-mode extraction is
    # authoritative whenever a page's true table geometry yields 2+ programme
    # columns (the flattened line stream of such pages is structure-less
    # junk, so it is discarded in favour of the geometry-derived rows).
    col_rows = _parse_column_pages(pages)
    if col_rows:
        rows_out = col_rows
        distinct_progs = sorted(
            {r["programme_id"] for r in rows_out if r["programme_id"]} | family_ids
        )
        distinct_sems = sorted({int(r["semester"]) for r in rows_out if r["semester"]})
    else:
        # Single-programme sheets: attribute rows that had no section context.
        if len(distinct_progs) == 1:
            for r in rows_out:
                if r["programme_id"] is None and "multi_column_row" not in r["validation_flags"]:
                    r["programme_id"] = distinct_progs[0]
                    if "unassigned_programme" in r["validation_flags"]:
                        r["validation_flags"].remove("unassigned_programme")
        if len(distinct_sems) == 1 and rows_out:
            for r in rows_out:
                if r["semester"] is None and "multi_column_row" not in r["validation_flags"]:
                    r["semester"] = str(distinct_sems[0])
        if family_ids:
            distinct_progs = sorted(set(distinct_progs) | family_ids)

    # Row-level inherited fields from the document.
    for idx, r in enumerate(rows_out, start=1):
        r["row_no"] = idx
        r["exam_type"] = exam_type
        if not r.get("batch"):
            r["batch"] = _extract_batch(r.get("raw") or "") or doc_batch
        if not r["start_time"] and not r["end_time"] and doc_start:
            # Doc-level uniform time (verbatim from a labelled header line).
            r["start_time"] = doc_start
            if doc_end:
                r["end_time"] = doc_end
            if "missing_time" in r["validation_flags"]:
                r["validation_flags"].remove("missing_time")
            if not r["end_time"] and "missing_end_time" not in r["validation_flags"]:
                r["validation_flags"].append("missing_end_time")
            r["validation_flags"] = sorted(set(r["validation_flags"]))
            warnings.append(f"row {idx}: applied document-level examination time {doc_start}"
                            + (f" - {doc_end}" if doc_end else ""))
        if r["start_time"] and not r["end_time"]:
            warnings.append(f"row {idx}: only a start time was found")

    if len(distinct_progs) == 1 and rows_out:
        for r in rows_out:
            if r["programme_id"] is None:
                r["programme_id"] = distinct_progs[0]
                if "unassigned_programme" in r["validation_flags"]:
                    r["validation_flags"].remove("unassigned_programme")

    if not rows_out:
        if is_datesheet:
            return ExtractionResult(
                notice_type="date_sheet",
                extraction_status="extraction_failed",
                extraction_error=(
                    "The document looks like a date sheet but no structured "
                    "schedule rows could be extracted (unsupported layout, "
                    "legacy .doc, or scanned image)."
                ),
                exam_type=exam_type,
                exam_session_label=session_label,
                programme_ids=distinct_progs,
                warnings=warnings,
            )
        # A general notice with no schedule table.
        return ExtractionResult(
            notice_type="notice",
            extraction_status="pending_verification",
            exam_type=exam_type,
            exam_session_label=session_label,
            programme_ids=distinct_progs,
            warnings=warnings,
        )

    status = "pending_verification"

    return ExtractionResult(
        notice_type="date_sheet" if is_datesheet else "notice",
        extraction_status=status,
        exam_type=exam_type,
        exam_session_label=session_label,
        programme_ids=distinct_progs,
        rows=rows_out,
        warnings=warnings,
    )