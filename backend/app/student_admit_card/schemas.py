"""
backend/app/student_admit_card/schemas.py

Payload contracts for Student Admit Card (Phase C).

Admin CRUD bodies are explicit pydantic models; import rows travel as raw
dicts (whatever the spreadsheet contained) so preview and confirm share ONE
server-side validator (app/student_admit_card/service.analyze_rows). The
confirm body is deliberately loose-typed: every field is re-validated on the
server, never trusted. Subjects/instructions are accepted as lists of strings
and stored as JSON arrays by the service.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ImportBundle(BaseModel):
    """Super-Admin confirm body: the raw spreadsheet rows echoed from preview."""

    filename: str = Field(default="", max_length=255)
    rows: list[dict[str, Any]] = Field(default_factory=list)


class _CardFields(BaseModel):
    """Shared card fields (explicit allowlists, server-validated)."""

    semester: int = Field(..., ge=1)
    exam_type: str = Field(default="Regular", max_length=50)
    exam_session: str = Field(default="", max_length=100)
    academic_year: str = Field(default="", max_length=20)
    centre_name: str = Field(default="", max_length=200)
    centre_code: str = Field(default="", max_length=20)
    centre_address: str = Field(default="")
    reporting_time: str = Field(default="", max_length=50)
    subjects: list[str] = Field(default_factory=list)
    instructions: list[str] = Field(default_factory=list)
    issued_date: str = Field(default="", max_length=20)


class AdmitCardCreate(_CardFields):
    """POST /api/admin/admit-cards body. reg_no resolves server-side."""

    reg_no: str = Field(..., max_length=40)


class AdmitCardUpdate(BaseModel):
    """PATCH /api/admin/admit-cards/{id} body. All fields optional."""

    semester: int | None = Field(default=None, ge=1)
    exam_type: str | None = Field(default=None, max_length=50)
    exam_session: str | None = Field(default=None, max_length=100)
    academic_year: str | None = Field(default=None, max_length=20)
    centre_name: str | None = Field(default=None, max_length=200)
    centre_code: str | None = Field(default=None, max_length=20)
    centre_address: str | None = Field(default=None)
    reporting_time: str | None = Field(default=None, max_length=50)
    subjects: list[str] | None = Field(default=None)
    instructions: list[str] | None = Field(default=None)
    issued_date: str | None = Field(default=None, max_length=20)