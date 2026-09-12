"""
backend/app/student_results/service.py

Data layer for Student Results (Phase B).

Design notes:
  - Server-side identity: every student lookup path derives student_id from
    the resolved StudentSession (dict from app.student.session.resolve_session);
    no client-controlled identifier ever influences the WHERE clause.
  - Semester allowlist: _ALLOWLIST is the single source
    of truth (API + import validator + chat flow all use it).
  - Duplicate policy (documented + enforced app-side — the StudentResult
    table has NO unique constraint):
        A result row is identified by
        (student, semester, academic_year, exam_type, subject_code).
        subject_code is optional in the file — when blank, subject_name takes
        its place in the key.
        The import REJECTS a file that is internally duplicated, or whose
        valid rows collide with rows already present in the database. Nothing
        is written until the whole file validates. This is the safe,
        deterministic choice: an import can never silently overwrite or mix
        published data.
  - Privacy: student-facing DTOs are explicit allowlists (never the ORM);
    no student_id, reg_no, internal ids, credentials or tokens.
"""

from __future__ import annotations

import io
import re
import uuid
from html import escape as html_escape
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func, or_
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Student, StudentResult

# The explicit semester allowlist (shared by the API, import validator &
# chat flow). A value missing here is treated as "not available".
_ALLOWLIST: frozenset[int] = settings.valid_student_semesters

# Examination roll numbers are opaque groupings of a published attempt
# (letters, digits and hyphens are safe to echo back into student renders).
_ROLL_FULL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{1,49}$")


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
    "exam_roll_no": ("examination roll number", "exam roll number", "examination roll no", "exam roll no", "exam roll", "examination roll"),
    "semester": ("semester", "sem", "sem no"),
    "exam_type": ("exam type", "examination type", "exam"),
    "academic_year": ("academic year", "academic session", "session", "year"),
    "subject_code": ("subject code", "subject", "code"),
    "subject_name": ("subject name", "subject", "paper", "subject title"),
    "internal_marks": ("internal marks", "internal", "ia marks", "ia"),
    "external_marks": ("external marks", "external", "theory marks", "theory"),
    "total_marks": ("total marks", "total"),
    "max_marks": ("max marks", "maximum marks", "max"),
    "grade": ("grade", "grades"),
    "sgpa": ("sgpa", "sgpi"),
    "cgpa": ("cgpa", "cgpi"),
    "status": ("status", "result", "declared"),
}

_REQUIRED_COLUMNS = ("registration_number", "semester", "subject_name")


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


def _opt_int(value: Any, label: str) -> tuple[int | None, str | None]:
    s = _cell(value)
    if not s:
        return None, None
    try:
        n = float(s)
    except ValueError:
        return None, f"{label} must be a whole number, got '{s}'"
    if not n.is_integer():
        return None, f"{label} must be a whole number, got '{s}'"
    if n < 0:
        return None, f"{label} cannot be negative"
    return int(n), None


def _is_duplicate_message(message: str) -> bool:
    m = message.lower()
    return "duplicate" in m or "already published" in m


