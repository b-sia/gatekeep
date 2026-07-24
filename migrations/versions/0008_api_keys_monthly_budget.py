"""api_keys monthly budget cap

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-23

Also adds a request_logs (key_id, created_at) index to support the budget
enforcement DB-fallback aggregate query, since both changes ship as part of
the same budgets feature.
"""

import sqlalchemy as sa
from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "api_keys",
        sa.Column("monthly_budget_usd", sa.Float(), nullable=True),
    )
    op.create_index(
        "ix_request_logs_key_id_created_at",
        "request_logs",
        ["key_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_request_logs_key_id_created_at", table_name="request_logs")
    op.drop_column("api_keys", "monthly_budget_usd")
