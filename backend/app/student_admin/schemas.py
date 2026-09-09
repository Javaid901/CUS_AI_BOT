"""
backend/app/student_admin/schemas.py

Request/response schemas for Super Admin → Student Services → Students.

Credential rules:
  - `dob` IS the student's password. It is accepted on create/reset only and is
    WRITE-ONLY: it is never returned in any API response (create, reset, detail
    or list), never logged, and never represented as a "password".
  - Response DTOs are explicit allowlists (the ORM Student is never serialised)
    and never include hashed_password, session material or the DOB credential.
"""

from __future__ import annotations

from pydantic import BaseModel, EmailStr, Field


class StudentCreate(BaseModel):
    reg_no: str = Field(min_length=1, max_length=50, description="Unique registration number")
    name: str = Field(min_length=1, max_length=200)
    dob: str = Field(min_length=1, max_length=20, description="Date of birth — the student's password")
    programme: str = Field(min_length=1, max_length=50)
    current_semester: int = Field(1, ge=1, le=12)
    admission_year: int = Field(..., ge=1990, le=2100)
    is_active: bool = True
    roll_no: str | None = Field(None, max_length=50)
    father_name: str | None = Field(None, max_length=200)
    mother_name: str | None = Field(None, max_length=200)
    gender: str | None = Field(None, max_length=10)
    category: str | None = Field(None, max_length=20)
    email: EmailStr | None = None
    phone: str | None = Field(None, max_length=20)
    college: str | None = Field(None, max_length=200)
    academic_scheme: str | None = Field(None, max_length=20)
    batch: str | None = Field(None, max_length=20)
    address: str | None = Field(None, max_length=2000)


class StudentUpdate(BaseModel):
    """Explicit allowlist of editable profile/academic fields.

    `reg_no` and `dob` are deliberately NOT here: reg_no is the login handle
    and dob is the credential. A DOB change must go through the dedicated
    reset endpoint (which re-hashes and revokes sessions atomically).
    """

    name: str | None = Field(None, max_length=200)
    roll_no: str | None = Field(None, max_length=50)
    father_name: str | None = Field(None, max_length=200)
    mother_name: str | None = Field(None, max_length=200)
    gender: str | None = Field(None, max_length=10)
    category: str | None = Field(None, max_length=20)
    email: str | None = Field(None, max_length=255)
    phone: str | None = Field(None, max_length=20)
    college: str | None = Field(None, max_length=200)
    programme: str | None = Field(None, max_length=50)
    academic_scheme: str | None = Field(None, max_length=20)
    current_semester: int | None = Field(None, ge=1, le=12)
    admission_year: int | None = Field(None, ge=1990, le=2100)
    batch: str | None = Field(None, max_length=20)
    address: str | None = Field(None, max_length=2000)


class StudentResetDob(BaseModel):
    dob: str = Field(min_length=1, max_length=20, description="New date of birth — becomes the student's password")