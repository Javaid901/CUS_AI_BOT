"""
backend/app/student_admit_card/service.py

Data layer for Student Admit Card (Phase C).

Design notes:
  - Server-side identity: every student lookup path derives student_id from
    the resolved StudentSession (dict from app.student.session.resolve_session);
    no client-controlled identifier ever influences the WHERE clause.
  - Semester allowlist: _ALLOWLIST is the single source
    of truth (API + import validator + chat flow all use it).
  - No file/PDF storage: an admit card is a STRUCTURED record with
    text fields (centre, session, reporting time) plus subjects/instructions
    stored as JSON arrays of strings in Text columns. There is intentionally
    no attachment column — the assigned card is the canonical record.
  - Duplicate policy (enforced app-side — no unique constraint on the table):
        A card is identified by
        (student, semester, exam_type, academic_year, exam_session).
        The import REJECTS a file that is internally duplicated, or whose
        valid rows collide with cards already present. Nothing is written
        until the whole file validates.
  - Privacy: student-facing DTOs are explicit allowlists (never the ORM);
    no student_id, reg_no, internal ids, credentials or tokens.
"""

from __future__ import annotations

import io
import json
import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Student, StudentAdmitCard

# The explicit semester allowlist (shared by the API, import validator &
# chat flow). A value missing here is treated as "not available".
_ALLOWLIST: frozenset[int] = settings.valid_student_semesters


# --------------------------------------------------------------------------- #
# Import validation
# --------------------------------------------------------------------------- #
class ImportDataError(ValueError):
    """Structured import failure. `duplicate_only` lets the route choose 409
    (duplicate collisions) vs 422 (validation) without mirroring the logic."""

    def __init__(self, errors: list[dict], duplicate_only: bool) -> None:
        self.errors = errors
        self.duplicate_only = duplicate_only
        super().__init__(self.summary)

    @property
    def summary(self) -> str:
        n = len(self.errors)
        head = "duplicate row(s)" if self.duplicate_only else "invalid row(s)"
        sample = self.errors[0]["message"] if self.errors else ""
        return f"{n} {head} blocked the import. Fix them and try again. First issue: {sample}"


# Canonical field -> accepted spreadsheet column spellings (matched case-insensitively).
_HEADER_ALIASES: dict[str, tuple[str, ...]] = {
    "registration_number": ("registration number", "registration", "reg number", "reg no", "regno", "reg_no"),
    "semester": ("semester", "sem", "sem no"),
    "exam_type": ("exam type", "examination type", "exam"),
    "exam_session": ("exam session", "examination session", "session"),
    "academic_year": ("academic year", "academic session", "session", "year"),
    "centre_name": ("centre name", "center name", "centre", "center", "exam centre", "exam center"),
    "centre_code": ("centre code", "center code", "centre", "center"),
    "centre_address": ("centre address", "center address", "address", "venue"),
    "reporting_time": ("reporting time", "report time", "time"),
    "subjects": ("subjects", "subject list", "papers"),
    "instructions": ("instructions", "instructions notes", "notes"),
    "issued_date": ("issued date", "issue date", "date of issue", "date"),
}

_REQUIRED_COLUMNS = ("registration_number", "semester", "centre_name")


def _norm_header(value: Any) -> str:
    s = str(value or "").strip().lower()
    return " ".join(s.split())


def _parse_headers(header_row: list[Any]) -> tuple[dict[str, int], list[str]]:
    """Map spreadsheet columns to canonical fields.

    Returns (column_map, missing_required). A column that matches multiple
    canonical fields is reported via the mapping resolution order.
    """
    mapping: dict[str, int] = {}
    for idx, raw in enumerate(header_row):
        key = _norm_header(raw)
        if not key:
            continue
        for canonical, aliases in _HEADER_ALIASES.items():
            if key == canonical or key in aliases:
                if canonical not in mapping:  # first occurrence wins
                    mapping[canonical] = idx
                break
    missing = [c for c in _REQUIRED_COLUMNS if c not in mapping]
    return mapping, missing


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).strip()


def _format_error(filename: str, size: int) -> None:
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if ext not in settings.student_results_import_extensions:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported import format '.{ext}'. Allowed: {', '.join(settings.student_results_import_extensions)}",
        )
    max_bytes = settings.STUDENT_RESULTS_IMPORT_MAX_MB * 1024 * 1024
    if size > max_bytes:
        raise HTTPException(
            status_code=413,
            detail=f"Import file too large ({size} bytes). Max {settings.STUDENT_RESULTS_IMPORT_MAX_MB} MB.",
        )


