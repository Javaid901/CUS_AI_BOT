"""
backend/app/student_results/

Phase B — Student Results.

  Student side  : GET /api/student/results   (cookie-session-scoped "my results")
  Admin side    : GET /api/admin/results          list
                  POST /api/admin/results/preview import CSV/XLSX → validate
                  POST /api/admin/results/confirm apply in ONE transaction

Import pipeline contract (preview → confirm):
  - Preview writes NOTHING. It parses the spreadsheet, validates every row
    against the explicit semester allowlist + the student table + duplicate
    policy, and returns full per-row status.
  - Confirm re-validates the SAME rows server-side (it never trusts the
    preview response) and applies them in a single transaction that rolls
    back on any failure — a partial import is impossible.
"""

from app.student_results.routes import admin_router, router

__all__ = ["router", "admin_router"]