"""
backend/app/models/demo_models.py

Demo/synthetic data ORM models for all student services.
These tables store fictional demo data used for presentation/testing.

Every table has a ForeignKey to students.id so the demo data
is linked to actual student accounts.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship

from app.database import Base, utcnow
from app.database import _UUID


def _fk_col(**kwargs):
    """UUID foreign key column with sensible defaults."""
    kwargs.setdefault("nullable", False)
    kwargs.setdefault("index", True)
    return Column(_UUID(as_uuid=True), ForeignKey("students.id", ondelete="CASCADE"), **kwargs)


class StudentResult(Base):
    __tablename__ = "student_results"

    id = Column(_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    student_id = _fk_col()
    exam_roll_no = Column(String(50), nullable=True)
    semester = Column(Integer, nullable=False)
    exam_type = Column(String(50), default="Regular")
    subject_name = Column(String(200), nullable=False)
    subject_code = Column(String(20), nullable=True)
    internal_marks = Column(Integer, nullable=True)
    external_marks = Column(Integer, nullable=True)
    total_marks = Column(Integer, nullable=True)
    max_marks = Column(Integer, default=100)
    grade = Column(String(5), nullable=True)
    sgpa = Column(String(5), nullable=True)
    cgpa = Column(String(5), nullable=True)
    status = Column(String(20), default="pass")
    academic_year = Column(String(20), nullable=True)
    created_at = Column(DateTime, default=utcnow)

    student = relationship("Student")


class StudentAdmitCard(Base):
    __tablename__ = "student_admit_cards"

    id = Column(_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    student_id = _fk_col()
    semester = Column(Integer, nullable=False)
    exam_type = Column(String(50), default="Regular")
    exam_session = Column(String(100), nullable=True)
    centre_name = Column(String(200), nullable=True)
    centre_address = Column(Text, nullable=True)
    centre_code = Column(String(20), nullable=True)
    reporting_time = Column(String(50), nullable=True)
    subjects = Column(Text, nullable=True)
    instructions = Column(Text, nullable=True)
    issued_date = Column(String(20), nullable=True)
    academic_year = Column(String(20), nullable=True)
    created_at = Column(DateTime, default=utcnow)

    student = relationship("Student")


class StudentExamForm(Base):
    __tablename__ = "student_exam_forms"

    id = Column(_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    student_id = _fk_col()
    semester = Column(Integer, nullable=False)
    exam_type = Column(String(50), default="Regular")
    form_status = Column(String(50), default="Pending")
    subjects = Column(Text, nullable=True)
    fee_status = Column(String(50), default="Unpaid")
    fee_amount = Column(Integer, nullable=True)
    transaction_id = Column(String(100), nullable=True)
    submission_date = Column(String(20), nullable=True)
    academic_year = Column(String(20), nullable=True)
    # Exam Session model additions (additive, nullable — legacy rows stay valid).
    exam_session_id = Column(_UUID(as_uuid=True), nullable=True, index=True)
    form_no = Column(String(30), nullable=True, index=True)
    photo_path = Column(String(255), nullable=True)
    eligibility_snapshot = Column(Text, nullable=True)
    printed_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow)

    student = relationship("Student")


class ExamSession(Base):
    """A provisioned examination window students fill their exam forms against.

    Lifecycle: Draft → Open (applications accepted) → Closed → Archived. The
    `code` (e.g. "EXMPG26") prefixes every server-generated form number so a
    form_no like "EXMPG26-3-00539" is globally readable and unique. `form_seq`
    is the per-session counter that backs form_no allocation.
    """

    __tablename__ = "exam_sessions"

    id = Column(_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name = Column(String(200), nullable=False)
    code = Column(String(40), nullable=False, unique=True, index=True)
    programme = Column(String(50), nullable=False)
    batch = Column(String(30), nullable=True)
    semester = Column(Integer, nullable=False)
    exam_type = Column(String(50), default="Regular")
    academic_year = Column(String(20), nullable=True)
    application_open_at = Column(DateTime, nullable=True)
    last_date_normal = Column(DateTime, nullable=True)
    last_date_late = Column(DateTime, nullable=True)
    base_fee = Column(Integer, default=0)
    late_fee = Column(Integer, default=0)
    status = Column(String(20), default="Draft")
    form_seq = Column(Integer, default=0)
    created_by = Column(_UUID(as_uuid=True), nullable=True)
    created_at = Column(DateTime, default=utcnow)
    updated_at = Column(DateTime, default=utcnow, onupdate=utcnow)


class ExamApplicationSubject(Base):
    """Server-derived subjects on a submitted/printed exam form.

    `source` records where the subject list came from (system = derived from the
    academic catalogue; manual = carved on a legacy/admin bookkeeping row). A
    student can never write rows here — the server writes them at fill time.
    """

    __tablename__ = "exam_application_subjects"

    id = Column(_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    form_id = Column(_UUID(as_uuid=True), ForeignKey("student_exam_forms.id", ondelete="CASCADE"), nullable=False, index=True)
    subject_code = Column(String(30), nullable=True)
    subject_name = Column(String(200), nullable=False)
    source = Column(String(20), default="system")
    verified = Column(Boolean, default=False)
    verified_by = Column(String(50), nullable=True)
    verified_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow)

    form = relationship("StudentExamForm")


class ExamEligibility(Base):
    """Snapshot of a student's eligibility check for one exam session.

    `rules` is a JSON list of {"rule", "passed", "message"} entries — the
    deterministic evidence behind `eligible`. A student can never express these;
    only the server writes them at fill time.
    """

    __tablename__ = "exam_eligibility"

    id = Column(_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    form_id = Column(_UUID(as_uuid=True), ForeignKey("student_exam_forms.id", ondelete="CASCADE"), nullable=False, index=True)
    session_id = Column(_UUID(as_uuid=True), nullable=True, index=True)
    eligible = Column(Boolean, default=False)
    rules = Column(Text, nullable=True)
    evaluated_at = Column(DateTime, default=utcnow)

    form = relationship("StudentExamForm")


class ExamPayment(Base):
    """A payment record on an exam form (mock/manual payment backend).

    Rows start as `initiated`; the payment backend adapter performs the actual
    transition to `success` (server-side, never trusted browser state). The
    exam form only becomes submittable once a `success` payment exists.
    """

    __tablename__ = "exam_payments"

    id = Column(_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    form_id = Column(_UUID(as_uuid=True), ForeignKey("student_exam_forms.id", ondelete="CASCADE"), nullable=False, index=True)
    session_id = Column(_UUID(as_uuid=True), nullable=True, index=True)
    amount = Column(Integer, nullable=False)
    head = Column(String(100), default="Exam Form Fee")
    status = Column(String(20), default="initiated")
    gateway = Column(String(50), default="mock")
    gateway_ref = Column(String(100), nullable=True)
    failure_reason = Column(String(200), nullable=True)
    recorded_by = Column(String(50), nullable=True)
    recorded_at = Column(DateTime, nullable=True)
    reconciled_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=utcnow)

    form = relationship("StudentExamForm")


class FeeReceipt(Base):
    __tablename__ = "fee_receipts"

    id = Column(_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    student_id = _fk_col()
    receipt_no = Column(String(50), nullable=True)
    transaction_id = Column(String(100), nullable=True)
    fee_heads = Column(Text, nullable=True)
    paid_amount = Column(Integer, nullable=True)
    total_amount = Column(Integer, nullable=True)
    pending_amount = Column(Integer, default=0)
    payment_date = Column(String(20), nullable=True)
    payment_mode = Column(String(50), nullable=True)
    semester = Column(Integer, nullable=True)
    academic_year = Column(String(20), nullable=True)
    status = Column(String(20), default="Paid")
    created_at = Column(DateTime, default=utcnow)

    student = relationship("Student")


class StudentAttendance(Base):
    __tablename__ = "student_attendance"

    id = Column(_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    student_id = _fk_col()
    semester = Column(Integer, nullable=False)
    subject_name = Column(String(200), nullable=False)
    subject_code = Column(String(20), nullable=True)
    total_classes = Column(Integer, nullable=True)
    attended_classes = Column(Integer, nullable=True)
    percentage = Column(String(10), nullable=True)
    academic_year = Column(String(20), nullable=True)
    created_at = Column(DateTime, default=utcnow)

    student = relationship("Student")


class StudentTranscript(Base):
    __tablename__ = "student_transcripts"

    id = Column(_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    student_id = _fk_col()
    semester = Column(Integer, nullable=False)
    academic_year = Column(String(20), nullable=True)
    credits_earned = Column(Integer, nullable=True)
    total_credits = Column(Integer, nullable=True)
    sgpa = Column(String(5), nullable=True)
    cgpa = Column(String(5), nullable=True)
    status = Column(String(20), default="Completed")
    created_at = Column(DateTime, default=utcnow)

    student = relationship("Student")


class MigrationCertificate(Base):
    __tablename__ = "migration_certificates"

    id = Column(_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    student_id = _fk_col()
    certificate_no = Column(String(50), nullable=True)
    issue_status = Column(String(50), default="Not Applied")
    issue_date = Column(String(20), nullable=True)
    application_date = Column(String(20), nullable=True)
    reason = Column(Text, nullable=True)
    created_at = Column(DateTime, default=utcnow)

    student = relationship("Student")


class Revaluation(Base):
    __tablename__ = "student_revaluations"

    id = Column(_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    student_id = _fk_col()
    semester = Column(Integer, nullable=False)
    subject_name = Column(String(200), nullable=False)
    subject_code = Column(String(20), nullable=True)
    application_date = Column(String(20), nullable=True)
    status = Column(String(50), default="Pending")
    result = Column(String(100), nullable=True)
    fee_status = Column(String(20), default="Paid")
    created_at = Column(DateTime, default=utcnow)

    student = relationship("Student")


class XeroxRequest(Base):
    __tablename__ = "xerox_requests"

    id = Column(_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    student_id = _fk_col()
    semester = Column(Integer, nullable=False)
    paper_name = Column(String(200), nullable=True)
    application_date = Column(String(20), nullable=True)
    fee_status = Column(String(20), default="Paid")
    request_status = Column(String(50), default="Processing")
    estimated_date = Column(String(20), nullable=True)
    created_at = Column(DateTime, default=utcnow)

    student = relationship("Student")


class BacklogStatus(Base):
    __tablename__ = "student_backlogs"

    id = Column(_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    student_id = _fk_col()
    semester = Column(Integer, nullable=False)
    subject_name = Column(String(200), nullable=False)
    subject_code = Column(String(20), nullable=True)
    exam_type = Column(String(50), default="Backlog")
    status = Column(String(20), default="Pending")
    improvement_applied = Column(Boolean, default=False)
    cleared_date = Column(String(20), nullable=True)
    created_at = Column(DateTime, default=utcnow)

    student = relationship("Student")


class CourseRegistration(Base):
    __tablename__ = "course_registrations"

    id = Column(_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    student_id = _fk_col()
    semester = Column(Integer, nullable=False)
    academic_year = Column(String(20), nullable=True)
    elective_subjects = Column(Text, nullable=True)
    registered_subjects = Column(Text, nullable=True)
    registration_date = Column(String(20), nullable=True)
    status = Column(String(20), default="Registered")
    created_at = Column(DateTime, default=utcnow)

    student = relationship("Student")


class HelpdeskTicket(Base):
    __tablename__ = "helpdesk_tickets"

    id = Column(_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    student_id = _fk_col()
    ticket_id = Column(String(50), nullable=True)
    category = Column(String(100), nullable=True)
    subject = Column(String(200), nullable=True)
    description = Column(Text, nullable=True)
    status = Column(String(50), default="Open")
    assigned_officer = Column(String(200), nullable=True)
    assigned_department = Column(String(200), nullable=True)
    resolution = Column(Text, nullable=True)
    created_date = Column(String(20), nullable=True)
    resolved_date = Column(String(20), nullable=True)
    created_at = Column(DateTime, default=utcnow)

    student = relationship("Student")


__all__ = [
    "BacklogStatus",
    "CourseRegistration",
    "ExamApplicationSubject",
    "ExamEligibility",
    "ExamPayment",
    "ExamSession",
    "FeeReceipt",
    "HelpdeskTicket",
    "MigrationCertificate",
    "Revaluation",
    "StudentAdmitCard",
    "StudentAttendance",
    "StudentExamForm",
    "StudentResult",
    "StudentTranscript",
    "XeroxRequest",
]

# Register the string-referenced relationship target (Student) so this module
# is also importable standalone (e.g. by one-off maintenance scripts).
from app.models.db_models import Student as _Student  # noqa: E402, F401
