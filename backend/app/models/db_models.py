"""
backend/app/models/db_models.py

SQLAlchemy ORM models for the CUS AI Assistant.

Tables:
  users            - admin / superadmin accounts (and the chat widget auto-registers
                     lightweight "student" users to obtain a JWT for chat).
  documents        - an uploaded source file and its processing status.
  document_chunks  - individual text chunks with page numbers (stored in Chroma too).
  conversations    - a chat session.
  messages         - individual user/assistant messages within a conversation.
  audit_logs       - admin actions (login, upload, delete, reindex, chat requests, errors).
  university_notices  - uploaded university notice documents (date sheets etc.),
                        one row per physical file, with a verify + publish two-step
                        lifecycle. Only VERIFIED + PUBLISHED notices are served.
  date_sheet_entries  - individual schedule rows (date, day, time window, subject,
                        paper code, venue, programme/stream/semester/batch) stored
                        verbatim from the source document or admin entry. The
                        assistant never fabricates schedule values.
"""

from __future__ import annotations

import uuid

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship

from app.database import Base, _UUID, utcnow


class User(Base):
    __tablename__ = "users"

    id = Column(_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username = Column(String(64), unique=True, index=True, nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=True)
    hashed_password = Column(String(255), nullable=False)
    role = Column(String(20), nullable=False, default="student")  # student | admin | authority_admin | superadmin
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    last_login = Column(DateTime(timezone=True), nullable=True)
    # ----- Authority scope (Authority Admin accounts) -----
    # An Authority Admin is bound to exactly one authority. Super Admin derives
    # the effective scope from this column in the DB — never from the request.
    authority_id = Column(
        String(36),
        ForeignKey("authorities.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # ----- Profile -----
    full_name = Column(String(120), nullable=True)
    designation = Column(String(120), nullable=True)
    phone = Column(String(30), nullable=True)
    avatar_path = Column(String(255), nullable=True)  # relative path under /api/uploads
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    documents = relationship("Document", back_populates="owner", cascade="all, delete-orphan")
    refresh_tokens = relationship("RefreshToken", back_populates="user", cascade="all, delete-orphan")
    authority = relationship("Authority", foreign_keys=[authority_id])


class RefreshToken(Base):
    __tablename__ = "refresh_tokens"

    id = Column(_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(_UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    token = Column(String(255), unique=True, index=True, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    revoked = Column(Boolean, default=False, nullable=False)

    user = relationship("User", back_populates="refresh_tokens")


class Document(Base):
    __tablename__ = "documents"

    id = Column(_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    owner_id = Column(_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    title = Column(String(400), nullable=False)            # human-friendly title
    filename = Column(String(400), nullable=False)          # sanitized stored filename
    original_filename = Column(String(400), nullable=True)  # as uploaded
    file_type = Column(String(20), nullable=True)           # pdf | docx | txt | md
    file_size = Column(Integer, nullable=True)              # bytes
    sha256 = Column(String(64), nullable=True, index=True)  # SHA256 hash for dedup
    status = Column(String(20), default="processing", nullable=False, index=True)
    # status: queued | processing | indexing | ready | failed
    chunk_count = Column(Integer, default=0, nullable=False)
    error = Column(Text, nullable=True)
    language = Column(String(10), nullable=True)
    # ----- Content metadata (enables scheme/semester-aware RAG filtering) -----
    academic_scheme = Column(String(20), nullable=True)   # cbcs | nep | nep2020
    programme = Column(String(50), nullable=True)         # e.g. "bca"
    department = Column(String(200), nullable=True)
    batch = Column(String(20), nullable=True)             # e.g. "2023-2026"
    semester = Column(String(10), nullable=True)          # e.g. "4"
    document_type = Column(String(50), nullable=True)     # syllabus | prospectus | fee_sheet | regulation | notice | exam_scheme
    category = Column(String(50), nullable=True)          # e.g. "nep2020"
    # ----- College-scoped knowledge source columns -----
    college_id = Column(String(64), nullable=True, index=True)    # college slug, e.g. "amar-singh-college"
    college_name = Column(String(255), nullable=True)            # display name of the owning college
    scope = Column(String(20), nullable=False, default="university", index=True)  # university | college
    source_kind = Column(String(20), nullable=True)             # upload | manual | url | backfill
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    owner = relationship("User", back_populates="documents")
    chunks = relationship("DocumentChunk", back_populates="document", cascade="all, delete-orphan")


class DocumentChunk(Base):
    __tablename__ = "document_chunks"
    __table_args__ = (UniqueConstraint("document_id", "chunk_index", name="uq_doc_chunk"),)

    id = Column(_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id = Column(_UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"), nullable=False, index=True)
    chunk_index = Column(Integer, nullable=False)
    page_number = Column(Integer, nullable=True)
    content = Column(Text, nullable=False)
    char_start = Column(Integer, nullable=True)
    char_end = Column(Integer, nullable=True)
    token_count = Column(Integer, nullable=True)

    document = relationship("Document", back_populates="chunks")


class Conversation(Base):
    __tablename__ = "conversations"

    id = Column(_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id = Column(_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    title = Column(String(300), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)

    messages = relationship("Message", back_populates="conversation", cascade="all, delete-orphan")


class Message(Base):
    __tablename__ = "messages"

    id = Column(_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    conversation_id = Column(_UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(20), nullable=False)  # user | assistant | system
    content = Column(Text, nullable=False)
    citations = Column(Text, nullable=True)    # JSON-encoded list of citation dicts
    model = Column(String(60), nullable=True)
    latency_ms = Column(Integer, nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)

    conversation = relationship("Conversation", back_populates="messages")


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id = Column(_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    actor_id = Column(_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)
    actor_role = Column(String(20), nullable=True)
    action = Column(String(40), nullable=False, index=True)  # login, upload, delete, reindex, chat, error
    target = Column(String(400), nullable=True)
    detail = Column(Text, nullable=True)
    ip = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)


class Student(Base):
    """A real university student with credentials for student services."""

    __tablename__ = "students"

    id = Column(_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    reg_no = Column(String(50), unique=True, index=True, nullable=False)
    roll_no = Column(String(50), nullable=True)
    name = Column(String(200), nullable=False)
    father_name = Column(String(200), nullable=True)
    mother_name = Column(String(200), nullable=True)
    dob = Column(String(20), nullable=True)
    gender = Column(String(10), nullable=True)
    category = Column(String(20), nullable=True)
    email = Column(String(255), nullable=True)
    phone = Column(String(20), nullable=True)
    college = Column(String(200), nullable=True)
    programme = Column(String(50), nullable=False)
    academic_scheme = Column(String(20), nullable=True)  # cbcs | nep | nep2020
    current_semester = Column(Integer, nullable=False, default=1)
    admission_year = Column(Integer, nullable=False)
    batch = Column(String(20), nullable=True)
    address = Column(Text, nullable=True)
    status = Column(String(20), default="active", nullable=False)
    hashed_password = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)


class StudentSession(Base):
    """An active student login session."""

    __tablename__ = "student_sessions"

    id = Column(_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    student_id = Column(_UUID(as_uuid=True), ForeignKey("students.id", ondelete="CASCADE"), nullable=False, index=True)
    token = Column(String(255), unique=True, index=True, nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    revoked = Column(Boolean, default=False, nullable=False)

    student = relationship("Student")


class UniversityNotice(Base):
    """A university notice document (date sheet, exam notice, circular).

    One row per physical file. Publication follows an admin-only two-step
    lifecycle: upload/extract -> verify -> publish. Only notices flagged
    VERIFIED and PUBLISHED (with their non-deleted VERIFIED schedule rows)
    are ever served to end users. `deleted_at` performs soft delete.
    """

    __tablename__ = "university_notices"

    id = Column(_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    title = Column(String(400), nullable=False)
    notice_type = Column(String(30), nullable=False, default="notice", index=True)  # notice | date_sheet
    filename = Column(String(400), nullable=False)            # sanitized stored filename
    original_filename = Column(String(400), nullable=True)    # as uploaded
    file_type = Column(String(20), nullable=True)             # pdf | docx
    file_size = Column(Integer, nullable=True)                # bytes
    sha256 = Column(String(64), nullable=True, index=True)    # dedup fingerprint
    file_path = Column(String(600), nullable=False)           # server path (kept out of the public uploads mount)
    categories = Column(Text, nullable=True)                  # JSON array of admin tags
    programme_ids = Column(Text, nullable=True)               # JSON array of programme ids covered by this notice
    exam_type = Column(String(60), nullable=True)             # e.g. annual | semester | supplementary
    exam_session_label = Column(String(120), nullable=True)   # e.g. "June 2026"
    notification_date = Column(DateTime(timezone=True), nullable=True)
    extraction_status = Column(String(30), default="draft", nullable=False, index=True)
    # extraction_status: draft | extracting | pending_verification | verified |
    #                    extraction_failed | manual_entry
    extraction_error = Column(Text, nullable=True)
    validation_flags = Column(Text, nullable=True)            # JSON array of issue codes
    is_verified = Column(Boolean, default=False, nullable=False)
    is_published = Column(Boolean, default=False, nullable=False)
    published_at = Column(DateTime(timezone=True), nullable=True)
    source_kind = Column(String(20), default="upload", nullable=False)  # upload | manual | backfill
    created_by = Column(_UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    entries = relationship("DateSheetEntry", back_populates="notice", cascade="all, delete-orphan")


class DateSheetEntry(Base):
    """A single schedule row belonging to a UniversityNotice.

    Every schedule fact (date, day, time window, subject, paper code, venue,
    programme/stream/semester/batch) is stored verbatim from the source
    document or from an admin manual entry. The assistant never fabricates
    or infers values: read paths return ONLY rows whose notice is VERIFIED +
    PUBLISHED and whose own `extraction_status` equals the verified marker.
    """

    __tablename__ = "date_sheet_entries"

    id = Column(_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    notice_id = Column(_UUID(as_uuid=True), ForeignKey("university_notices.id", ondelete="CASCADE"), nullable=False, index=True)
    row_no = Column(Integer, nullable=False, default=0)
    programme_id = Column(String(20), nullable=True, index=True)
    programme_name = Column(String(200), nullable=True)
    stream = Column(String(60), nullable=True)
    semester = Column(String(10), nullable=True, index=True)
    batch = Column(String(20), nullable=True)
    exam_type = Column(String(60), nullable=True)
    exam_date = Column(String(10), nullable=True)             # ISO yyyy-mm-dd (verbatim from source)
    day = Column(String(12), nullable=True)
    start_time = Column(String(8), nullable=True)             # 24h HH:MM
    end_time = Column(String(8), nullable=True)
    subject_code = Column(String(30), nullable=True)
    subject = Column(String(300), nullable=True)
    paper_code = Column(String(30), nullable=True)
    venue = Column(String(200), nullable=True)
    source_page = Column(Integer, nullable=True)
    source_section = Column(String(200), nullable=True)
    raw = Column(Text, nullable=True)                          # original line/table row text
    extraction_status = Column(String(30), default="pending_verification", nullable=False, index=True)
    # extraction_status: pending_verification | verified | marked_missing | discarded
    is_manual = Column(Boolean, default=False, nullable=False)
    is_corrected = Column(Boolean, default=False, nullable=False)
    validation_flags = Column(Text, nullable=True)             # JSON array of issue codes
    created_at = Column(DateTime(timezone=True), default=utcnow, nullable=False)
    updated_at = Column(DateTime(timezone=True), default=utcnow, onupdate=utcnow, nullable=False)
    deleted_at = Column(DateTime(timezone=True), nullable=True)

    notice = relationship("UniversityNotice", back_populates="entries")


__all__ = [
    "_UUID",
    "AuditLog",
    "Base",
    "Conversation",
    "DateSheetEntry",
    "Document",
    "DocumentChunk",
    "Message",
    "RefreshToken",
    "Student",
    "StudentSession",
    "UniversityNotice",
    "User",
]
