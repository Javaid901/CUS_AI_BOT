"""
backend/app/student_exam_form/exam_session.py

Exam Session data layer (super-admin provisioning + student availability).

An ExamSession is the container an exam form is filled against: it carries the
programme/batch/semester/exam-type identity, the application window, the fee
(base + late) and a per-session `form_seq` counter that backs server-generated
form numbers (e.g. "EXMPG26-3-00539"). Only Super Admins mutate sessions;
students only READ the sessions that are OPEN AND match their own profile.

Security contract:
  - Session identity is never client-controlled in a WHERE clause that matters:
    student-facing lookups filter by the session cookie's student identity and
    derived programme/batch/semester — never by a client-supplied session id.
  - form_seq increments happen server-side inside the fill transaction; the
    generated form_no is structurally unique within a session.
"""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import HTTPException
from sqlalchemy import func
from sqlalchemy.orm import Session

from app.config import settings
from app.models import ExamSession, StudentExamForm

# Canonical session lifecycle (Title-case, consistent with forms/status casing).
SESSION_STATUSES: frozenset[str] = frozenset({"Draft", "Open", "Closed", "Archived"})

# Form number pattern: code + dash + 5-digit sequence.
_FORM_NO_WIDTH = 5

# Semester allowlist shares the canonical student list.
_ALLOWLIST: frozenset[int] = settings.valid_student_semesters


class SessionError(ValueError):
    """Structured exam-session failure (routes map to HTTP statuses)."""


def _value_error(message: str) -> SessionError:
    return SessionError(message)


def session_dto(s: ExamSession, counts: bool = False) -> dict[str, Any]:
    out: dict[str, Any] = {
        "id": str(s.id),
        "name": s.name,
        "code": s.code,
        "programme": s.programme,
        "batch": s.batch or "",
        "semester": s.semester,
        "exam_type": s.exam_type or "Regular",
        "academic_year": s.academic_year or "",
        "application_open_at": s.application_open_at.isoformat() if s.application_open_at else None,
        "last_date_normal": s.last_date_normal.isoformat() if s.last_date_normal else None,
        "last_date_late": s.last_date_late.isoformat() if s.last_date_late else None,
        "base_fee": s.base_fee,
        "late_fee": s.late_fee,
        "status": s.status or "Draft",
        "form_seq": s.form_seq or 0,
        "created_at": s.created_at.isoformat() if s.created_at else None,
        "updated_at": s.updated_at.isoformat() if s.updated_at else None,
    }
    if counts:
        out["application_count"] = (s.form_seq or 0)
    return out


def _session_or_404(db: Session, session_id: str) -> ExamSession:
    try:
        uid = uuid.UUID(str(session_id))
    except (ValueError, AttributeError):
        raise _value_error("Exam session not found")
    session = db.get(ExamSession, uid)
    if session is None:
        raise _value_error("Exam session not found")
    return session


def _normalise_code(value: str) -> str:
    code = "".join(str(value).strip().upper().split())
    if not code or len(code) > 40 or not code.replace("-", "").replace("_", "").isalnum():
        raise _value_error("Exam session code must be alphanumeric (letters, digits, - or _), max 40 chars")
    return code


def _check_semester(semester: int) -> None:
    if semester not in _ALLOWLIST:
        raise _value_error(f"Invalid semester selection (allowed: {', '.join(str(s) for s in sorted(_ALLOWLIST))})")


def _check_programme(programme: str) -> str:
    programme = (programme or "").strip().lower()
    if not programme:
        raise _value_error("Programme is required")
    return programme


def _resolve_datetime(value: Any, label: str) -> object | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo
        try:
            return _dt.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            from datetime import datetime
            for fmt in ("%Y-%m-%d", "%Y-%m-%dT%H:%M", "%d-%b-%Y"):
                try:
                    return datetime.strptime(value.strip(), fmt)
                except ValueError:
                    continue
            raise _value_error(f"'{label}' must be a valid date/time")
    return value


def _validate_window(session: ExamSession) -> None:
    if session.application_open_at and session.last_date_normal:
        if session.last_date_normal < session.application_open_at:
            raise _value_error("Last date (normal) must be on or after the application open date")
    if session.application_open_at and session.last_date_late:
        if session.last_date_late < session.application_open_at:
            raise _value_error("Last date (late) must be on or after the application open date")


