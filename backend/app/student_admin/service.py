"""
backend/app/student_admin/service.py

Data layer for Super Admin → Student Services → Students (Phase A).

All credential writes go through app/student/dob so that hashing and
verification always share one canonical DOB representation.

Transaction guarantees:
  - reset_dob_password: the hash replacement AND the revocation of every
    existing StudentSession commit in a single transaction — a student can
    never be left with a changed credential but live old sessions.
  - toggle_active: deactivation sets is_active=False + status="deactivated"
    AND revokes all sessions in one transaction.
  - `audit()` lives in the route layer (it uses its own session) so it cannot
    interfere with these atomic units.
"""

from __future__ import annotations

import uuid

from sqlalchemy import case, func, or_
from sqlalchemy.orm import Session

from app.models import (
    BacklogStatus,
    CourseRegistration,
    FeeReceipt,
    Grievance,
    HelpdeskTicket,
    MigrationCertificate,
    Revaluation,
    Student,
    StudentAdmitCard,
    StudentAttendance,
    StudentExamForm,
    StudentResult,
    StudentSession,
    StudentTranscript,
    XeroxRequest,
)
from app.student.dob import hash_dob, normalize_dob
from app.student_admin.schemas import StudentCreate, StudentUpdate


def _student_or_404(db: Session, student_id: str) -> Student:
    try:
        uid = uuid.UUID(str(student_id))
    except (ValueError, AttributeError):
        raise ValueError("Student not found")
    student = db.get(Student, uid)
    if student is None:
        raise ValueError("Student not found")
    return student


def _revoke_all_sessions(db: Session, student_id) -> None:
    db.query(StudentSession).filter(
        StudentSession.student_id == student_id,
        StudentSession.revoked == False,  # noqa: E712
    ).update({"revoked": True}, synchronize_session=False)


def delete_student(db: Session, student_id: str) -> dict:
    """Permanently delete a student and every linked record, in one transaction.

    The ORM declares ON DELETE CASCADE on every StudentService child table and
    ON DELETE SET NULL on Grievance.student_id, but the local SQLite engine
    does not enforce foreign keys (no `PRAGMA foreign_keys=ON`) and Student
    has no parent-side ORM cascade. To make SQLite behave identically to
    PostgreSQL (which honors the declarative clauses), the linked records are
    removed explicitly here:

      - deleted: sessions, results, admit cards, exam forms, fee receipts,
        attendance, transcripts, migration certificates, revaluations,
        xerox requests, backlogs, course registrations, helpdesk tickets
      - grievance: history is KEPT; student_id is set NULL (FK semantics)

    The student row is deleted last. The whole unit commits atomically and
    rolls back on any failure. Sessions (and their cookie-held tokens) are
    gone, so existing cookies fail immediately and the deleted student can
    never sign in again — no ghost sessions, no orphan rows.
    """
    student = _student_or_404(db, student_id)
    reg_no = student.reg_no

    cascade_children = [
        StudentResult,
        StudentAdmitCard,
        StudentExamForm,
        FeeReceipt,
        StudentAttendance,
        StudentTranscript,
        MigrationCertificate,
        Revaluation,
        XeroxRequest,
        BacklogStatus,
        CourseRegistration,
        HelpdeskTicket,
    ]
    try:
        for model in cascade_children:
            db.query(model).filter(model.student_id == student.id).delete(
                synchronize_session=False
            )
        db.query(StudentSession).filter(
            StudentSession.student_id == student.id
        ).delete(synchronize_session=False)
        db.query(Grievance).filter(Grievance.student_id == student.id).update(
            {"student_id": None}, synchronize_session=False
        )
        db.delete(student)
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {"id": str(student.id), "reg_no": reg_no}


# --------------------------------------------------------------------------- #
# DTO helpers (explicit allowlists — never the ORM, never hashed_password)
# --------------------------------------------------------------------------- #
def to_list_dto(s: Student) -> dict:
    return {
        "id": str(s.id),
        "reg_no": s.reg_no,
        "roll_no": s.roll_no,
        "name": s.name,
        "programme": s.programme,
        "current_semester": s.current_semester,
        "admission_year": s.admission_year,
        "college": s.college,
        "status": s.status,
        "is_active": s.is_active,
    }


