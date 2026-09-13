"""
backend/app/database.py

SQLAlchemy engine / session management.

The metadata layer (users, documents, chunks, conversations, messages, audit logs)
uses SQLAlchemy Core 2.0 style with a session factory. UUID primary keys are used
everywhere. Works with SQLite out of the box; switch to PostgreSQL by setting
DATABASE_URL (e.g. postgresql+psycopg://user:pass@host:5432/cus_ai).
"""

from __future__ import annotations

import uuid
from collections.abc import Generator
from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
from sqlalchemy.types import CHAR, TypeDecorator

from app.config import settings


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _normalized_title_hash(normalized_title: str | None) -> str | None:
    """sha256 hex of the normalized title, matching the engine's title_hash."""
    if not normalized_title:
        return None
    return __import__("hashlib").sha256(normalized_title.encode("utf-8")).hexdigest()


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class _UUID(TypeDecorator):
    """Cross-database UUID type: native UUID on Postgres, CHAR(32) hex on SQLite.

    Defined here (not in app.models) so model modules — including app.analytics —
    can use it without triggering app.models package initialization (circular import).
    """

    impl = CHAR
    cache_ok = True

    def __init__(self, as_uuid: bool = True, length: int = 32, **kwargs):
        self._as_uuid = as_uuid
        super().__init__(length=length, **kwargs)

    def load_dialect_impl(self, dialect):
        if dialect.name == "postgresql":
            return dialect.type_descriptor(PG_UUID(as_uuid=True))
        return dialect.type_descriptor(CHAR(32))

    def process_bind_param(self, value, dialect):
        if value is None:
            return value
        if dialect.name == "postgresql":
            return value
        return value.hex if isinstance(value, uuid.UUID) else str(value).replace("-", "")

    def process_result_value(self, value, dialect):
        if value is None:
            return value
        if dialect.name == "postgresql":
            return value
        return uuid.UUID(value)


def _make_engine():
    url = settings.DATABASE_URL
    connect_args = {}
    # SQLite needs check_same_thread=False for use across FastAPI threads.
    if url.startswith("sqlite"):
        connect_args = {"check_same_thread": False}
    return create_engine(
        url,
        echo=settings.DB_ECHO,
        future=True,
        pool_pre_ping=True,
        connect_args=connect_args,
    )


engine = _make_engine()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency that yields a database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def create_all() -> None:
    """Create all tables (used by run.py / startup). Prefer Alembic in production."""
    # Import models so they are registered on Base.metadata before create_all.
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)
    _upgrade_schema()


