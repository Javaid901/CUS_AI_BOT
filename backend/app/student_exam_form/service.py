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
from app.models import (
    ExamApplicationSubject,
    ExamEligibility,
    ExamPayment,
    ExamSession,
    Student,
    StudentExamForm,
)
from app.student_exam_form import exam_session as es
from app.student_exam_form.eligibility import (
    evaluate_eligibility,
    snapshot_rules,
    system_subjects_for,
)

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
    """Parse the Text column into a list of strings (JSON array, tolerant).

    Session-derived subject rows are stored as [{"subject_code", "subject_name"}]
    dicts so the server keeps codes; this renderer flattens them to names so
    the legacy string-list contract (student DTO / admin list) is unchanged.
    """
    if not value:
        return []
    s = value.strip()
    if not s:
        return []

    def _name(item: Any) -> str:
        if isinstance(item, dict):
            for key in ("subject_name", "name", "title"):
                if str(item.get(key) or "").strip():
                    return str(item[key]).strip()
            return str(item)
        return str(item).strip()

    try:
        data = json.loads(s)
        if isinstance(data, list):
            return [_name(x) for x in data if str(x).strip()]
        return [_name(data)]
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


def student_form_records(db: Session, student_id: str) -> list[dict[str, Any]]:
    """Numbered forms for the student's Print picker (own record, safe allowlist).

    Includes session-bound forms carrying a server-generated form_no — the
    student's own printed record, so form_no/session info is expected here.
    No fee references, no identity fields beyond the printed form number.
    """
    rows = (
        db.query(StudentExamForm)
        .filter(StudentExamForm.student_id == student_id)
        .order_by(StudentExamForm.semester, StudentExamForm.created_at.desc())
        .all()
    )
    out: list[dict[str, Any]] = []
    for r in rows:
        if r.semester not in _ALLOWLIST:
            continue
        session = None
        if r.exam_session_id:
            session = db.get(ExamSession, r.exam_session_id)
        out.append({
            "id": str(r.id),
            "form_no": r.form_no or "",
            "semester": r.semester,
            "exam_type": r.exam_type or "Regular",
            "academic_year": r.academic_year or "",
            "form_status": r.form_status or "Pending",
            "fee_status": r.fee_status or "Unpaid",
            "submission_date": r.submission_date or "",
            "session_code": session.code if session else "",
            "session_name": session.name if session else "",
        })
    return out


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
    """Student Fill: create a new Pending exam form for their own identity.

    Dispatches on `exam_session_id`. Session-driven fill is the primary path:
    the server derives programme/semester/exam_type/fee/subjects from the OPEN
    ExamSession and gates on the deterministic eligibility snapshot. Without a
    session id the legacy allow-list path is used, kept backward-compatible.
    """
    session_id = data.get("exam_session_id")
    if session_id:
        return _fill_by_session(db, student_id, session_id, bool(data.get("confirm", True)))

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


