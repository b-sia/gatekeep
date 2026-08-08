"""add request_logs.outcome

Revision ID: 0013
Revises: 0012
Create Date: 2026-08-08
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the nullable `outcome` column.

    Nullable so pre-migration rows stay NULL. Unlike `path` (migration
    0012), no index accompanies this column and no `autocommit_block` is
    needed - a plain `ADD COLUMN ... NULL` is a fast metadata-only change
    on Postgres, not a full table rewrite.
    """
    op.add_column("request_logs", sa.Column("outcome", sa.String(32), nullable=True))


def downgrade() -> None:
    """Drop the `outcome` column."""
    op.drop_column("request_logs", "outcome")