def _upgrade_schema() -> None:
    """Add columns introduced after the first deploy without a full migration.

    create_all() only creates missing tables; existing tables keep their old
    shape, so newly added columns are patched in with ALTER TABLE ADD COLUMN
    (safe on SQLite for nullable columns). Runs idempotently on every startup.
    """
    from sqlalchemy import inspect, text

    additions = {
        "users": {
            "full_name": "VARCHAR(120)",
            "designation": "VARCHAR(120)",
            "phone": "VARCHAR(30)",
            "avatar_path": "VARCHAR(255)",
            "updated_at": "DATETIME",
            "authority_id": "VARCHAR(36)",
        },
        "authorities": {
            "category_id": "VARCHAR(36)",
            "source_kind": "VARCHAR(20)",
            "deleted_at": "DATETIME",
        },
        "students": {
            "academic_scheme": "VARCHAR(20)",
        },
        "student_results": {
            "exam_roll_no": "VARCHAR(50)",
        },
        "student_exam_forms": {
            "exam_session_id": "VARCHAR(32)",
            "form_no": "VARCHAR(30)",
            "photo_path": "VARCHAR(255)",
            "eligibility_snapshot": "TEXT",
            "printed_at": "DATETIME",
        },
        "documents": {
            "academic_scheme": "VARCHAR(20)",
            "programme": "VARCHAR(50)",
            "department": "VARCHAR(200)",
            "batch": "VARCHAR(20)",
            "semester": "VARCHAR(10)",
            "document_type": "VARCHAR(50)",
            "category": "VARCHAR(50)",
            "college_id": "VARCHAR(64)",
            "college_name": "VARCHAR(255)",
            "scope": "VARCHAR(20)",
            "source_kind": "VARCHAR(20)",
        },
        "programmes": {
            "scheme_id": "VARCHAR(32)",
            "eligibility": "TEXT",
            "fee_structure": "TEXT",
        },
        "grievances": {
            "programme": "VARCHAR(50)",
            "phone": "VARCHAR(30)",
            "source_kind": "VARCHAR(20)",
            "email_status": "VARCHAR(20)",
            "authority_email_status": "VARCHAR(20)",
            "tracking_token_hash": "VARCHAR(128)",
            "client_request_id": "VARCHAR(64)",
            "is_read": "BOOLEAN",
            "read_at": "DATETIME",
            "read_by": "VARCHAR(200)",
            "authority_response": "TEXT",
            "authority_response_at": "DATETIME",
            "response_email_status": "VARCHAR(20)",
        },
        "grievance_notifications": {
            "provider_message_id": "VARCHAR(200)",
        },
        "knowledge_gaps": {
            "resolution_text": "TEXT",
            "resolved_by": "VARCHAR(200)",
        },
        # Phase 1 — Intelligent Website Document Ingestion (additive only).
        "website_pages": {
            "doc_type": "VARCHAR(20)",
            "classification_status": "VARCHAR(30)",
            "classification_confidence": "TEXT",
            "classification_signals": "TEXT",
            "doc_meta": "TEXT",
            "raw_sha256": "VARCHAR(64)",
            "raw_size": "INTEGER",
            "reviewed_by": "VARCHAR(200)",
            "reviewed_at": "DATETIME",
            "review_note": "TEXT",
        },
        "website_page_versions": {
            "raw_path": "VARCHAR(500)",
            "raw_sha256": "VARCHAR(64)",
        },
    }
    inspector = inspect(engine)
    try:
        table_names = set(inspector.get_table_names())
    except Exception:
        return
    with engine.begin() as conn:
        for table, columns in additions.items():
            if table not in table_names:
                continue
            existing = {c["name"] for c in inspector.get_columns(table)}
            for name, col_type in columns.items():
                if name in existing:
                    continue
                try:
                    conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {name} {col_type}'))
                except Exception:
                    # Dialect doesn't support DDL here — ignore and keep going.
                    pass

    # Ensure the category FK column has an index on pre-existing databases
    # (SQLite cannot add FK constraints via ALTER; the constraint itself is
    # declared in the ORM and is present on freshly created tables).
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_authorities_category_id "
                "ON authorities (category_id)"
            ))
    except Exception:
        pass

    # Same rationale for the Authority Admin scope column on users.
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_users_authority_id "
                "ON users (authority_id)"
            ))
    except Exception:
        pass

    # Grievance tracking digests: unique index for existing databases (fresh
    # databases get it from the ORM index declaration). Lookups happen only
    # with both reference AND token, so the unique digest guard is the point.
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_grievances_tracking_token_hash "
                "ON grievances (tracking_token_hash)"
            ))
    except Exception:
        pass

    # Same rationale for the client idempotency key: duplicates must be
    # impossible at the storage layer, retries must be answered, not repeated.
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_grievances_client_request_id "
                "ON grievances (client_request_id)"
            ))
    except Exception:
        pass

    # Authority Admin portal read-path: list+unread-filter inside one authority.
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_grievances_authority_read "
                "ON grievances (authority_id, is_read)"
            ))
    except Exception:
        pass

    # Backfill: assign academic scheme to students that predate the column.
    # NEP 2020 cohorts start from 2023 admissions; earlier cohorts follow CBCS.
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE students SET academic_scheme = CASE WHEN admission_year >= 2023 "
                "THEN 'nep2020' ELSE 'cbcs' END WHERE academic_scheme IS NULL"
            ))
    except Exception:
        pass

    # Backfill: stamp a STABLE examination roll number on StudentResult rows
    # that predate the column (demo data — one examination roll per student,
    # assigned in Semester 1 and reused for every later semester).
    # Deterministic: <2-digit admission year><3-digit enrollment order>.
    # Idempotent: only rows still NULL are touched, so real imported rows with
    # no roll remain exactly as imported. Roll numbers never enter URLs.
    try:
        from app.models import Student, StudentResult  # noqa: F401

        with SessionLocal() as _db:
            enrolled = _db.query(Student).order_by(Student.reg_no).all()
            seq = {str(s.id): i + 1 for i, s in enumerate(enrolled)}
            pending = (
                _db.query(StudentResult)
                .filter(StudentResult.exam_roll_no.is_(None))
                .all()
            )
            for r in pending:
                sid = str(r.student_id)
                if sid not in seq:
                    continue
                yy = str(r.student.admission_year)[-2:] if r.student and r.student.admission_year else "00"
                r.exam_roll_no = f"{yy}{seq[sid]:03d}"
            if pending:
                _db.commit()
    except Exception:
        pass

    # Backfill: pre-existing documents are university-wide content.
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE documents SET scope = 'university' WHERE scope IS NULL"
            ))
    except Exception:
        pass

    # Backfill: stamp legacy Chroma chunks as university-wide so scope filters
    # do not hide them. Best-effort: without this, vector retrieval would only
    # see college-tagged chunks once a scope clause is applied.
    try:
        from app.ingest.store import backfill_scope_metadata
        backfill_scope_metadata("university")
    except Exception:
        pass

    # Phase 1 backfill: existing website pages exposed to the new
    # classification-state columns get a sane default so the non-null ORM
    # constraint and admin filters stay consistent for pre-upgrade rows.
    try:
        with engine.begin() as conn:
            conn.execute(text(
                "UPDATE website_pages SET classification_status = 'draft' "
                "WHERE classification_status IS NULL"
            ))
    except Exception:
        pass

    # Title-similarity dedup relies on title_hash, which pre-upgrade rows never
    # received. Compute it deterministically from the normalized title using
    # the same hashing scheme as the engine.
    try:
        from app.models.website_sync import WebsitePage

        with SessionLocal() as _db:
            pending = _db.query(WebsitePage).filter(
                WebsitePage.title_hash.is_(None),
                WebsitePage.normalized_title.isnot(None),
            ).all()
            for p in pending:
                p.title_hash = _normalized_title_hash(p.normalized_title)
            if pending:
                _db.commit()
    except Exception:
        pass