def _read_rows(ext: str, content: bytes) -> tuple[list[str], list[list[Any]]]:
    """Return (header cells, data-row cells) for a csv/xlsx blob."""
    if ext == "csv":
        import csv

        text = content.decode("utf-8-sig", errors="replace")
        reader = csv.reader(io.StringIO(text))
        rows = [r for r in reader if any(str(c).strip() for c in r)]
        if not rows:
            return [], []
        return rows[0], rows[1:]
    from openpyxl import load_workbook

    wb = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
    try:
        ws = wb[wb.sheetnames[0]] if wb.sheetnames else None
        if ws is None:
            return [], []
        matrix = [list(r) for r in ws.iter_rows(values_only=True)]
    finally:
        wb.close()
    matrix = [r for r in matrix if any(str(c).strip() for c in r)]
    if not matrix:
        return [], []
    return matrix[0], matrix[1:]


def parse_import_file(filename: str, content: bytes) -> dict[str, Any]:
    """Parse + shape a spreadsheet into canonical raw rows (no validation yet)."""
    _format_error(filename, len(content))
    ext = filename.rsplit(".", 1)[-1].lower()
    headers, data_rows = _read_rows(ext, content)
    if not headers:
        return {"filename": filename, "format": ext, "errors": [{"row": 0, "message": "The file is empty or has no header row."}], "rows": []}

    column_map, missing = _parse_headers(headers)
    recognized = {canonical: headers[idx] for canonical, idx in column_map.items()}
    if missing:
        return {
            "filename": filename,
            "format": ext,
            "errors": [{"row": 0, "message": f"Missing required column(s): {', '.join(missing)}."}],
            "recognized_columns": recognized,
            "rows": [],
        }

    raw_rows: list[dict[str, Any]] = []
    for row in data_rows:
        record: dict[str, Any] = {}
        for canonical, idx in column_map.items():
            record[canonical] = _cell(row[idx] if idx < len(row) else "")
        raw_rows.append(record)
    return {
        "filename": filename,
        "format": ext,
        "recognized_columns": recognized,
        "rows": raw_rows,
    }


# --------------------------------------------------------------------------- #
# subjects / instructions: JSON arrays of strings (demo + imports share this)
# --------------------------------------------------------------------------- #
def _json_list(value: str | None) -> list[str]:
    """Parse the Text column into a list of strings.

    Stored data is a JSON array of strings (as the demo seeder writes it).
    Tolerates legacy plain-text payloads (one item per line / comma-separated).
    """
    if not value:
        return []
    s = value.strip()
    if not s:
        return []
    try:
        data = json.loads(s)
        if isinstance(data, list):
            return [str(x).strip() for x in data if str(x).strip()]
        return [str(data)]
    except Exception:
        items = [x.strip() for x in s.replace(";", "\n").replace(",", "\n").splitlines() if x.strip()]
        return items or [s]


def _json_normalize(value: str) -> str:
    """Canonical JSON-array string for storage from a raw spreadsheet cell."""
    return json.dumps(_json_list(value))


def _parse_int(value: str, label: str) -> tuple[int | None, str | None]:
    s = _cell(value)
    if not s:
        return None, f"{label} is required"
    try:
        n = float(s)
    except ValueError:
        return None, f"{label} must be a whole number, got '{s}'"
    if not n.is_integer():
        return None, f"{label} must be a whole number, got '{s}'"
    if n < 1:
        return None, f"{label} must be a positive whole number"
    return int(n), None


def _duplicate_key(item: dict) -> tuple[str, int, str, str, str]:
    return (
        str(item["student_id"]),
        item["semester"],
        item.get("exam_type") or "Regular",
        item.get("academic_year") or "",
        item.get("exam_session") or "",
    )


def _is_duplicate_message(message: str) -> bool:
    m = message.lower()
    return "duplicate" in m or "already" in m


def _resolve_student(db: Session, reg: str) -> Student | None:
    return (
        db.query(Student)
        .filter(func.upper(Student.reg_no) == reg.upper())
        .first()
    )


