"""
backend/app/student_exam_form/service.py

Data layer for Student Exam Form (Phase D).

Design notes:
  - Server-side identity: every student lookup derives student_id from the
    resolved StudentSession (dict from app.student.session.resolve_session).
    No client-controlled identifier ever influences a WHERE clause → IDOR-safe.
  - A form is a STRUCTURED record (the existing StudentExamForm model). It is
    a HYBRID workflow:
        * The student FILLS a new form (Regular/Backlog → semester → subjects
          → submit). Filling creates a StudentExamForm in form_status Pending;
          submission is the deterministic Pending → Submitted transition that
          also stamps submission_date.
          - The student owns exactly ONE open (Pending/Submitted) form per
            (semester, exam_type, academic_year) identity — duplicates rejected.
        * PRINT is a print-friendly representation of the student's own form.
          No PDF subsystem — a normal HTML render (frontend) is sufficient.
        * Super Admin PROVISION / manage / import forms and own the
          administrative fields (fee_status, fee_amount, transaction_id,
          submission_date, and lifecycle states Approved/Rejected/Withdrawn).
  - Payment/fee fields are Super-Admin-controlled data. Students can never
    express or mutate fee_status / transaction_id / fee_amount / submission_date.
  - form_status allowlist (server-only): Pending, Submitted, Approved,
    Rejected, Withdrawn. Only Super Admins set these. Students only perform the
    Pending → Submitted transition via /submit (affirm).
  - Semester allowlist: _ALLOWLIST is the single source of truth.
  - Duplicate policy (app-side, no unique constraint on the table):
        A form is identified by (student_id, semester, exam_type, academic_year).
        Import rejects in-file duplicates and DB collisions atomically.
  - Privacy: student DTO is an explicit allowlist (never the ORM): id,
    semester, exam_type, academic_year, subjects, form_status, submission_date,
    fee_status, fee_amount. No reg_no / student_id / transaction_id / internal
    ids / credentials are exposed to the student. The student DTO intentionally
    OMITS transaction_id — a payment reference is Super-Admin-only data.
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
from app.models import Student, StudentExamForm

# The explicit semester allowlist (shared by the API, import validator & chat
# flow). A value missing here is treated as "not available".
_ALLOWLIST: frozenset[int] = settings.valid_student_semesters

# Canonical exam types (existing model default "Regular"; Backlog is the
# other supported flow). Backend allowlist — arbitrary strings are rejected.
EXAM_TYPES: frozenset[str] = frozenset({"Regular", "Backlog"})

# Canonical form lifecycle states. Students only ever drive Pending → Submitted
# (server-side); every other transition is Super-Admin only.
FORM_STATUSES: frozenset[str] = frozenset(
    {"Pending", "Submitted", "Approved", "Rejected", "Withdrawn"}
)

# The single acceptable student-driven transition: Pending → Submitted.
_SUBMITABLE_STATUSES: frozenset[str] = frozenset({"Pending"})


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
    "registration_number": ("registration number", "registration", "reg number", "reg no", "regno", "reg_no", "student registration"),
    "semester": ("semester", "sem", "sem no"),
    "exam_type": ("exam type", "examination type", "exam", "regular/backlog", "type"),
    "academic_year": ("academic year", "academic session", "session", "year"),
    "subjects": ("subjects", "subject list", "papers", "subject names"),
    "form_status": ("form status", "status", "state"),
    "fee_status": ("fee status", "payment status", "fee"),
    "fee_amount": ("fee amount", "amount", "fees"),
    "transaction_id": ("transaction id", "transaction", "txn id", "txn"),
    "submission_date": ("submission date", "submitted on", "date of submission"),
}

_REQUIRED_COLUMNS = ("registration_number", "semester", "exam_type")


def _norm_header(value: Any) -> str:
    s = str(value or "").strip().lower()
    return " ".join(s.split())


def _parse_headers(header_row: list[Any]) -> tuple[dict[str, int], list[str]]:
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


def _json_list(value: str | None) -> list[str]:
    """Parse the Text column into a list of strings (JSON array, tolerant)."""
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
    return json.dumps(_json_list(value))


def _parse_int(value: str, label: str, allow_zero: bool = False) -> tuple[int | None, str | None]:
    s = _cell(value)
    if not s:
        return None, None
    try:
        n = float(s)
    except ValueError:
        return None, f"{label} must be a whole number, got '{s}'"
    if not n.is_integer():
        return None, f"{label} must be a whole number, got '{s}'"
    if n < 0 or (n == 0 and not allow_zero):
        return None, f"{label} must be a positive whole number"
    return int(n), None


def _is_duplicate_message(message: str) -> bool:
    m = message.lower()
    return "duplicate" in m or "already" in m


def _duplicate_key(item: dict) -> tuple[str, int, str, str]:
    return (
        str(item["student_id"]),
        item["semester"],
        item.get("exam_type") or "Regular",
        item.get("academic_year") or "",
    )


def _resolve_student(db: Session, reg: str) -> Student | None:
    return (
        db.query(Student)
        .filter(func.upper(Student.reg_no) == reg.upper())
        .first()
    )


def analyze_rows(db: Session, raw_rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate raw canonical rows. Read-only — never writes."""
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
        if not sem_raw:
            errs.append("Semester is required")
        else:
            try:
                sem_f = float(sem_raw)
                if sem_f.is_integer():
                    semester = int(sem_f)
                else:
                    errs.append(f"Semester must be a whole number, got '{sem_raw}'")
            except ValueError:
                errs.append(f"Semester must be a whole number, got '{sem_raw}'")
            if semester is not None and semester not in allowlist:
                errs.append(f"Semester {semester} is not in the allowed semester list")

        exam_type = _cell(raw.get("exam_type")) or "Regular"
        if exam_type not in EXAM_TYPES:
            errs.append(f"Exam type must be one of {', '.join(sorted(EXAM_TYPES))}, got '{exam_type}'")

        fee_amount, e = _parse_int(raw.get("fee_amount"), "Fee amount")
        if e:
            errs.append(e)

        academic_year = _cell(raw.get("academic_year"))[:20]
        form_status = _cell(raw.get("form_status")) or "Pending"
        if form_status not in FORM_STATUSES:
            errs.append(f"Form status must be one of {', '.join(sorted(FORM_STATUSES))}, got '{form_status}'")
        fee_status = _cell(raw.get("fee_status")) or "Unpaid"
        transaction_id = _cell(raw.get("transaction_id"))[:100]
        submission_date = _cell(raw.get("submission_date"))[:20]
        subjects = _json_normalize(_cell(raw.get("subjects")))

        item: dict[str, Any] = {
            "row": idx,
            "registration_number": student.reg_no if student else (reg or ""),
            "student_id": str(student.id) if student else "",
            "semester": semester,
            "exam_type": exam_type,
            "academic_year": academic_year,
            "subjects": subjects,
            "form_status": form_status,
            "fee_status": fee_status,
            "fee_amount": fee_amount,
            "transaction_id": transaction_id,
            "submission_date": submission_date,
        }

        if not errs and student is not None and semester is not None:
            key = _duplicate_key(item)
            if key in seen:
                errs.append(f"Duplicate row in the file for the same student/semester/exam (first seen on line {seen[key]})")
            else:
                seen[key] = idx

        if errs:
            message = "; ".join(errs)
            errors.append({"row": idx, "message": message, "dup": _is_duplicate_message(message)})
            item["ok"] = False
            item["errors"] = errs
            items.append(item)
            continue

        item["ok"] = True
        item["errors"] = []
        items.append(item)

    # Collision check against already-present forms (exact identity match).
    for item in items:
        if not item["ok"]:
            continue
        sid = uuid.UUID(item["student_id"])
        match = (
            db.query(StudentExamForm)
            .filter(
                StudentExamForm.student_id == sid,
                StudentExamForm.semester == item["semester"],
                func.coalesce(StudentExamForm.exam_type, "Regular") == (item.get("exam_type") or "Regular"),
                func.coalesce(StudentExamForm.academic_year, "") == (item.get("academic_year") or ""),
            )
            .first()
        )
        if match is not None:
            message = "An exam form already exists for this student, semester and exam — duplicate import blocked"
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


