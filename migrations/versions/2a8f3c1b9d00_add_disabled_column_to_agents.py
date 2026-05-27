"""add_disabled_column_to_agents

ALTER TABLE 迁移 — 为 agents 表添加 disabled 列。
兼容 SQLite (INTEGER) 和 MySQL (TINYINT(1)) 双模式，保留已有数据。

此迁移依赖于 c2f23a1b4d8e (tenant_id 添加完成之后)，
与 01eb1637ac57 (初始 schema) 形成线性链:
  01eb1637ac57 → c2f23a1b4d8e → 2a8f3c1b9d00

Revision ID: 2a8f3c1b9d00
Revises: c2f23a1b4d8e
Create Date: 2026-05-27 16:30:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql
from sqlalchemy import inspect

# revision identifiers, used by Alembic.
revision: str = '2a8f3c1b9d00'
down_revision: Union[str, Sequence[str], None] = 'c2f23a1b4d8e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _column_exists(table: str, column: str) -> bool:
    """Check if a column exists in the given table (works on both SQLite & MySQL)."""
    bind = op.get_bind()
    inspector = inspect(bind)
    columns = [c['name'] for c in inspector.get_columns(table)]
    return column in columns


def upgrade() -> None:
    """Upgrade schema — add disabled column to agents table.

    SQLite:  INTEGER NOT NULL DEFAULT 0  (matches _SCHEMA_SQL in store.py)
    MySQL:   TINYINT(1) NOT NULL DEFAULT 0  (matches _SCHEMA_SQL_MYSQL in store.py)
    """
    if _column_exists('agents', 'disabled'):
        return  # idempotent — already exists

    bind = op.get_bind()
    if bind.dialect.name == 'mysql':
        col_type = mysql.TINYINT(display_width=1)
    else:
        col_type = sa.Integer()

    op.add_column(
        'agents',
        sa.Column('disabled', col_type, nullable=False, server_default='0'),
    )


def downgrade() -> None:
    """Downgrade schema — drop disabled column from agents table.

    NOTE: SQLite 3.35.0+ supports DROP COLUMN natively.
    For older SQLite versions, this downgrade will fail;
    in that case the migration should be treated as irreversible.
    """
    if _column_exists('agents', 'disabled'):
        op.drop_column('agents', 'disabled')