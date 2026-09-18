"""widen date_sheet_entries.programme_id to String(60)

Revision ID: 77de5c9f387d
Revises: 05c97c9296aa
Create Date: 2026-09-14 21:20:22.800302

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '77de5c9f387d'
down_revision: Union[str, None] = '05c97c9296aa'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Widen programme_id from VARCHAR(20) to VARCHAR(60).
    
    PostgreSQL: ALTER COLUMN TYPE VARCHAR(60) - preserves data.
    SQLite: No-op (SQLite doesn't enforce VARCHAR length; the column
    affinity remains TEXT and values are already stored correctly).
    """
    # Get the current dialect
    bind = op.get_bind()
    dialect_name = bind.dialect.name
    
    if dialect_name == 'postgresql':
        op.alter_column(
            'date_sheet_entries',
            'programme_id',
            type_=sa.String(60),
            existing_type=sa.String(20),
            existing_nullable=True,
        )
    elif dialect_name == 'sqlite':
        # SQLite doesn't enforce VARCHAR length; no ALTER needed.
        # The ORM model change is sufficient for future inserts.
        pass
    else:
        # Fallback for other dialects
        op.alter_column(
            'date_sheet_entries',
            'programme_id',
            type_=sa.String(60),
            existing_type=sa.String(20),
            existing_nullable=True,
        )


def downgrade() -> None:
    """Attempt to narrow programme_id back to VARCHAR(20).
    
    WARNING: This will FAIL on PostgreSQL if any existing values exceed 20 chars.
    On SQLite: no-op (no length enforcement).
    """
    bind = op.get_bind()
    dialect_name = bind.dialect.name
    
    if dialect_name == 'postgresql':
        # Check if any values would be truncated
        result = bind.execute(
            sa.text("SELECT count(*) FROM date_sheet_entries WHERE length(programme_id) > 20")
        ).scalar()
        if result and result > 0:
            raise RuntimeError(
                f"Cannot downgrade: {result} rows have programme_id longer than 20 characters. "
                "Manual data cleanup required before downgrade."
            )
        op.alter_column(
            'date_sheet_entries',
            'programme_id',
            type_=sa.String(20),
            existing_type=sa.String(60),
            existing_nullable=True,
        )
    elif dialect_name == 'sqlite':
        # SQLite doesn't enforce VARCHAR length
        pass
    else:
        op.alter_column(
            'date_sheet_entries',
            'programme_id',
            type_=sa.String(20),
            existing_type=sa.String(60),
            existing_nullable=True,
        )