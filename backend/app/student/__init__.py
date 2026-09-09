"""
backend/app/student/__init__.py

Student Services (Step 1) — authenticated entry point for a student's own
academic services (results / admit card / exam form).

This module provides:
  - session.py: opaque server-side login sessions (HttpOnly cookie + SHA-256
    token hash at rest, reusing the existing StudentSession table)
  - gate.py:   SSE event builders for the auth gate and the authenticated hub
  - routes.py: POST /api/student/verify · POST /api/student/logout ·
               GET /api/student/session

No credentials ever travel through /api/chat/ask, chat history, analytics or
audit logs — the verify endpoint stands fully outside the chat pipeline.
"""