def _fill_by_session(db: Session, student_id: str, session_id: str, confirm: bool) -> dict[str, Any]:
    """Session-driven fill (Phase D2). Everything academic is derived server-side.

    Order of operations inside ONE transaction:
      1. session lock via allocate_form_no (atomically bumps form_seq)
      2. deterministic eligibility gate (snapshot persisted to exam_eligibility
         + the form's eligibility_snapshot column)
      3. subjects from the academic catalogue (exam_application_subjects rows +
         the form.subjects JSON for backward-compatible DTO rendering)
      4. fee from the session (base + late when past last_date_normal);
         amount 0 → auto-paid zero-amount system payment (still gated).
    """
    if not confirm:
        raise ValueError("Form submission requires confirmation.")

    student = db.get(Student, uuid.UUID(str(student_id)))
    if student is None:
        raise ValueError("Student profile not found")

    session = es.get_session(db, session_id)
    if (session.status or "Draft") != "Open":
        raise ValueError("This exam session is not open for applications.")

    # Duplicate: canonical (student, session) identity — plus a guard that a
    # student with an existing LEGACY (session-less) form for the same identity
    # cannot double-declare the same exam via the session path.
    if _duplicate_session_form(db, student.id, session.id):
        raise ValueError("An exam form already exists for this student and exam session")
    legacy_key = (str(student.id), session.semester, session.exam_type or "Regular", session.academic_year or "")
    if _legacy_duplicate_exists(db, legacy_key):
        raise ValueError("An exam form already exists for this student, semester and exam")

    eligibility = evaluate_eligibility(db, student, session)
    if not eligibility.get("eligible"):
        failed = [r["message"] for r in eligibility["rules"] if not r["passed"]]
        detail = " ".join(failed)
        raise ValueError(f"Your profile does not meet the eligibility criteria for this exam session. {detail}".strip())

    subjects = system_subjects_for(db, session, student)
    if not subjects:
        raise ValueError("No subjects are available for this exam session yet — please check again later.")

    subject_names = [s.get("subject_name", "") for s in subjects if s.get("subject_name")]
    amount, late_applied = _session_fee(db, session)

    form_no = es.allocate_form_no(db, session.id)
    fee_status = "Paid" if amount == 0 else "Unpaid"

    form = StudentExamForm(
        id=uuid.uuid4(),
        student_id=student.id,
        semester=session.semester,
        exam_type=session.exam_type or "Regular",
        form_status="Pending",
        subjects=json.dumps(subjects),
        fee_status=fee_status,
        fee_amount=amount,
        transaction_id=None,
        submission_date=None,
        academic_year=session.academic_year or None,
        exam_session_id=session.id,
        form_no=form_no,
        photo_path=None,
        eligibility_snapshot=snapshot_rules(eligibility),
    )
    db.add(form)
    db.flush()

    for subj in subjects:
        db.add(ExamApplicationSubject(
            id=uuid.uuid4(),
            form_id=form.id,
            subject_code=(subj.get("subject_code") or "")[:30] or None,
            subject_name=(subj.get("subject_name") or "")[:200],
            source="system",
        ))
    db.add(ExamEligibility(
        id=uuid.uuid4(),
        form_id=form.id,
        session_id=session.id,
        eligible=True,
        rules=snapshot_rules(eligibility),
    ))
    if amount == 0:
        # Zero-fee session: an immediately-successful system payment keeps the
        # "submittable only after a success payment" invariant uniform.
        from datetime import datetime as _dt

        now = _dt.utcnow()
        db.add(ExamPayment(
            id=uuid.uuid4(),
            form_id=form.id,
            session_id=session.id,
            amount=0,
            head="Exam Form Fee",
            status="success",
            gateway="mock",
            gateway_ref=f"ZERO-{form_no}",
            recorded_by="system",
            recorded_at=now,
            reconciled_at=now,
        ))

    db.commit()
    db.refresh(form)
    return _student_dto(form)


def _duplicate_session_form(db: Session, student_id: uuid.UUID, session_id: uuid.UUID) -> bool:
    return (
        db.query(StudentExamForm)
        .filter(StudentExamForm.student_id == student_id, StudentExamForm.exam_session_id == session_id)
        .first()
        is not None
    )


def _legacy_duplicate_exists(db: Session, key: tuple) -> bool:
    """Identity collision against legacy (session-less) forms only.

    A session-bound form is scoped by (student, session); a legacy form is
    scoped by (student, semester, exam_type, academic_year). The two never
    double-count: a student may hold several session forms for the same exam
    cycle but never a session form AND a legacy form for the same identity.
    """
    student_id, semester, exam_type, academic_year = key
    return (
        db.query(StudentExamForm)
        .filter(
            StudentExamForm.student_id == student_id,
            StudentExamForm.semester == semester,
            func.coalesce(StudentExamForm.exam_type, "Regular") == exam_type,
            func.coalesce(StudentExamForm.academic_year, "") == academic_year,
            StudentExamForm.exam_session_id.is_(None),
        )
        .first()
        is not None
    )


def _session_fee(db: Session, session: ExamSession) -> tuple[int, bool]:
    """Server-derived payable amount for the session.

    Late fee applies when the normal deadline has passed (and was set). Returns
    (amount, late_applied). Timezone differences between naive stored dates and
    aware "now" are normalized defensively.
    """
    from datetime import datetime, timezone

    def _ensure_utc(dt):
        if dt is None:
            return None
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)

    base = int(session.base_fee or 0)
    late = int(session.late_fee or 0)
    normal = _ensure_utc(session.last_date_normal)
    now = _ensure_utc(datetime.now())
    if normal is not None and late > 0 and now > normal:
        return base + late, True
    return base, False


def student_submit(db: Session, student_id: str, form_id: str, confirm: bool) -> tuple[dict[str, Any], bool]:
    """Affirm the filled form. Server performs the allowed Pending → Submitted
    transition and stamps submission_date. `was_submitted` tells the route
    whether this call actually transitioned the state (audit only on change).

    Session-driven forms are gated (Phase D2): the session must still be Open,
    the persisted eligibility snapshot must be eligible, and a payable fee must
    be Paid (a success payment existed server-side). Legacy forms keep the old
    un-gated behaviour.
    """
    form = _form_or_404(db, form_id)
    if str(form.student_id) != student_id:
        raise ValueError("You are not authorized to access this form.")
    if not confirm:
        raise ValueError("Form submission requires confirmation.")
    if form.form_status not in _SUBMITABLE_STATUSES:
        raise ValueError("Your Exam Form has already been submitted.")

    if form.exam_session_id:
        session = db.get(ExamSession, form.exam_session_id)
        if session is None:
            raise ValueError("Exam session not found")
        if (session.status or "Draft") != "Open":
            raise ValueError("The application window for this exam session has closed.")
        try:
            snapshot = json.loads(form.eligibility_snapshot or "{}")
        except Exception:
            snapshot = {}
        if snapshot.get("eligible") is not True:
            raise ValueError("Your Exam Form is not marked eligible by the university.")
        if (form.fee_amount or 0) > 0 and (form.fee_status or "Unpaid") != "Paid":
            raise ValueError("Your Exam Form is unpaid. Please pay the exam fee before submitting.")

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