def _duplicate_key(item: dict) -> tuple[str, int, str, str, str]:
    return (
        str(item["student_id"]),
        item["semester"],
        item.get("academic_year") or "",
        item.get("exam_type") or "Regular",
        (item.get("subject_code") or "").strip() or (item.get("subject_name") or "").strip(),
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
            student = (
                db.query(Student)
                .filter(func.upper(Student.reg_no) == reg.upper())
                .first()
            )
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

        subject_name = _cell(raw.get("subject_name"))
        if not subject_name:
            errs.append("Subject name is required")

        internal_marks, e = _opt_int(raw.get("internal_marks"), "Internal marks")
        if e:
            errs.append(e)
        external_marks, e = _opt_int(raw.get("external_marks"), "External marks")
        if e:
            errs.append(e)
        total_marks, e = _opt_int(raw.get("total_marks"), "Total marks")
        if e:
            errs.append(e)
        max_marks, e = _opt_int(raw.get("max_marks"), "Max marks")
        if e:
            errs.append(e)
        max_marks = max_marks if max_marks is not None else 100
        if max_marks <= 0:
            errs.append("Max marks must be greater than zero")

        exam_type = _cell(raw.get("exam_type")) or "Regular"
        status = _cell(raw.get("status")).lower() or "pass"
        if status not in ("pass", "fail"):
            errs.append(f"Status must be 'pass' or 'fail', got '{status}'")

        academic_year = _cell(raw.get("academic_year"))[:20]
        subject_code = _cell(raw.get("subject_code"))[:20]
        grade = _cell(raw.get("grade"))[:5]
        sgpa = _cell(raw.get("sgpa"))[:5]
        cgpa = _cell(raw.get("cgpa"))[:5]

        exam_roll_no = _cell(raw.get("exam_roll_no"))[:50]
        if exam_roll_no and not _ROLL_FULL_RE.fullmatch(exam_roll_no):
            errs.append("Examination roll number is invalid (letters, digits and hyphens only)")

        item: dict[str, Any] = {
            "row": idx,
            "registration_number": student.reg_no if student else (reg or ""),
            "student_id": str(student.id) if student else "",
            "exam_roll_no": exam_roll_no or None,
            "semester": semester,
            "exam_type": exam_type,
            "academic_year": academic_year,
            "subject_code": subject_code,
            "subject_name": subject_name,
            "internal_marks": internal_marks,
            "external_marks": external_marks,
            "total_marks": total_marks,
            "max_marks": max_marks,
            "grade": grade,
            "sgpa": sgpa,
            "cgpa": cgpa,
            "status": status,
        }

        if not errs and student is not None and semester is not None:
            key = _duplicate_key(item)
            if key in seen:
                errs.append(f"Duplicate row in the file for the same subject (first seen on line {seen[key]})")
            else:
                seen[key] = idx

        if errs:
            errors.append({"row": idx, "message": "; ".join(errs), "dup": _is_duplicate_message("; ".join(errs))})
            item["errors"] = errs
            item["ok"] = False
            items.append(item)
            continue

        item["ok"] = True
        item["errors"] = []
        items.append(item)

    # Collision check against already-published rows (precise subject match:
    # subject_code when the file supplies it, else subject_name).
    for item in items:
        if not item["ok"]:
            continue
        sid = uuid.UUID(item["student_id"])
        key = _duplicate_key(item)
        candidates = (
            db.query(StudentResult)
            .filter(
                StudentResult.student_id == sid,
                StudentResult.semester == item["semester"],
                func.coalesce(StudentResult.academic_year, "") == (item.get("academic_year") or ""),
                func.coalesce(StudentResult.exam_type, "Regular") == (item.get("exam_type") or "Regular"),
            )
            .all()
        )
        match = False
        for existing_row in candidates:
            existing_subj = (existing_row.subject_code or "").strip()
            if not existing_subj:
                existing_subj = (existing_row.subject_name or "").strip()
            if existing_subj == key[4]:
                match = True
                break
        if match:
            message = "This subject is already published for the same semester — duplicate import blocked"
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


def _build_result_record(item: dict) -> StudentResult:
    return StudentResult(
        student_id=uuid.UUID(item["student_id"]),
        exam_roll_no=item["exam_roll_no"],
        semester=item["semester"],
        exam_type=item["exam_type"] or "Regular",
        subject_name=item["subject_name"],
        subject_code=item["subject_code"] or None,
        internal_marks=item["internal_marks"],
        external_marks=item["external_marks"],
        total_marks=item["total_marks"],
        max_marks=item["max_marks"] or 100,
        grade=item["grade"] or None,
        sgpa=item["sgpa"] or None,
        cgpa=item["cgpa"] or None,
        status=item["status"] or "pass",
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
    records = [_build_result_record(i) for i in report["valid_items"]]
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
def student_semesters(
    db: Session,
    student_id: str,
    current_semester: int | None = None,
) -> list[dict[str, Any]]:
    """Available semesters for a student (grouped, PII-free).

    The list is constrained by the AUTHORITATIVE current semester already
    stored on the Student record: no semester after `current_semester` is ever
    offered, regardless of what rows exist. `current_semester = None` yields
    an empty list (no verified profile means nothing to look up).
    """
    if current_semester is None:
        return []
    rows = (
        db.query(StudentResult)
        .filter(
            StudentResult.student_id == student_id,
            StudentResult.semester <= current_semester,
        )
        .order_by(StudentResult.semester, StudentResult.academic_year, StudentResult.exam_type)
        .all()
    )
    groups: dict[tuple, list[StudentResult]] = {}
    for r in rows:
        key = (r.semester, r.academic_year or "", r.exam_type or "Regular")
        groups.setdefault(key, []).append(r)
    out = []
    for (semester, academic_year, exam_type), group in sorted(groups.items()):
        first = group[0]
        out.append({
            "semester": semester,
            "academic_year": academic_year or None,
            "exam_type": exam_type,
            "subject_count": len(group),
            "sgpa": first.sgpa,
            "cgpa": first.cgpa,
            "result": "pass" if all(r.status == "pass" for r in group) else "fail",
        })
    return out


def _subject_dto(r: StudentResult) -> dict[str, Any]:
    return {
        "semester": r.semester,
        "exam_type": r.exam_type,
        "academic_year": r.academic_year,
        "subject_code": r.subject_code,
        "subject_name": r.subject_name,
        "internal_marks": r.internal_marks,
        "external_marks": r.external_marks,
        "total_marks": r.total_marks,
        "max_marks": r.max_marks,
        "grade": r.grade,
        "status": r.status,
        "sgpa": r.sgpa,
        "cgpa": r.cgpa,
    }


def student_results_payload(db: Session, student_id: str, semester: int) -> dict[str, Any] | None:
    """One semester of results for a student (None when not available).

    The semester must be in the explicit allowlist; anything else is treated
    as "not available" rather than an error so callers can show a safe message.
    """
    if semester not in _ALLOWLIST:
        return None
    rows = (
        db.query(StudentResult)
        .filter(StudentResult.student_id == student_id, StudentResult.semester == semester)
        .order_by(StudentResult.subject_name)
        .all()
    )
    if not rows:
        return None
    subjects = [_subject_dto(r) for r in rows]
    first = rows[0]
    return {
        "semester": semester,
        "semester_summary": {
            "semester": semester,
            "academic_year": first.academic_year,
            "exam_type": first.exam_type or "Regular",
            "subject_count": len(subjects),
            "sgpa": first.sgpa,
            "cgpa": first.cgpa,
            "result": "pass" if all(r.status == "pass" for r in rows) else "fail",
        },
        "subjects": subjects,
    }


def student_result_view(
    db: Session,
    student_id: str,
    semester: int,
    exam_roll_no: str,
    current_semester: int | None = None,
) -> dict[str, Any] | None:
    """One published attempt for a student, scoped to (semester, roll number).

    This is the SINGLE lookup path for the Results flow. `student_id` always
    comes from the resolved StudentSession — the roll number is only an input
    that selects which of the student's OWN published attempts to show, so a
    wrong/foreign roll simply yields None (the safe "no result" message).
    Returns None when the attempt does not exist.
    """
    if semester not in _ALLOWLIST:
        return None
    if current_semester is not None and semester > current_semester:
        return None
    roll = (exam_roll_no or "").strip()
    if not roll or not _ROLL_FULL_RE.fullmatch(roll):
        return None
    rows = (
        db.query(StudentResult)
        .filter(
            StudentResult.student_id == student_id,
            StudentResult.semester == semester,
            func.upper(StudentResult.exam_roll_no) == roll.upper(),
        )
        .order_by(StudentResult.subject_name)
        .all()
    )
    if not rows:
        return None
    subjects = [_subject_dto(r) for r in rows]
    first = rows[0]
    student = first.student
    return {
        "semester": semester,
        "exam_roll_no": roll,
        "student_name": student.name if student else "",
        "reg_no": student.reg_no if student else "",
        "semester_summary": {
            "semester": semester,
            "academic_year": first.academic_year,
            "exam_type": first.exam_type or "Regular",
            "subject_count": len(subjects),
            "sgpa": first.sgpa,
            "cgpa": first.cgpa,
            "result": "pass" if all(r.status == "pass" for r in rows) else "fail",
        },
        "subjects": subjects,
    }


def render_result_print_html(result: dict[str, Any]) -> str:
    """Server-rendered, markup-safe result document for print / save-as-PDF.

    Every value is HTML-escaped. No internal IDs, session tokens or URL
    parameters are embedded (a saved PDF therefore contains no private URL).
    """
    s = result["semester_summary"]
    e = html_escape
    rows = "".join(
        f"""<tr>
          <td>{i}</td>
          <td>{e(r.get('subject_code') or '-')}</td>
          <td>{e(r.get('subject_name') or '')}</td>
          <td>{e(r.get('grade') or '-')}</td>
          <td>{e(str(r.get('sgpa') or '-'))}</td>
          <td>{e((r.get('status') or 'pass').upper())}</td>
         </tr>"""  # noqa: E501
        for i, r in enumerate(result["subjects"], 1)
    )
    outcome = "PASS" if s["result"] == "pass" else "FAIL"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>Result - Semester {e(str(result['semester']))}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  body {{ font-family: 'Segoe UI', Arial, sans-serif; color: #1b2530; margin: 0; background: #eef1f5; }}
  .page {{ max-width: 820px; margin: 18px auto; background: #fff; border-radius: 12px; padding: 34px 40px; box-shadow: 0 2px 14px rgba(0,0,0,.08); }}
  .head {{ text-align: center; border-bottom: 3px double #143a5c; padding-bottom: 12px; margin-bottom: 20px; }}
  .head .org {{ font-size: 15px; color: #143a5c; letter-spacing: 2px; margin: 0 0 4px; }}
  .head h1 {{ font-size: 22px; margin: 0; color: #0f2c49; }}
  .head .doc {{ font-size: 12px; color: #556070; margin-top: 4px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 14px; }}
  td, th {{ padding: 8px 10px; border: 1px solid #d5dbe3; text-align: left; }}
  th {{ background: #143a5c; color: #fff; font-weight: 600; }}
  .meta td:first-child {{ width: 30%; font-weight: 600; background: #f3f6fa; }}
  .marks {{ margin-top: 24px; }}
  .marks .num {{ text-align: center; width: 34px; }}
  .foot {{ margin-top: 26px; font-size: 11px; color: #7a8494; display: flex; justify-content: space-between; }}
</style></head><body>
<div class="page">
  <div class="head">
    <p class="org">CLUSTER UNIVERSITY SRINAGAR</p>
    <h1>Statement of Marks</h1>
    <div class="doc">Generated from the student self-service portal - not an official transcript.</div>
  </div>
  <table class="meta">
    <tr><td>Candidate Name</td><td>{e(result.get('student_name') or '')}</td></tr>
    <tr><td>Registration Number</td><td>{e(result.get('reg_no') or '')}</td></tr>
    <tr><td>Examination Roll Number</td><td>{e(result.get('exam_roll_no') or '')}</td></tr>
    <tr><td>Semester</td><td>{e(str(s['semester']))}</td></tr>
    <tr><td>Exam Type</td><td>{e(s['exam_type'])}</td></tr>
    <tr><td>Academic Year</td><td>{e(s.get('academic_year') or '-')}</td></tr>
    <tr><td>SGPA</td><td>{e(str(s.get('sgpa') or '-'))}</td></tr>
    <tr><td>CGPA</td><td>{e(str(s.get('cgpa') or '-'))}</td></tr>
    <tr><td>Overall Result</td><td><strong>{outcome}</strong></td></tr>
  </table>
  <table class="marks">
    <thead><tr><th class="num">#</th><th>Subject Code</th><th>Subject</th><th>Grade</th><th>SGPA</th><th>Result</th></tr></thead>
    <tbody>{rows}</tbody>
  </table>
  <div class="foot">
    <span>Save as PDF using your browser's print dialog.</span>
    <span>Demo instance - values are illustrative.</span>
  </div>
</div></body></html>"""  # noqa: E501
def _admin_dto(r: StudentResult) -> dict[str, Any]:
    dto = _subject_dto(r)
    dto.update(
        {
            "id": str(r.id),
            "reg_no": r.student.reg_no if r.student else "",
            "roll_no": r.student.roll_no if r.student else None,
            "exam_roll_no": r.exam_roll_no,
            "name": r.student.name if r.student else "",
        }
    )
    return dto


def delete_result(db: Session, result_id: str) -> dict[str, Any]:
    """Delete EXACTLY ONE result row (Super Admin only). Never a bulk delete.

    The row is resolved by its primary key so a malformed id or a missing row
    is a 404, and only that single subject/attempt record is removed — sibling
    semesters, other subjects and other students are never touched. Fails
    closed and rolls back on error; returns the row snapshot before deletion.
    """
    try:
        rid = uuid.UUID(str(result_id))
    except (ValueError, AttributeError):
        raise ValueError("Result not found")
    row = db.get(StudentResult, rid)
    if row is None:
        raise ValueError("Result not found")
    dto = _admin_dto(row)
    dto["student_id"] = str(row.student_id) if row.student_id else None
    try:
        db.delete(row)
        db.commit()
    except Exception:
        db.rollback()
        raise
    return dto


def list_results(
    db: Session,
    q: str | None = None,
    semester: int | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    query = db.query(StudentResult)
    if q:
        like = f"%{q.strip()}%"
        query = query.join(Student, Student.id == StudentResult.student_id).filter(
            or_(func.upper(Student.reg_no).like(like.upper()), Student.name.ilike(like))
        )
    if semester is not None:
        if semester not in _ALLOWLIST:
            raise ValueError("Invalid semester selection.")
        query = query.filter(StudentResult.semester == semester)
    total = query.count()
    rows = (
        query.order_by(StudentResult.semester, StudentResult.student_id, StudentResult.subject_name)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "results": [_admin_dto(r) for r in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }