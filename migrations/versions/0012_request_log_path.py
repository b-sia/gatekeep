"""add request_logs.path and a created_at index

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-05
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the nullable `path` column and the created_at-only index.

    `path` is deliberately not backfilled: any value invented for a
    pre-0012 row would be a guess, and latency queries exclude NULLs.
    """
    op.add_column("request_logs", sa.Column("path", sa.String(32), nullable=True))
    op.create_index("ix_request_logs_created_at", "request_logs", ["created_at"])


def downgrade() -> None:
    """Drop the created_at index and the `path` column."""
    op.drop_index("ix_request_logs_created_at", table_name="request_logs")
    op.drop_column("request_logs", "path")