def _build_form_record(item: dict) -> StudentExamForm:
    return StudentExamForm(
        id=uuid.uuid4(),
        student_id=uuid.UUID(item["student_id"]),
        semester=item["semester"],
        exam_type=item["exam_type"] or "Regular",
        form_status=item["form_status"] or "Pending",
        subjects=item["subjects"],
        fee_status=item["fee_status"] or "Unpaid",
        fee_amount=item["fee_amount"],
        transaction_id=item["transaction_id"] or None,
        submission_date=item["submission_date"] or None,
        academic_year=item["academic_year"] or None,
    )


def preview_import_file(db: Session, filename: str, content: bytes) -> dict[str, Any]:
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
    report = analyze_rows(db, rows)
    if report["errors"]:
        duplicate_only = all(e["dup"] for e in report["errors"])
        raise ImportDataError(report["errors"], duplicate_only)
    records = [_build_form_record(i) for i in report["valid_items"]]
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
# Student-facing (server-side identity only)
# --------------------------------------------------------------------------- #
def _student_dto(r: StudentExamForm) -> dict[str, Any]:
    return {
        "id": str(r.id),
        "semester": r.semester,
        "exam_type": r.exam_type or "Regular",
        "academic_year": r.academic_year or "",
        "subjects": _json_list(r.subjects),
        "form_status": r.form_status or "Pending",
        "submission_date": r.submission_date or "",
        "fee_status": r.fee_status or "Unpaid",
        "fee_amount": r.fee_amount,
    }


