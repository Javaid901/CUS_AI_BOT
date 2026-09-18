"""Alembic environment for the CUS AI Assistant (Phase 3C-2).

Target database resolution (in priority order):
    1. ALEMBIC_DATABASE_URL environment variable (explicit, preferred for
       isolated PostgreSQL validation / controlled cutover).
    2. app.config.settings.DATABASE_URL (the application's own setting).

The URL is never stored in alembic.ini and never printed with credentials.
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Make `app` importable when alembic is invoked from backend/.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.config import settings  # noqa: E402
from app.database import Base, _UUID  # noqa: E402

# Import the model package so every table is registered on Base.metadata.
import app.models  # noqa: E402,F401

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)


def _resolve_url() -> str:
    return os.environ.get("ALEMBIC_DATABASE_URL") or settings.DATABASE_URL


def _redacted(url: str) -> str:
    """Return a safe-to-log description of the target database."""
    if url.startswith("sqlite"):
        return "sqlite"
    # postgresql+psycopg://user:password@host:port/db -> show dialect + host/db
    try:
        scheme, _, rest = url.partition("://")
        if "@" in rest:
            _, _, hostpart = rest.rpartition("@")
        else:
            hostpart = rest
        return f"{scheme}@{hostpart}"
    except Exception:
        return "unknown"


config.set_main_option("sqlalchemy.url", _resolve_url())

target_metadata = Base.metadata


def render_item(type_, obj, autogen_context):
    """Render the project's cross-database UUID type portably.

    Without this, autogenerate would emit the TypeDecorator's SQLite impl
    (CHAR(32)), producing a schema that is wrong on PostgreSQL.
    """
    if type_ == "type" and isinstance(obj, _UUID):
        autogen_context.imports.add("from app.database import _UUID")
        return "_UUID(as_uuid=%r)" % obj._as_uuid
    return False


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (emit SQL to stdout)."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_item=render_item,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode against a live engine."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
        future=True,
    )

    print(f"[alembic] target database: {_redacted(_resolve_url())}")

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            render_item=render_item,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