def analyze_rows(db: Session, raw_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate raw canonical rows. Read-only — never writes.

    Returns:
      valid_items  : resolved, typed row dicts (student_id resolved server-side)
      errors       : [{"row": int, "message": str, "dup": bool}, ...]
      valid_count / error_count
    """
    allowlist = _ALLOWLIST
    seen: dict[tuple, int] = {}
    items: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []

    for idx, raw in enumerate(raw_rows, start=1):
        errs: list[str] = []

        reg = _cell(raw.get("registration_number"))
        student = None
        if not reg:
            errs.append("Registration number is required")
        else:
            student = _resolve_student(db, reg)
            if student is None:
                errs.append(f"Unknown registration number '{reg}'")

        semester: int | None = None
        sem_raw = _cell(raw.get("semester"))
        semester, e = _parse_int(sem_raw, "Semester")
        if e:
            errs.append(e)
        elif semester not in allowlist:
            errs.append(f"Semester {semester} is not in the allowed semester list")

        centre_name = _cell(raw.get("centre_name"))
        if not centre_name:
            errs.append("Centre name is required")

        exam_type = _cell(raw.get("exam_type"))[:50] or "Regular"
        exam_session = _cell(raw.get("exam_session"))[:100]
        academic_year = _cell(raw.get("academic_year"))[:20]
        centre_code = _cell(raw.get("centre_code"))[:20]
        centre_address = _cell(raw.get("centre_address"))
        reporting_time = _cell(raw.get("reporting_time"))[:50]
        subjects = _json_normalize(_cell(raw.get("subjects")))
        instructions = _json_normalize(_cell(raw.get("instructions")))
        issued_date = _cell(raw.get("issued_date"))[:20]

        item: dict[str, Any] = {
            "row": idx,
            "registration_number": student.reg_no if student else (reg or ""),
            "student_id": str(student.id) if student else "",
            "semester": semester,
            "exam_type": exam_type,
            "exam_session": exam_session,
            "academic_year": academic_year,
            "centre_name": centre_name,
            "centre_code": centre_code,
            "centre_address": centre_address,
            "reporting_time": reporting_time,
            "subjects": subjects,
            "instructions": instructions,
            "issued_date": issued_date,
        }

        if not errs and student is not None and semester is not None:
            key = _duplicate_key(item)
            if key in seen:
                errs.append(f"Duplicate row in the file for the same examinee/semester (first seen on line {seen[key]})")
            else:
                seen[key] = idx

        if errs:
            msg = "; ".join(errs)
            errors.append({"row": idx, "message": msg, "dup": _is_duplicate_message(msg)})
            item["ok"] = False
            item["errors"] = errs
            items.append(item)
            continue

        item["ok"] = True
        item["errors"] = []
        items.append(item)

    # Collision check against already-published cards (exact identity match).
    for item in items:
        if not item["ok"]:
            continue
        sid = uuid.UUID(item["student_id"])
        match = (
            db.query(StudentAdmitCard)
            .filter(
                StudentAdmitCard.student_id == sid,
                StudentAdmitCard.semester == item["semester"],
                func.coalesce(StudentAdmitCard.academic_year, "") == (item.get("academic_year") or ""),
                func.coalesce(StudentAdmitCard.exam_type, "Regular") == (item.get("exam_type") or "Regular"),
                func.coalesce(StudentAdmitCard.exam_session, "") == (item.get("exam_session") or ""),
            )
            .first()
        )
        if match is not None:
            message = "An admit card already exists for this student and semester — duplicate import blocked"
            errors.append({"row": item["row"], "message": message, "dup": True})
            item["ok"] = False
            item["errors"] = [message]

    valid_items = [i for i in items if i["ok"]]
    return {
        "valid_items": valid_items,
        "errors": errors,
        "valid_count": len(valid_items),
        "error_count": len(errors),
    }


def _build_admit_card_record(item: dict) -> StudentAdmitCard:
    return StudentAdmitCard(
        id=uuid.uuid4(),
        student_id=uuid.UUID(item["student_id"]),
        semester=item["semester"],
        exam_type=item["exam_type"] or "Regular",
        exam_session=item["exam_session"] or None,
        centre_name=item["centre_name"],
        centre_code=item["centre_code"] or None,
        centre_address=item["centre_address"] or None,
        reporting_time=item["reporting_time"] or None,
        subjects=item["subjects"],
        instructions=item["instructions"],
        issued_date=item["issued_date"] or None,
        academic_year=item["academic_year"] or None,
    )


def preview_import_file(db: Session, filename: str, content: bytes) -> dict[str, Any]:
    """Parsed + validated preview. Writes NOTHING.

    Returns header/parse info, per-row validation errors, the resolved valid
    rows for display, and `raw_rows` (all canonical rows) that the client must
    echo back on confirm — confirm re-validates the raw data server-side.
    """
    parsed = parse_import_file(filename, content)
    parsed["valid_count"] = 0
    parsed["error_count"] = len(parsed.get("errors") or [])
    parsed["total_rows"] = len(parsed.get("rows") or [])
    parsed["raw_rows"] = parsed.get("rows") or []
    if parsed.get("errors"):
        return parsed
    report = analyze_rows(db, parsed["raw_rows"])
    parsed["rows"] = report["valid_items"]
    parsed["valid_count"] = report["valid_count"]
    parsed["error_count"] = report["error_count"]
    parsed["errors"] = report["errors"]
    return parsed


def apply_import(db: Session, rows: list[dict[str, Any]]) -> int:
    """Validate (fresh) + apply import rows in ONE transaction.

    Any row failure aborts the whole batch — a partial import is impossible.
    """
    report = analyze_rows(db, rows)
    if report["errors"]:
        duplicate_only = all(e["dup"] for e in report["errors"])
        raise ImportDataError(report["errors"], duplicate_only)
    records = [_build_admit_card_record(i) for i in report["valid_items"]]
    try:
        db.add_all(records)
        db.commit()
    except Exception:
        db.rollback()
        raise
    for record in records:
        db.refresh(record)
    return len(records)


# --------------------------------------------------------------------------- #
# Student-facing lookup (server-side identity only)
# --------------------------------------------------------------------------- #
def _student_dto(r: StudentAdmitCard) -> dict[str, Any]:
    return {
        "semester": r.semester,
        "exam_type": r.exam_type or "Regular",
        "exam_session": r.exam_session or "",
        "academic_year": r.academic_year or "",
        "centre_name": r.centre_name or "",
        "centre_code": r.centre_code or "",
        "centre_address": r.centre_address or "",
        "reporting_time": r.reporting_time or "",
        "subjects": _json_list(r.subjects),
        "instructions": _json_list(r.instructions),
        "issued_date": r.issued_date or "",
    }


def student_card_semesters(db: Session, student_id: str) -> list[dict[str, Any]]:
    """Available semester entries for a student (allowlist-filtered, PII-free).

    One entry per semester; when several exam groups exist for a semester the
    latest card (by academic year / issued date / creation) wins the picker.
    A semester outside the allowlist is never surfaced.
    """
    rows = (
        db.query(StudentAdmitCard)
        .filter(StudentAdmitCard.student_id == student_id)
        .order_by(
            StudentAdmitCard.semester.asc(),
            StudentAdmitCard.academic_year.desc(),
            StudentAdmitCard.issued_date.desc(),
            StudentAdmitCard.created_at.desc(),
        )
        .all()
    )
    best: dict[int, StudentAdmitCard] = {}
    for r in rows:
        if r.semester not in _ALLOWLIST:
            continue
        if r.semester not in best:
            best[r.semester] = r
    return [
        {
            "semester": s,
            "exam_type": r.exam_type or "Regular",
            "exam_session": r.exam_session or "",
            "academic_year": r.academic_year or "",
        }
        for s, r in sorted(best.items())
    ]


def student_card_payload(db: Session, student_id: str, semester: int) -> dict[str, Any] | None:
    """The latest admit card for `semester` (None when not available).

    The semester must be in the explicit allowlist; anything else is treated
    as "not available" rather than an error so callers can show a safe message.
    """
    if semester not in _ALLOWLIST:
        return None
    row = (
        db.query(StudentAdmitCard)
        .filter(StudentAdmitCard.student_id == student_id, StudentAdmitCard.semester == semester)
        .order_by(
            StudentAdmitCard.academic_year.desc(),
            StudentAdmitCard.issued_date.desc(),
            StudentAdmitCard.created_at.desc(),
        )
        .first()
    )
    if row is None:
        return None
    return _student_dto(row)


# --------------------------------------------------------------------------- #
# Super-Admin management
# --------------------------------------------------------------------------- #
def _admin_dto(r: StudentAdmitCard) -> dict[str, Any]:
    dto = _student_dto(r)
    dto.update(
        {
            "id": str(r.id),
            "reg_no": r.student.reg_no if r.student else "",
            "roll_no": r.student.roll_no if r.student else None,
            "name": r.student.name if r.student else "",
        }
    )
    return dto


def list_admit_cards(
    db: Session,
    q: str | None = None,
    semester: int | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    query = db.query(StudentAdmitCard)
    if q:
        like = f"%{q.strip()}%"
        query = query.join(Student, Student.id == StudentAdmitCard.student_id).filter(
            or_(func.upper(Student.reg_no).like(like.upper()), Student.name.ilike(like))
        )
    if semester is not None:
        if semester not in _ALLOWLIST:
            raise ValueError("Invalid semester selection.")
        query = query.filter(StudentAdmitCard.semester == semester)
    total = query.count()
    rows = (
        query.order_by(StudentAdmitCard.semester, StudentAdmitCard.student_id, StudentAdmitCard.academic_year)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "admit_cards": [_admin_dto(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def _card_or_404(db: Session, card_id: str) -> StudentAdmitCard:
    try:
        uid = uuid.UUID(str(card_id))
    except (ValueError, AttributeError):
        raise ValueError("Admit card not found")
    card = db.get(StudentAdmitCard, uid)
    if card is None:
        raise ValueError("Admit card not found")
    return card


def _card_fields(data: dict[str, Any], card: StudentAdmitCard, semester_changed: bool) -> None:
    """Apply the explicit field allowlist to `card` (or validate before create).

    `data` carries the FULL field set on create (schema defaults filled in) and
    only the provided keys on update (exclude_unset), so `present ⇒ set`, and
    update never overwrites/validates a field the caller did not send.
    """
    for field, length in (
        ("exam_type", 50),
        ("exam_session", 100),
        ("academic_year", 20),
        ("centre_code", 20),
        ("reporting_time", 50),
        ("issued_date", 20),
    ):
        if field not in data:
            continue
        value = (data.get(field) or "")
        if not isinstance(value, str):
            value = str(value)
        setattr(card, field, value.strip()[:length] or None)
    if "centre_name" in data:
        centre_name = data.get("centre_name") or ""
        if not isinstance(centre_name, str):
            centre_name = str(centre_name)
        centre_name = centre_name.strip()[:200]
        if not centre_name:
            raise ValueError("Centre name is required")
        card.centre_name = centre_name
    if "centre_address" in data:
        centre_address = (data.get("centre_address") or "")
        if not isinstance(centre_address, str):
            centre_address = str(centre_address)
        card.centre_address = centre_address.strip() or None
    if "subjects" in data:
        card.subjects = json.dumps([str(x).strip() for x in (data.get("subjects") or []) if str(x).strip()])
    if "instructions" in data:
        card.instructions = json.dumps([str(x).strip() for x in (data.get("instructions") or []) if str(x).strip()])
    if semester_changed:
        semester = data.get("semester")
        if semester is None:
            raise ValueError("Semester is required")
        if semester not in _ALLOWLIST:
            raise ValueError("Invalid semester selection.")
        card.semester = semester


def _duplicate_exists(db: Session, key: tuple, exclude_id: uuid.UUID | None = None) -> bool:
    student_id, semester, exam_type, academic_year, exam_session = key
    query = db.query(StudentAdmitCard).filter(
        StudentAdmitCard.student_id == student_id,
        StudentAdmitCard.semester == semester,
        func.coalesce(StudentAdmitCard.academic_year, "") == academic_year,
        func.coalesce(StudentAdmitCard.exam_type, "Regular") == exam_type,
        func.coalesce(StudentAdmitCard.exam_session, "") == exam_session,
    )
    if exclude_id is not None:
        query = query.filter(StudentAdmitCard.id != exclude_id)
    return query.first() is not None


def create_card(db: Session, body: Any) -> dict[str, Any]:
    reg = (body.reg_no or "").strip()
    if not reg:
        raise ValueError("Registration number is required")
    student = _resolve_student(db, reg)
    if student is None:
        raise ValueError(f"Student with registration number '{reg}' not found")

    card = StudentAdmitCard(id=uuid.uuid4(), student_id=student.id, semester=1)
    _card_fields(body.model_dump(), card, semester_changed=True)

    key = _duplicate_key({
        "student_id": str(student.id),
        "semester": card.semester,
        "exam_type": card.exam_type or "Regular",
        "academic_year": card.academic_year or "",
        "exam_session": card.exam_session or "",
    })
    if _duplicate_exists(db, key):
        raise ValueError("An admit card already exists for this student, semester and exam")

    db.add(card)
    db.commit()
    db.refresh(card)
    return _admin_dto(card)


def update_card(db: Session, card_id: str, body: Any) -> dict[str, Any]:
    card = _card_or_404(db, card_id)
    changes = body.model_dump(exclude_unset=True)
    semester_changed = "semester" in changes
    original = (
        str(card.student_id),
        card.semester,
        card.exam_type or "Regular",
        card.academic_year or "",
        card.exam_session or "",
    )
    _card_fields(changes, card, semester_changed)
    new_key = (
        str(card.student_id),
        card.semester,
        card.exam_type or "Regular",
        card.academic_year or "",
        card.exam_session or "",
    )
    if new_key != original and _duplicate_exists(db, new_key, exclude_id=card.id):
        db.rollback()  # drop the uncommitted field mutation
        raise ValueError("An admit card already exists for this student, semester and exam")
    db.commit()
    db.refresh(card)
    return _admin_dto(card)


def delete_card(db: Session, card_id: str) -> dict[str, Any]:
    card = _card_or_404(db, card_id)
    dto = _admin_dto(card)
    db.delete(card)
    db.commit()
    return dto