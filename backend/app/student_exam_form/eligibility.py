"""
backend/app/student_exam_form/eligibility.py

Deterministic eligibility engine + system subject derivation for Exam Sessions.

Eligibility is a pure server-side function of known records — NO LLM, NO client
input. Every rule reports `passed` plus the evidence message so students see WHY
they are (or are not) eligible and Super Admins get an auditable snapshot.

Rule semantics:
  - Missing evidence never blocks: a rule with no data is reported as
    "not_verified" and counts as passed (deterministic and safe for students
    whose records are not yet published).
  - Failures are chainable: the student sees every failed rule at once.

Subjects derive from the academic catalogue (ProgrammeSubject rows for the
student's programme + the session's semester). When the catalogue has no rows
for a programme/semester, the last resort is the student's OWN published
StudentResult rows for that semester (still server-derived, never client input).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from statistics import mean
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ExamSession, Student, StudentAttendance, StudentResult

# Acceptable rule outcome values reported to students/admins.
PASS = "passed"
FAIL = "failed"
NOT_VERIFIED = "not_verified"


def _catalogue_programme_id(db: Session, student: Student) -> uuid.UUID | None:
    """Resolve the student's programme code to a catalogue Programme.id.

    Programme codes are stored uppercase in the catalogue (e.g. "BCA") while
    student records store lowercase codes (e.g. "bca") — compare case-insensitively.
    """
    prog_code = (student.programme or "").strip()
    if not prog_code:
        return None
    from app.catalogue.models import Programme

    row = (
        db.query(Programme.id)
        .filter(func.lower(Programme.code) == prog_code.lower())
        .first()
    )
    return row[0] if row else None


def system_subjects_for(db: Session, session: ExamSession, student: Student) -> list[dict[str, Any]]:
    """Server-derived subject list for the session (catalogue-first).

    Returns [{"subject_code", "subject_name"}] — object columns are never read
    nor trusted. Falls back to the student's own published results for the
    semester when the catalogue has no rows for the programme/semester.
    """
    programme_id = _catalogue_programme_id(db, student)
    if programme_id is not None:
        from app.catalogue.service import get_semester_subjects

        rows = get_semester_subjects(
            programme_id=str(programme_id),
            semester=session.semester,
            db=db,
        )
        subjects = [
            {
                "subject_code": str(r.get("subject_code") or "") or None,
                "subject_name": str(r.get("subject_name") or "").strip(),
            }
            for r in rows
            if str(r.get("subject_name") or "").strip()
        ]
        if subjects:
            return subjects

    # Fallback: the student's OWN published results for the same semester.
    results = (
        db.query(StudentResult.subject_name, StudentResult.subject_code)
        .filter(StudentResult.student_id == student.id, StudentResult.semester == session.semester)
        .order_by(StudentResult.subject_name)
        .all()
    )
    seen: set[str] = set()
    fallback: list[dict[str, Any]] = []
    for name, code in results:
        name = (name or "").strip()
        if not name or name.lower() in seen:
            continue
        seen.add(name.lower())
        fallback.append({
            "subject_code": str(code or "") or None,
            "subject_name": name,
        })
    return fallback


def _app_window_now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_utc(value: datetime | None) -> datetime | None:
    """Normalize stored window dates to aware UTC (naive values = UTC)."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _inside_window(session: ExamSession) -> bool:
    now = _app_window_now()
    if session.application_open_at:
        if now < _ensure_utc(session.application_open_at):
            return False
    deadline = session.last_date_late or session.last_date_normal
    if deadline:
        if now > _ensure_utc(deadline):
            return False
    return True


def _rule(passed: bool, code: str, message: str, outcome: str | None = None) -> dict[str, Any]:
    return {
        "rule": code,
        "outcome": outcome or (PASS if passed else FAIL),
        "passed": passed,
        "message": message,
    }


def _internals_proportion(db: Session, student_id: uuid.UUID, semester: int) -> float | None:
    rows = (
        db.query(StudentResult)
        .filter(StudentResult.student_id == student_id, StudentResult.semester == semester)
        .all()
    )
    if not rows:
        return None
    values = []
    for r in rows:
        if r.internal_marks is None:
            continue
        max_marks = r.max_marks or 100
        if max_marks <= 0:
            max_marks = 100
        values.append((float(r.internal_marks) / float(max_marks)) * 100.0)
    if not values:
        return None
    return mean(values)


def _attendance_proportion(db: Session, student_id: uuid.UUID, semester: int) -> float | None:
    rows = (
        db.query(StudentAttendance)
        .filter(StudentAttendance.student_id == student_id, StudentAttendance.semester == semester)
        .all()
    )
    if not rows:
        return None
    values = []
    for r in rows:
        if r.percentage is not None:
            try:
                values.append(float(str(r.percentage).strip().rstrip("%")))
            except (TypeError, ValueError):
                continue
        elif r.attended_classes is not None and r.total_classes:
            try:
                total = float(r.total_classes)
                if total > 0:
                    values.append((float(r.attended_classes) / total) * 100.0)
            except (TypeError, ValueError):
                continue
    if not values:
        return None
    return mean(values)


