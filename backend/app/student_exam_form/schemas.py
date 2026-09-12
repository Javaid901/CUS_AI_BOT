"""
backend/app/student_exam_form/schemas.py

Payload contracts for Student Exam Form (Phase D).

Admin create/update bodies carry the administrative fields a Super Admin may set
(including fee/payment metadata) but NEVER student identity from the client:
`reg_no` on create is resolved server-side to a Student.id. The student Fill
body is a strict allowlist: only semester, exam type, academic year, subjects,
instructions-grade form text and the self-declared submission intent. Students
can never express fee_status / transaction_id / fee_amount / submission_date.

Subjects/instructions travel as lists of strings (JSON arrays server-side), the
same representation used by Results and Admit Card.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

# Supported examination types (canonical values — driven by the existing model /
# seed data which uses "Regular"; Backlog is the other half of the fill/print flow).
EXAM_TYPES: tuple[str, ...] = ("Regular", "Backlog")


class ExamSessionCreate(BaseModel):
    """POST /api/admin/exam-sessions body (super-admin provisioning)."""

    name: str = Field(..., max_length=200)
    code: str = Field(..., max_length=40)
    programme: str = Field(..., max_length=50)
    batch: str = Field(default="", max_length=30)
    semester: int = Field(..., ge=1)
    exam_type: str = Field(default="Regular", max_length=50)
    academic_year: str = Field(default="", max_length=20)
    application_open_at: str | None = Field(default=None, max_length=40)
    last_date_normal: str | None = Field(default=None, max_length=40)
    last_date_late: str | None = Field(default=None, max_length=40)
    base_fee: int = Field(default=0, ge=0)
    late_fee: int = Field(default=0, ge=0)
    status: str = Field(default="Draft", max_length=20)


class ExamSessionUpdate(BaseModel):
    """PATCH /api/admin/exam-sessions/{id} body. All fields optional."""

    name: str | None = Field(default=None, max_length=200)
    code: str | None = Field(default=None, max_length=40)
    programme: str | None = Field(default=None, max_length=50)
    batch: str | None = Field(default=None, max_length=30)
    semester: int | None = Field(default=None, ge=1)
    exam_type: str | None = Field(default=None, max_length=50)
    academic_year: str | None = Field(default=None, max_length=20)
    application_open_at: str | None = Field(default=None, max_length=40)
    last_date_normal: str | None = Field(default=None, max_length=40)
    last_date_late: str | None = Field(default=None, max_length=40)
    base_fee: int | None = Field(default=None, ge=0)
    late_fee: int | None = Field(default=None, ge=0)
    status: str | None = Field(default=None, max_length=20)


class ExamSessionStatusUpdate(BaseModel):
    """POST /api/admin/exam-sessions/{id}/status — deterministic transition."""

    status: str = Field(..., max_length=20)


class ImportBundle(BaseModel):
    """Super-Admin confirm body: the raw spreadsheet rows echoed from preview."""

    filename: str = Field(default="", max_length=255)
    rows: list[dict[str, Any]] = Field(default_factory=list)


class _FormFields(BaseModel):
    """Shared form fields (explicit allowlists, server-validated)."""

    semester: int = Field(..., ge=1)
    exam_type: str = Field(default="Regular", max_length=50)
    academic_year: str = Field(default="", max_length=20)
    subjects: list[str] = Field(default_factory=list)


class AdminFormCreate(_FormFields):
    """POST /api/admin/exam-forms body. reg_no resolves server-side.

    Administrative payment fields are allowed here (Super Admin only).
    """

    reg_no: str = Field(..., max_length=40)
    form_status: str = Field(default="Pending", max_length=50)
    fee_status: str = Field(default="Unpaid", max_length=50)
    fee_amount: int | None = Field(default=None, ge=0)
    transaction_id: str = Field(default="", max_length=100)
    submission_date: str = Field(default="", max_length=20)


class AdminFormUpdate(BaseModel):
    """PATCH /api/admin/exam-forms/{id} body. All fields optional."""

    semester: int | None = Field(default=None, ge=1)
    exam_type: str | None = Field(default=None, max_length=50)
    academic_year: str | None = Field(default=None, max_length=20)
    subjects: list[str] | None = Field(default=None)
    form_status: str | None = Field(default=None, max_length=50)
    fee_status: str | None = Field(default=None, max_length=50)
    fee_amount: int | None = Field(default=None, ge=0)
    transaction_id: str | None = Field(default=None, max_length=100)
    submission_date: str | None = Field(default=None, max_length=20)


class StudentFillCreate(BaseModel):
    """POST /api/student/exam-forms body — the student Fill workflow.

    Student is always identified by the resolved session cookie; only the form
    identity + own data may be expressed here. Fee/payment/admin fields are
    deliberately absent — the server owns them.

    Two mutually-exclusive flavours (the server dispatches on `exam_session_id`):

      * Session-driven (primary, Phase D2): send ONLY `exam_session_id`. The
        server derives programme/semester/exam_type/academic_year/fee/subjects
        from the OPEN ExamSession and runs the deterministic eligibility gate.
        `subjects` and the legacy identity fields are IGNORED when a session is
        given (forged subject lists can never override catalogue subjects).

      * Legacy (backward-compatible exception path): send semester + exam_type
        (+ optional academic_year/subjects). Used by the existing chat picker /
        admin-provisioned flow and kept so pre-session forms keep working.
    """

    exam_session_id: str | None = Field(default=None, max_length=36)
    semester: int | None = Field(default=None, ge=1)
    exam_type: str | None = Field(default=None, max_length=50)
    academic_year: str = Field(default="", max_length=20)
    subjects: list[str] = Field(default_factory=list)
    confirm: bool = Field(default=True)


class StudentPrintBody(BaseModel):
    """POST /api/student/exam-forms/{id}/print — PDF download/open vehicle.

    `as_attachment=True` sends a Content-Disposition: attachment (download);
    False returns the same PDF for inline preview. No other client state is
    accepted; the server re-scopes the form to the session student.
    """

    as_attachment: bool = False


class StudentSubmitBody(BaseModel):
    """POST /api/student/exam-forms/{id}/submit — affirm the filled form.

    The only client-declared value is confirmation; the server performs the
    deterministic Pending → Submitted transition and writes submission_date.
    """

    confirm: bool = True


class FormStatusUpdate(BaseModel):
    """POST /api/admin/exam-forms/{id}/status — deterministic status transitio.

    Restricts form_status to the server-side allowlist (Draft/Pending/Submitted/
    Approved/Rejected/Withdrawn). Students can never drive these.
    """

    form_status: str = Field(..., max_length=50)
