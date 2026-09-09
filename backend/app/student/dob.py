"""
backend/app/student/dob.py

The student's Date of Birth IS their password.

This module owns the ONE canonical server-side representation used for both
hashing (@ create / credential reset) and verification (@ /api/student/verify):

    canonical DOB  ->  YYYY-MM-DD  ->  bcrypt hash  ->  Student.hashed_password

The same `normalize_dob` function is used everywhere credentials are created,
reset or verified so the two directions can never drift apart.

`normalize_dob` accepts the formats a real user may reasonably type or a date
picker submits:
  - ISO        2004-07-15  /  2004/07/15
  - DMY numeric 15-07-2004  /  15/07/2004
  - text month  15-Jul-2004 / 15 July 2004 / Jul 15 2004
and returns the canonical YYYY-MM-DD string (or raises ValueError).
"""

from __future__ import annotations

from datetime import date

from app.auth.security import hash_password, verify_password

_MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


def _month_of(token: str) -> int | None:
    return _MONTHS.get(token[:3].lower())


def normalize_dob(value) -> str:
    """Return the canonical YYYY-MM-DD string used for hashing/verification.

    Raises ValueError for missing, ambiguous or impossible dates (e.g. a DOB
    of 31-Feb-2004), so callers can translate it into a 4xx response without
    ever echoing credential material in the error.
    """
    raw = str(value).strip()
    if not raw:
        raise ValueError("Date of birth is required")
    parts = [p for p in raw.replace("/", " ").replace("-", " ").replace(".", " ").split() if p]
    if len(parts) != 3:
        raise ValueError("Date of birth must be a valid calendar date")

    numeric: dict[int, int] = {}
    month_pos: int | None = None
    month: int | None = None
    for i, tok in enumerate(parts):
        m = _month_of(tok)
        if m is not None:
            if month is not None:
                raise ValueError("Date of birth must be a valid calendar date")
            month_pos, month = i, m
        elif tok.isdigit():
            numeric[i] = int(tok)
        else:
            raise ValueError("Date of birth must be a valid calendar date")

    day = month_name = year = None
    if month is not None:
        if len(numeric) != 2:
            raise ValueError("Date of birth must be a valid calendar date")
        if month_pos == 0:      # Jul 15 2004
            day, year = numeric[1], numeric[2]
        elif month_pos == 1:    # 15-Jul-2004 / 15 July 2004
            day, year = numeric[0], numeric[2]
        else:                   # 15 2004 Jul (unusual but deterministic)
            day, year = numeric[0], numeric[1]
        month_name = month
    else:
        if len(numeric) != 3:
            raise ValueError("Date of birth must be a valid calendar date")
        if len(parts[0]) == 4:  # 2004-07-15 -> year first
            year, month_name, day = numeric[0], numeric[1], numeric[2]
        else:                   # 15-07-2004 -> day first (Indian/university convention)
            day, month_name, year = numeric[0], numeric[1], numeric[2]

    try:
        return date(year, month_name, day).strftime("%Y-%m-%d")
    except ValueError:
        raise ValueError("Date of birth must be a valid calendar date")


def hash_dob(value) -> str:
    """Normalize the DOB and return a bcrypt hash (the stored credential)."""
    return hash_password(normalize_dob(value))


def verify_dob(dob: str, hashed: str) -> bool:
    """Normalize the supplied DOB and verify it against the stored bcrypt hash.

    Never raises: an unparseable DOB simply fails verification (the route
    applies its own timing equalisation when it needs stronger indistinguishability).
    """
    try:
        canonical = normalize_dob(dob)
    except ValueError:
        return False
    return verify_password(canonical, hashed)