def evaluate_eligibility(db: Session, student: Student, session: ExamSession) -> dict[str, Any]:
    """Deterministic eligibility snapshot for (student, session).

    Returns {"eligible": bool, "rules": [...]}. Rows never touch the client
    model; every value is derived or allow-listed.
    """
    rules: list[dict[str, Any]] = []

    # 1. Programme profile.
    if (student.programme or "").strip().lower() == (session.programme or "").strip().lower():
        rules.append(_rule(True, "programme_match", f"Programme '{student.programme}' matches the exam session."))
    else:
        rules.append(_rule(False, "programme_match", f"Programme '{student.programme}' does not match this exam session."))

    # 2. Batch profile (when BOTH sides are known; missing batch is unverifiable).
    if session.batch and student.batch:
        if str(session.batch).strip() == str(student.batch).strip():
            rules.append(_rule(True, "batch_match", f"Batch '{student.batch}' matches the exam session."))
        else:
            rules.append(_rule(False, "batch_match", f"Batch '{student.batch}' does not match this exam session."))
    else:
        rules.append(_rule(True, "batch_match", "Batch match could not be verified (batch not set).", outcome=NOT_VERIFIED))

    # 3. Semester profile (when the student's current semester is known).
    if student.current_semester:
        if student.current_semester == session.semester:
            rules.append(_rule(True, "semester_eligible", f"Current semester {student.current_semester} matches the exam session semester."))
        else:
            rules.append(_rule(False, "semester_eligible", f"This exam session is for Semester {session.semester} but your current semester is {student.current_semester}."))
    else:
        rules.append(_rule(True, "semester_eligible", "Semester match could not be verified (current semester not set).", outcome=NOT_VERIFIED))

    # 4. Application window.
    if _inside_window(session):
        date_hint = "the application window is open"
        if session.application_open_at:
            date_hint += f" from {session.application_open_at.strftime('%d-%b-%Y')}"
        if session.last_date_late:
            date_hint += f" until {session.last_date_late.strftime('%d-%b-%Y')}"
        rules.append(_rule(True, "window_open", f"Applications are accepted: {date_hint}."))
    else:
        msg = "The application window for this exam session is currently closed."
        rules.append(_rule(False, "window_open", msg))

    # 5. Internals (only when evidence exists).
    internals = _internals_proportion(db, student.id, session.semester)
    min_internal = getattr(settings, "STUDENT_EXAM_MIN_INTERNAL_PERCENT", 40.0)
    if internals is None:
        rules.append(_rule(True, "internals_ok", "Internal marks for this semester are not published yet — no check applied.", outcome=NOT_VERIFIED))
    elif internals >= min_internal:
        rules.append(_rule(True, "internals_ok", f"Internal marks ({internals:.1f}%) meet the minimum requirement ({min_internal:.0f}%)."))
    else:
        rules.append(_rule(False, "internals_ok", f"Internal marks ({internals:.1f}%) are below the minimum requirement ({min_internal:.0f}%)."))

    # 6. Attendance (only when evidence exists).
    attendance = _attendance_proportion(db, student.id, session.semester)
    min_attendance = getattr(settings, "STUDENT_EXAM_MIN_ATTENDANCE_PERCENT", 75.0)
    if attendance is None:
        rules.append(_rule(True, "attendance_ok", "Attendance for this semester is not published yet — no check applied.", outcome=NOT_VERIFIED))
    elif attendance >= min_attendance:
        rules.append(_rule(True, "attendance_ok", f"Attendance ({attendance:.1f}%) meets the minimum requirement ({min_attendance:.0f}%)."))
    else:
        rules.append(_rule(False, "attendance_ok", f"Attendance ({attendance:.1f}%) is below the minimum requirement ({min_attendance:.0f}%)."))

    eligible = all(rule["passed"] for rule in rules)
    return {"eligible": eligible, "rules": rules}


def snapshot_rules(eligible: dict[str, Any]) -> str:
    """JSON string persisted on the form's eligibility_snapshot column."""
    import json

    return json.dumps(
        {
            "eligible": bool(eligible.get("eligible")),
            "rules": [
                {
                    "rule": r.get("rule"),
                    "outcome": r.get("outcome"),
                    "passed": bool(r.get("passed")),
                    "message": r.get("message"),
                }
                for r in (eligible.get("rules") or [])
            ],
            "evaluated_at": _app_window_now().isoformat(),
        }
    )