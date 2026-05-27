"""add_tenant_id_to_registry_tables

Add tenant_id column to registry tables (agents, oauth_clients, oauth_tokens,
auth_codes) for multi-tenant isolation support.

The column was already applied in the Store DDL (store.py), but the Alembic
migration metadata was out of sync. This migration adds the column for
existing MySQL databases that were created directly by the DDL without
going through Alembic.

Revision ID: c2f23a1b4d8e
Revises: 01eb1637ac57
Create Date: 2026-05-27 16:25:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect


# revision identifiers, used by Alembic.
revision: str = 'c2f23a1b4d8e'
down_revision: Union[str, Sequence[str], None] = '01eb1637ac57'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table: str, column: str) -> bool:
    """Check if a column exists in the given table (works on both SQLite & MySQL).

    In offline mode (``sql=True``), returns ``False`` so the full DDL
    is emitted — safe default for fresh MySQL databases where columns
    don't exist yet.
    """
    bind = op.get_bind()
    try:
        inspector = inspect(bind)
        columns = [c['name'] for c in inspector.get_columns(table)]
        return column in columns
    except Exception:
        return False


def upgrade() -> None:
    """Add tenant_id columns to registry tables.

    Idempotent: silently skips any table where the column already exists
    (e.g. databases pre-created by Store's inline DDL).
    """

    for tbl in ("agents", "oauth_clients", "oauth_tokens", "auth_codes"):
        if _column_exists(tbl, "tenant_id"):
            continue
        with op.batch_alter_table(tbl) as batch_op:
            batch_op.add_column(
                sa.Column("tenant_id", sa.String(255), nullable=False, server_default="")
            )


def downgrade() -> None:
    """Remove tenant_id columns from registry tables.

    Idempotent: silently skips any table where the column has already
    been removed.
    """

    for tbl in ("auth_codes", "oauth_tokens", "oauth_clients", "agents"):
        if not _column_exists(tbl, "tenant_id"):
            continue
        with op.batch_alter_table(tbl) as batch_op:
            batch_op.drop_column("tenant_id")