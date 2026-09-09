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
    """

    semester: int = Field(..., ge=1)
    exam_type: str = Field(..., max_length=50)
    academic_year: str = Field(default="", max_length=20)
    subjects: list[str] = Field(default_factory=list)


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
