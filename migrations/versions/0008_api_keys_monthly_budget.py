"""api_keys monthly budget cap

Revision ID: 0008
Revises: 0007
Create Date: 2026-07-23
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


def downgrade() -> None:
    op.drop_column("api_keys", "monthly_budget_usd")