def to_detail_dto(s: Student) -> dict:
    """Profile view for Super Admin.

    Deliberately excludes `dob` — a student's DOB is their password and is
    write-only (created/reset only). It is never returned to any client.
    """
    dto = to_list_dto(s)
    dto.update(
        {
            "father_name": s.father_name,
            "mother_name": s.mother_name,
            "gender": s.gender,
            "category": s.category,
            "email": s.email,
            "phone": s.phone,
            "academic_scheme": s.academic_scheme,
            "batch": s.batch,
            "address": s.address,
            "created_at": s.created_at.isoformat() if s.created_at else None,
        }
    )
    return dto


# --------------------------------------------------------------------------- #
# CRUD
# --------------------------------------------------------------------------- #
def create_student(db: Session, body: StudentCreate) -> dict:
    reg = (body.reg_no or "").strip().upper()
    if not reg:
        raise ValueError("Registration number is required")
    if len(reg) > 50:
        raise ValueError("Registration number is too long")
    # Canonicalise the DOB now so validation failures are 4xx, never silent.
    canonical = normalize_dob(body.dob)

    existing = (
        db.query(Student)
        .filter(func.upper(Student.reg_no) == reg)
        .first()
    )
    if existing:
        raise ValueError(f"Registration number {reg} is already registered")

    student = Student(
        reg_no=reg,
        roll_no=body.roll_no,
        name=(body.name or "").strip(),
        father_name=body.father_name,
        mother_name=body.mother_name,
        gender=body.gender,
        category=body.category,
        email=body.email,
        phone=body.phone,
        college=body.college,
        programme=(body.programme or "").strip(),
        academic_scheme=body.academic_scheme,
        current_semester=body.current_semester,
        admission_year=body.admission_year,
        batch=body.batch,
        address=body.address,
        status="active" if body.is_active else "deactivated",
        is_active=bool(body.is_active),
        hashed_password=hash_dob(canonical),
    )
    db.add(student)
    db.commit()
    db.refresh(student)
    return to_detail_dto(student)


