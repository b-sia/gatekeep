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

    The index is built `CONCURRENTLY`: a plain `CREATE INDEX` takes a
    `SHARE` lock that blocks writes to `request_logs` - the hottest,
    most append-heavy table in the schema - for the duration of the
    build. `CONCURRENTLY` takes longer and cannot run inside the
    transaction Alembic normally wraps a migration in, hence the
    `autocommit_block`.
    """
    op.add_column("request_logs", sa.Column("path", sa.String(32), nullable=True))
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_request_logs_created_at",
            "request_logs",
            ["created_at"],
            postgresql_concurrently=True,
        )


def downgrade() -> None:
    """Drop the created_at index and the `path` column.

    The index drop also runs `CONCURRENTLY`, for the same reason the
    upgrade's build does: a plain `DROP INDEX` takes the same
    write-blocking lock.
    """
    with op.get_context().autocommit_block():
        op.drop_index(
            "ix_request_logs_created_at",
            table_name="request_logs",
            postgresql_concurrently=True,
        )
    op.drop_column("request_logs", "path")