# --------------------------------------------------------------------------- #
# Lookup / listing
# --------------------------------------------------------------------------- #
def get_session(db: Session, session_id: str) -> ExamSession:
    return _session_or_404(db, session_id)


def list_sessions(
    db: Session,
    q: str | None = None,
    programme: str | None = None,
    semester: int | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict[str, Any]:
    query = db.query(ExamSession)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(
            func.upper(ExamSession.name).like(like.upper())
            | func.upper(ExamSession.code).like(like.upper())
        )
    if programme:
        query = query.filter(ExamSession.programme == str(programme).strip().lower())
    if semester is not None:
        _check_semester(semester)
        query = query.filter(ExamSession.semester == semester)
    if status:
        if status not in SESSION_STATUSES:
            raise _value_error(f"Invalid session status: {status}")
        query = query.filter(ExamSession.status == status)
    total = query.count()
    rows = (
        query.order_by(ExamSession.semester, ExamSession.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "sessions": [session_dto(s, counts=True) for s in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def code_exists(db: Session, code: str, exclude_id: uuid.UUID | None = None) -> bool:
    query = db.query(ExamSession).filter(func.upper(ExamSession.code) == code.upper())
    if exclude_id is not None:
        query = query.filter(ExamSession.id != exclude_id)
    return query.first() is not None


# --------------------------------------------------------------------------- #
# Super-Admin mutations
# --------------------------------------------------------------------------- #
def create_session(db: Session, data: dict[str, Any]) -> dict[str, Any]:
    code = _normalise_code(data.get("code") or "")
    if code_exists(db, code):
        raise _value_error(f"An exam session with code '{code}' already exists")
    programme = _check_programme(data.get("programme"))
    semester = data.get("semester")
    if semester is None:
        raise _value_error("Semester is required")
    _check_semester(semester)
    status = (data.get("status") or "Draft").strip()
    if status not in SESSION_STATUSES:
        raise _value_error(f"Invalid session status: {status}")
    base_fee = int(data.get("base_fee") or 0)
    late_fee = int(data.get("late_fee") or 0)
    if base_fee < 0 or late_fee < 0:
        raise _value_error("Fees cannot be negative")

    session = ExamSession(
        id=uuid.uuid4(),
        name=(data.get("name") or "").strip()[:200],
        code=code,
        programme=programme,
        batch=(data.get("batch") or "").strip()[:30] or None,
        semester=semester,
        exam_type=((data.get("exam_type") or "Regular").strip()[:50]) or "Regular",
        academic_year=(data.get("academic_year") or "").strip()[:20] or None,
        application_open_at=_resolve_datetime(data.get("application_open_at"), "application_open_at"),
        last_date_normal=_resolve_datetime(data.get("last_date_normal"), "last_date_normal"),
        last_date_late=_resolve_datetime(data.get("last_date_late"), "last_date_late"),
        base_fee=base_fee,
        late_fee=late_fee,
        status=status,
        form_seq=0,
        created_by=_uuid_or_none(data.get("created_by")),
    )
    _validate_window(session)
    db.add(session)
    db.commit()
    db.refresh(session)
    return session_dto(session, counts=True)


def update_session(db: Session, session_id: str, data: dict[str, Any]) -> dict[str, Any]:
    session = _session_or_404(db, session_id)
    if "code" in data:
        code = _normalise_code(data["code"])
        if code.upper() != (session.code or "").upper():
            if session.form_seq and (session.form_seq or 0) > 0:
                raise _value_error("The session code cannot change once exam forms have been numbered for it")
            if code_exists(db, code, exclude_id=session.id):
                raise _value_error(f"An exam session with code '{code}' already exists")
            session.code = code
    if "name" in data:
        session.name = str(data["name"]).strip()[:200]
    if "programme" in data:
        session.programme = _check_programme(data["programme"])
    if "batch" in data:
        session.batch = str(data.get("batch") or "").strip()[:30] or None
    if "semester" in data:
        _check_semester(int(data["semester"]))
        session.semester = int(data["semester"])
    if "exam_type" in data:
        session.exam_type = (str(data.get("exam_type") or "Regular").strip()[:50]) or "Regular"
    if "academic_year" in data:
        session.academic_year = str(data.get("academic_year") or "").strip()[:20] or None
    if "application_open_at" in data:
        session.application_open_at = _resolve_datetime(data.get("application_open_at"), "application_open_at")
    if "last_date_normal" in data:
        session.last_date_normal = _resolve_datetime(data.get("last_date_normal"), "last_date_normal")
    if "last_date_late" in data:
        session.last_date_late = _resolve_datetime(data.get("last_date_late"), "last_date_late")
    if "base_fee" in data and data.get("base_fee") is not None:
        base_fee = int(data["base_fee"])
        if base_fee < 0:
            raise _value_error("Base fee cannot be negative")
        session.base_fee = base_fee
    if "late_fee" in data and data.get("late_fee") is not None:
        late_fee = int(data["late_fee"])
        if late_fee < 0:
            raise _value_error("Late fee cannot be negative")
        session.late_fee = late_fee
    if "status" in data:
        status = str(data["status"]).strip()
        if status not in SESSION_STATUSES:
            raise _value_error(f"Invalid session status: {status}")
        session.status = status
    _validate_window(session)
    db.commit()
    db.refresh(session)
    return session_dto(session, counts=True)


def set_session_status(db: Session, session_id: str, status: str) -> dict[str, Any]:
    session = _session_or_404(db, session_id)
    if status not in SESSION_STATUSES:
        raise _value_error(f"Invalid session status: {status}")
    session.status = status
    db.commit()
    db.refresh(session)
    return session_dto(session, counts=True)


def delete_session(db: Session, session_id: str) -> dict[str, Any]:
    session = _session_or_404(db, session_id)
    linked = (
        db.query(StudentExamForm)
        .filter(StudentExamForm.exam_session_id == session.id)
        .count()
    )
    if linked:
        raise _value_error("This exam session already has exam forms — archive it instead of deleting")
    dto = session_dto(session, counts=True)
    db.delete(session)
    db.commit()
    return dto


# --------------------------------------------------------------------------- #
# Student-facing availability + form number allocation
# --------------------------------------------------------------------------- #
def _uuid_or_none(value: Any) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        return None


def student_available_sessions(
    db: Session, student_id: str, programme: str | None = None,
    batch: str | None = None, semester: int | None = None,
) -> list[dict[str, Any]]:
    """OPEN sessions that match the student's own profile.

    Programme is the hard filter; semester matches the student's current
    semester (when known); a session batch, when set, must match the student's.
    Students never see another programme's sessions.
    """
    from app.models import Student

    student = db.get(Student, uuid.UUID(str(student_id)))
    if student is None:
        return []
    prog = (programme or student.programme or "").strip().lower()
    sem = semester if semester is not None else student.current_semester
    stu_batch = (batch or student.batch or "").strip()

    query = db.query(ExamSession).filter(ExamSession.status == "Open")
    if prog:
        query = query.filter(ExamSession.programme == prog)
    if sem is not None and sem in _ALLOWLIST:
        query = query.filter(ExamSession.semester == sem)
    rows = query.order_by(ExamSession.semester, ExamSession.created_at.desc()).all()
    out: list[dict[str, Any]] = []
    for s in rows:
        if s.batch and stu_batch and s.batch != stu_batch:
            continue
        out.append(session_dto(s))
    return out


def _lock_session(db: Session, session_id: uuid.UUID) -> ExamSession:
    """Read the session row with a write lock where the DB supports it.

    SQLite serialises writers at the file level and SQLAlchemy emulates
    FOR UPDATE by no-op; Postgres uses a genuine row lock so two concurrent
    fills for the same session cannot both read the same form_seq.
    """
    try:
        return (
            db.query(ExamSession)
            .filter(ExamSession.id == session_id)
            .with_for_update()
            .one()
        )
    except Exception:
        session = db.get(ExamSession, session_id)
        if session is None:
            raise _value_error("Exam session not found")
        return session


def allocate_form_no(db: Session, session_id: uuid.UUID) -> str:
    """Server-generated, session-scoped form number (flushed, not committed).

    Reads the session under a write lock, increments the per-session sequence
    and returns e.g. "EXMPG26-3-00539". The caller commits as part of the form
    write so the number and the form are one atomic operation.
    """
    session = _lock_session(db, session_id)
    session.form_seq = (session.form_seq or 0) + 1
    form_no = f"{session.code}-{session.semester}-{session.form_seq:0{_FORM_NO_WIDTH}d}"
    db.flush()
    return form_no