def list_students(
    db: Session,
    q: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    query = db.query(Student)
    if q:
        like = f"%{q.strip()}%"
        query = query.filter(or_(Student.reg_no.ilike(like), Student.name.ilike(like)))
    if status == "active":
        query = query.filter(Student.is_active == True, Student.status == "active")  # noqa: E712
    elif status == "inactive":
        query = query.filter(or_(Student.is_active == False, Student.status != "active"))  # noqa: E712
    total = query.count()
    rows = (
        query.order_by(Student.reg_no)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )
    return {
        "students": [to_list_dto(s) for s in rows],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def _to_search_dto(s: Student, exam_roll_no: str | None, exam_roll_conflict: bool) -> dict:
    """Search result allowlist (Super Admin, READ-ONLY).

    Only the fields the Search feature may display are present. DOB,
    hashed_password, sessions and tokens are never queried for a search result.
    `roll_no` is the class/college roll and is never confused with the
    examination roll number.
    """
    return {
        "id": str(s.id),
        "name": s.name,
        "reg_no": s.reg_no,
        "roll_no": s.roll_no,
        "exam_roll_no": exam_roll_no,
        "exam_roll_conflict": exam_roll_conflict,
        "programme": s.programme,
        "current_semester": s.current_semester,
    }


def search_students(
    db: Session,
    q: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 20,
) -> dict:
    """Server-side Student Search (Super Admin, READ-ONLY).

    One search box matches three fields with deterministic, safe semantics:

      * name  -> case-insensitive substring (tolerant: "abid" finds
                 "Abid Ahmad", "Abid Hussain", ...)
      * reg_no -> case-insensitive substring (preserves the existing Student
                 Management list-search convention); an exact reg match ranks
                 first
      * roll_no (class/college roll) -> case-insensitive prefix, so a typed,
                 complete roll number matches exactly

    Ranking (SQL, before pagination): exact reg 0, exact roll 1, exact name 2,
    any partial match 3 — never an arbitrary "first result".

    DOB / hashed_password / student_id-derived secrets are NEVER queried or
    returned. The query runs against the live `students` table, so permanently
    deleted students cannot appear.

    Examination roll number comes ONLY from the existing
    StudentResult.exam_roll_no (the published examination-roll field) — it is
    never derived from roll_no/reg_no. Safety policy for multiple results:
    exactly one distinct non-null value -> returned; zero -> None; two or more
    distinct values -> None with exam_roll_conflict=True (inconsistency is
    reported, never silently resolved by picking one).

    `q` is never treated as a DOB lookup (out of scope by design).
    """
    empty = {"students": [], "total": 0, "page": page, "page_size": page_size}
    term = (q or "").strip()
    if not term:
        return empty

    query = db.query(Student)
    q_up = term.upper()
    rank = case(
        (func.upper(Student.reg_no) == q_up, 0),
        (func.upper(Student.roll_no) == q_up, 1),
        (func.upper(Student.name) == q_up, 2),
        else_=3,
    )
    query = query.filter(
        or_(
            Student.name.ilike(f"%{term}%"),
            func.upper(Student.reg_no).like(f"%{q_up}%"),
            func.upper(Student.roll_no).like(q_up + "%"),
        )
    )
    if status == "active":
        query = query.filter(Student.is_active == True, Student.status == "active")  # noqa: E712
    elif status == "inactive":
        query = query.filter(or_(Student.is_active == False, Student.status != "active"))  # noqa: E712

    total = query.count()
    rows = (
        query.order_by(rank, Student.reg_no)
        .offset((page - 1) * page_size)
        .limit(page_size)
        .all()
    )

    ids = [s.id for s in rows]
    exam_rolls_by_student: dict[str, set[str]] = {}
    if ids:
        for sid, roll in (
            db.query(StudentResult.student_id, StudentResult.exam_roll_no)
            .filter(
                StudentResult.student_id.in_(ids),
                StudentResult.exam_roll_no.isnot(None),
                StudentResult.exam_roll_no != "",
            )
            .all()
        ):
            exam_rolls_by_student.setdefault(str(sid), set()).add(str(roll).strip())

    students = []
    for s in rows:
        rolls = exam_rolls_by_student.get(str(s.id), set())
        exam_roll_no: str | None = None
        conflict = False
        if len(rolls) == 1:
            exam_roll_no = sorted(rolls)[0]
        elif len(rolls) > 1:
            conflict = True
        students.append(_to_search_dto(s, exam_roll_no, conflict))

    return {
        "students": students,
        "total": total,
        "page": page,
        "page_size": page_size,
    }


def get_student(db: Session, student_id: str) -> dict:
    return to_detail_dto(_student_or_404(db, student_id))


def update_student(db: Session, student_id: str, body: StudentUpdate) -> dict:
    student = _student_or_404(db, student_id)
    changes = body.model_dump(exclude_unset=True)

    # Wholly admin-side fields (optional at creation) are coerced: an empty
    # string means "clear it", None means "leave untouched".
    for field in (
        "roll_no",
        "father_name",
        "mother_name",
        "gender",
        "category",
        "email",
        "phone",
        "college",
        "academic_scheme",
        "batch",
        "address",
    ):
        if field not in changes:
            continue
        value = changes[field]
        if isinstance(value, str):
            value = value.strip() or None
        setattr(student, field, value)

    for field in ("name", "programme"):
        if field in changes:
            setattr(student, field, (changes[field] or "").strip())

    for field in ("current_semester", "admission_year"):
        if field in changes and changes[field] is not None:
            setattr(student, field, changes[field])

    db.commit()
    db.refresh(student)
    return to_detail_dto(student)


def reset_dob_password(db: Session, student_id: str, dob: str) -> dict:
    """Replace the DOB credential and revoke every existing session, atomically."""
    student = _student_or_404(db, student_id)
    canonical = normalize_dob(dob)
    student.hashed_password = hash_dob(canonical)
    _revoke_all_sessions(db, student.id)
    db.commit()
    db.refresh(student)
    return to_detail_dto(student)


def toggle_active(db: Session, student_id: str) -> dict:
    """Activate / deactivate. Deactivation revokes every session atomically.

    `is_active` is the authentication gate; `status` mirrors it for lifecycle
    display. Both are kept consistent so the session revalidation logic
    (`resolve_session`) behaves exactly as expected.
    """
    student = _student_or_404(db, student_id)
    if student.is_active and student.status == "active":
        student.is_active = False
        student.status = "deactivated"
        _revoke_all_sessions(db, student.id)
    else:
        student.is_active = True
        student.status = "active"
    db.commit()
    db.refresh(student)
    return to_detail_dto(student)