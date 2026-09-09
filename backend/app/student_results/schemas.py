"""
backend/app/student_results/schemas.py

Payload contracts for Student Results (Phase B).

Import rows travel through the pipeline as raw dicts (whatever the spreadsheet
contained) so that preview and confirm share ONE server-side validator
(app/student_results/service.analyze_rows). The confirm body is deliberately
loose-typed: every field is re-validated on the sever, never trusted.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class ImportBundle(BaseModel):
    """Super-Admin confirm body: the raw spreadsheet rows echoed from preview."""

    filename: str = Field(default="", max_length=255)
    rows: list[dict[str, Any]] = Field(default_factory=list)


class ResultsLookup(BaseModel):
    """Per-attempt result lookup for the student chat flow.

    `exam_roll_no` is an INPUT (an opaque grouping of a published attempt),
    never an authorization credential — the student is resolved server-side
    from the authenticated StudentSession.
    """

    semester: int
    exam_roll_no: str = Field(default="", max_length=50)
    as_attachment: bool = False