def student_form_semesters(db: Session, student_id: str) -> list[dict[str, Any]]:
    """Available (semester, exam_type) entries for a student, PII-free."""
    rows = (
        db.query(StudentExamForm)
        .filter(StudentExamForm.student_id == student_id)
        .order_by(StudentExamForm.semester, StudentExamForm.exam_type, StudentExamForm.academic_year)
        .all()
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        if r.semester not in _ALLOWLIST:
            continue
        out.append({
            "id": str(r.id),
            "semester": r.semester,
            "exam_type": r.exam_type or "Regular",
            "academic_year": r.academic_year or "",
            "form_status": r.form_status or "Pending",
        })
    return out


def student_form_by_identity(
    db: Session, student_id: str, semester: int, exam_type: str
) -> StudentExamForm | None:
    """Fetch the student's form matching (semester, exam_type). Identity is
    server-derived from the session — never from the client."""
    if semester not in _ALLOWLIST:
        return None
    if exam_type not in EXAM_TYPES:
        return None
    return (
        db.query(StudentExamForm)
        .filter(
            StudentExamForm.student_id == student_id,
            StudentExamForm.semester == semester,
            StudentExamForm.exam_type == exam_type,
        )
        .order_by(StudentExamForm.academic_year.desc(), StudentExamForm.created_at.desc())
        .first()
    )


def _duplicate_exists(db: Session, key: tuple, exclude_id: uuid.UUID | None = None) -> bool:
    student_id, semester, exam_type, academic_year = key
    query = db.query(StudentExamForm).filter(
        StudentExamForm.student_id == student_id,
        StudentExamForm.semester == semester,
        func.coalesce(StudentExamForm.exam_type, "Regular") == exam_type,
        func.coalesce(StudentExamForm.academic_year, "") == academic_year,
    )
    if exclude_id is not None:
        query = query.filter(StudentExamForm.id != exclude_id)
    return query.first() is not None


def student_fill(
    db: Session, student_id: str, data: dict[str, Any]
) -> dict[str, Any]:
    """Student Fill: create a new Pending exam form for their own identity."""
    semester = data.get("semester")
    exam_type = (data.get("exam_type") or "Regular").strip()
    if semester not in _ALLOWLIST:
        raise ValueError("The selected examination type is not available." if semester is None
                         else "Invalid semester selection.")
    if exam_type not in EXAM_TYPES:
        raise ValueError("The selected examination type is not available.")

    key = (str(student_id), semester, exam_type, data.get("academic_year") or "")
    if _duplicate_exists(db, key):
        raise ValueError("An exam form already exists for this student, semester and exam")

    form = StudentExamForm(
        id=uuid.uuid4(),
        student_id=uuid.UUID(student_id),
        semester=semester,
        exam_type=exam_type,
        form_status="Pending",
        subjects=json.dumps([str(x).strip() for x in (data.get("subjects") or []) if str(x).strip()]),
        fee_status="Unpaid",
        fee_amount=None,
        transaction_id=None,
        submission_date=None,
        academic_year=(data.get("academic_year") or "")[:20] or None,
    )
    db.add(form)
    db.commit()
    db.refresh(form)
    return _student_dto(form)


def student_submit(db: Session, student_id: str, form_id: str, confirm: bool) -> tuple[dict[str, Any], bool]:
    """Affirm the filled form. Server performs the allowed Pending → Submitted
    transition and stamps submission_date. `was_submitted` tells the route
    whether this call actually transitioned the state (audit only on change)."""
    form = _form_or_404(db, form_id)
    if str(form.student_id) != student_id:
        raise ValueError("You are not authorized to access this form.")
    if not confirm:
        raise ValueError("Form submission requires confirmation.")
    if form.form_status not in _SUBMITABLE_STATUSES:
        raise ValueError("Your Exam Form has already been submitted.")
    form.form_status = "Submitted"
    from datetime import datetime
    form.submission_date = datetime.now().strftime("%d-%b-%Y")
    db.commit()
    db.refresh(form)
    return _student_dto(form), True


def student_print_payload(db: Session, student_id: str, semester: int, exam_type: str) -> dict[str, Any] | None:
    """Print/view payload for the student's own form. Returns None (safe) when
    no form matches; never leaks another student's data."""
    form = student_form_by_identity(db, student_id, semester, exam_type)
    if form is None:
        return None
    return _student_dto(form)


# --------------------------------------------------------------------------- #
# Super-Admin management
# --------------------------------------------------------------------------- #
def _admin_dto(r: StudentExamForm) -> dict[str, Any]:
    dto = {
        "id": str(r.id),
        "semester": r.semester,
        "exam_type": r.exam_type or "Regular",
        "academic_year": r.academic_year or "",
        "subjects": _json_list(r.subjects),
        "form_status": r.form_status or "Pending",
        "fee_status": r.fee_status or "Unpaid",
        "fee_amount": r.fee_amount,
        "transaction_id": r.transaction_id or "",
        "submission_date": r.submission_date or "",
    }
    dto.update(
        {
            "reg_no": r.student.reg_no if r.student else "",
            "roll_no": r.student.roll_no if r.student else None,
            "name": r.student.name if r.student else "",
        }
    )
    return dto


def list_exam_forms(
    db: Session,
    q: str | None = None,
    semester: int | None = None,
    exam_type: str | None = None,
    form_status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    query = db.query(StudentExamForm)
    if q:
        like = f"%{q.strip()}%"
        query = query.join(Student, Student.id == StudentExamForm.student_id).filter(
            or_(func.upper(Student.reg_no).like(like.upper()), Student.name.ilike(like))
        )
    if semester is not None:
        if semester not in _ALLOWLIST:
            raise ValueError("Invalid semester selection.")
        query = query.filter(StudentExamForm.semester == semester)
    if exam_type:
        if exam_type not in EXAM_TYPES:
            raise ValueError("Invalid exam type selection.")
        query = query.filter(StudentExamForm.exam_type == exam_type)
    if form_status:
        if form_status not in FORM_STATUSES:
            raise ValueError("Invalid form status selection.")
        query = query.filter(StudentExamForm.form_status == form_status)
    total = query.count()
    rows = (
        query.order_by(StudentExamForm.semester, StudentExamForm.student_id, StudentExamForm.created_at)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "exam_forms": [_admin_dto(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def _form_or_404(db: Session, form_id: str) -> StudentExamForm:
    try:
        uid = uuid.UUID(str(form_id))
    except (ValueError, AttributeError):
        raise ValueError("Exam form not found")
    form = db.get(StudentExamForm, uid)
    if form is None:
        raise ValueError("Exam form not found")
    return form


# Public aliases used by routes (print endpoint / DTO serialization).
form_or_404 = _form_or_404
student_dto = _student_dto


def _apply_admin_fields(data: dict[str, Any], form: StudentExamForm, semester_changed: bool) -> None:
    for field, length in (
        ("academic_year", 20),
        ("transaction_id", 100),
        ("submission_date", 20),
    ):
        if field not in data:
            continue
        value = data.get(field) or ""
        if not isinstance(value, str):
            value = str(value)
        setattr(form, field, value.strip()[:length] or None)

    for field in ("form_status", "fee_status"):
        if field not in data:
            continue
        value = (data.get(field) or "").strip()
        if not value:
            raise ValueError(f"{field.replace('_', ' ').title()} is required")
        if field == "form_status" and value not in FORM_STATUSES:
            raise ValueError(f"Invalid form status: {value}")
        setattr(form, field, value[:50])

    if "fee_amount" in data:
        value = data.get("fee_amount")
        if value is not None:
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise ValueError("Fee amount must be a whole number")
            if value < 0:
                raise ValueError("Fee amount cannot be negative")
            form.fee_amount = value
        else:
            form.fee_amount = None

    if "subjects" in data:
        form.subjects = json.dumps([str(x).strip() for x in (data.get("subjects") or []) if str(x).strip()])

    if semester_changed:
        semester = data.get("semester")
        if semester is None:
            raise ValueError("Semester is required")
        if semester not in _ALLOWLIST:
            raise ValueError("Invalid semester selection.")
        form.semester = semester
        if "exam_type" in data:
            exam_type = (data.get("exam_type") or "").strip()
            if exam_type not in EXAM_TYPES:
                raise ValueError("Invalid exam type selection.")
            form.exam_type = exam_type


def create_exam_form(db: Session, body: Any) -> dict[str, Any]:
    reg = (body.reg_no or "").strip()
    if not reg:
        raise ValueError("Registration number is required")
    student = _resolve_student(db, reg)
    if student is None:
        raise ValueError(f"Student with registration number '{reg}' not found")

    data = body.model_dump()
    semester = data["semester"]
    exam_type = (data.get("exam_type") or "Regular").strip()
    if semester not in _ALLOWLIST:
        raise ValueError("Invalid semester selection.")
    if exam_type not in EXAM_TYPES:
        raise ValueError("Invalid exam type selection.")
    if body.form_status not in FORM_STATUSES:
        raise ValueError("Invalid form status.")

    form = StudentExamForm(id=uuid.uuid4(), student_id=student.id, semester=semester, exam_type=exam_type,
                           form_status="Pending")
    _apply_admin_fields(data, form, semester_changed=True)

    key = _duplicate_key({
        "student_id": str(student.id),
        "semester": form.semester,
        "exam_type": form.exam_type or "Regular",
        "academic_year": form.academic_year or "",
    })
    if _duplicate_exists(db, key):
        raise ValueError("An exam form already exists for this student, semester and exam")

    db.add(form)
    db.commit()
    db.refresh(form)
    return _admin_dto(form)


def update_exam_form(db: Session, form_id: str, body: Any) -> dict[str, Any]:
    form = _form_or_404(db, form_id)
    changes = body.model_dump(exclude_unset=True)
    semester_changed = "semester" in changes
    if "exam_type" in changes:
        exam_type = (changes.get("exam_type") or "").strip()
        if exam_type not in EXAM_TYPES:
            raise ValueError("Invalid exam type selection.")
        changes["exam_type"] = exam_type
    original = (str(form.student_id), form.semester, form.exam_type or "Regular", form.academic_year or "")
    _apply_admin_fields(changes, form, semester_changed)
    new_key = (str(form.student_id), form.semester, form.exam_type or "Regular", form.academic_year or "")
    if new_key != original and _duplicate_exists(db, new_key, exclude_id=form.id):
        db.rollback()
        raise ValueError("An exam form already exists for this student, semester and exam")
    db.commit()
    db.refresh(form)
    return _admin_dto(form)


def set_exam_form_status(db: Session, form_id: str, form_status: str) -> dict[str, Any]:
    form = _form_or_404(db, form_id)
    if form_status not in FORM_STATUSES:
        raise ValueError(f"Invalid form status: {form_status}")
    form.form_status = form_status
    db.commit()
    db.refresh(form)
    return _admin_dto(form)


def delete_exam_form(db: Session, form_id: str) -> dict[str, Any]:
    form = _form_or_404(db, form_id)
    dto = _admin_dto(form)
    db.delete(form)
    db.commit()
    return dto
