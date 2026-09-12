"""backend/app/notices/validator.py — row-level schedule verification rules.

The verify gate is structural: a notice can only be marked VERIFIED when every
non-deleted schedule row is "verify-ready" (date present and sane, subject or
paper code present, a time window present). Missing or invalid facts are never
invented here — they block verification and stay visible to the admin, who must
correct or delete the offending rows.
"""

from __future__ import annotations

import re

_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME24_RE = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def iso_date_ok(value: str | None) -> bool:
    if not value:
        return False
    return bool(_ISO_DATE_RE.match(value))


def time_ok(value: str | None) -> bool:
    if not value:
        return False
    return bool(_TIME24_RE.match(value))


def row_error_summary(row: dict) -> list[str]:
    """Normalize the validation flags on a row to human-readable problems."""
    flags = set(row.get("validation_flags") or [])
    if not iso_date_ok(row.get("exam_date")):
        flags.add("invalid_date")
        flags.add("missing_exam_date")
    if not (row.get("subject") or row.get("paper_code")):
        flags.add("missing_subject")
    if not (row.get("start_time") or row.get("end_time")):
        flags.add("missing_time")
    if row.get("start_time") and not time_ok(row["start_time"]):
        flags.add("invalid_start_time")
    if row.get("end_time") and not time_ok(row["end_time"]):
        flags.add("invalid_end_time")
    return sorted(flags)


def is_verify_ready(row: dict) -> bool:
    """Preconditions for a row to be part of a VERIFIED schedule:
       - an explicit, well-formed exam date
       - a subject and/or a paper code
       - at least one bound of the time window
    Every other field (venue, day, stream, batch) may legitimately be missing
    and stays missing — that never blocks verification.
    """
    if not iso_date_ok(row.get("exam_date")):
        return False
    if not (row.get("subject") or row.get("paper_code")):
        return False
    if not (row.get("start_time") or row.get("end_time")):
        return False
    if row.get("start_time") and not time_ok(row["start_time"]):
        return False
    if row.get("end_time") and not time_ok(row["end_time"]):
        return False
    return True


_BLOCKING_PROBLEMS = frozenset({
    "invalid_date",
    "missing_exam_date",
    "missing_subject",
    "missing_time",
    "invalid_start_time",
    "invalid_end_time",
})


def verify_ready_problems(rows: list[dict]) -> list[str]:
    """Structural blockers only — the exact conditions is_verify_ready() gates on.

    Attention annotations (ambiguous_day_month, unassigned_programme,
    multi_column_row, missing_end_time, ...) stay persisted and visible to the
    admin in the schedule review UI, but a row with BOTH time bounds missing is
    the only time-related hard blocker: a single printed bound (e.g. a doc-level
    "Examination Time" or a start-only shift) is a legitimate verify-ready row.
    """
    problems: list[str] = []
    for idx, row in enumerate(rows, start=1):
        for flag in row_error_summary(row):
            if flag in _BLOCKING_PROBLEMS:
                problems.append(f"row {row.get('row_no', idx)}: {flag.replace('_', ' ')}")
    return problems