def _json_subject_names(value: str | None) -> list[str]:
    """Names-only list for the printable document (dict-safe)."""
    return _json_list(value)


def student_form_document(db: Session, student_id: str, form_id: str) -> dict[str, Any] | None:
    """Printable DOCUMENT payload for the student's OWN exam form.

    This is the ONLY student-facing surface that renders form_no, identity
    fields and the payment reference — it is the student's own printed record
    (mirrors the physical admit-card document pattern). Returns None for
    anyone else's form. Never includes DOB, credentials or session tokens.
    """
    form = _form_or_404(db, form_id)
    if str(form.student_id) != student_id:
        return None
    student = form.student

    session = None
    if form.exam_session_id:
        session = db.get(ExamSession, form.exam_session_id)

    from app.student_exam_form.payment import payment_snapshot

    pay = payment_snapshot(db, form.id)
    base_fee = int(session.base_fee or 0) if session else None
    late_fee = int(session.late_fee or 0) if session else None
    from datetime import datetime

    now_iso = datetime.utcnow().isoformat() + "Z"

    return {
        "form_id": str(form.id),
        "form_no": form.form_no or "",
        "semester": form.semester,
        "exam_type": form.exam_type or "Regular",
        "academic_year": form.academic_year or "",
        "form_status": form.form_status or "Pending",
        "submission_date": form.submission_date or "",
        "subjects": _json_subject_names(form.subjects),
        "name": student.name if student else "",
        "reg_no": student.reg_no if student else "",
        "roll_no": student.roll_no or "" if student else "",
        "programme": (student.programme or "").upper() if student else "",
        "college": student.college or "" if student else "",
        "batch": student.batch or "" if student else "",
        "session_code": session.code if session else "",
        "session_name": session.name if session else "",
        "fee_normal": base_fee,
        "fee_late": late_fee,
        "fee_total": form.fee_amount,
        "fee_status": form.fee_status or "Unpaid",
        "transaction_id": pay.get("gateway_ref") if pay else (form.transaction_id or ""),
        "payment_date": pay.get("recorded_at") if pay else "",
        "photo_path": form.photo_path or "",
        "generated_at": now_iso,
    }


def student_payment_receipt(db: Session, student_id: str, form_id: str) -> dict[str, Any] | None:
    """Receipt payload for the student's OWN successful payment.

    Reuses the allow-listed exam-form document dict (name, reg no, form_no,
    exam info, fee, transaction ref, payment date) and enriches it with the
    server-owned payment identity (payment_id, gateway, payment_status). Only
    display values — never DOB, credentials or session tokens. Returns None for
    anyone else's form; the caller gates the "not yet paid" case via
    `fee_status` (server-owned).
    """
    data = student_form_document(db, student_id, form_id)
    if data is None:
        return None
    if (data.get("fee_status") or "Unpaid") != "Paid":
        return data
    from app.student_exam_form.payment import payment_snapshot

    pay = payment_snapshot(db, form_id)
    if pay:
        data["payment_id"] = pay.get("id") or ""
        data["payment_status"] = pay.get("status") or "success"
        data["gateway"] = pay.get("gateway") or "mock"
        data["payment_date_iso"] = pay.get("recorded_at") or data.get("payment_date")
    return data


def mark_form_printed(db: Session, student_id: str, form_id: str) -> None:
    """Stamp printed_at on the student's OWN form (print/download only)."""
    form = _form_or_404(db, form_id)
    if str(form.student_id) != student_id:
        return
    from datetime import datetime

    form.printed_at = datetime.utcnow()
    db.commit()


# --------------------------------------------------------------------------- #
# Super-Admin management
# --------------------------------------------------------------------------- #
def _admin_dto(r: StudentExamForm, db: Session | None = None) -> dict[str, Any]:
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
        "form_no": r.form_no or "",
        "exam_session_id": str(r.exam_session_id) if r.exam_session_id else "",
        "session_code": "",
        "printed_at": r.printed_at.isoformat() if r.printed_at else "",
    }
    if r.exam_session_id and db is not None:
        session = db.get(ExamSession, r.exam_session_id)
        if session is not None:
            dto["session_code"] = session.code or ""
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
        "exam_forms": [_admin_dto(r, db) for r in rows],
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
