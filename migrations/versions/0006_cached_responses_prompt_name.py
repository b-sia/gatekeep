"""add prompt_name to cached_responses (prompt-update cache invalidation)

Revision ID: 0006
Revises: 0005
Create Date: 2026-07-10
"""

import sqlalchemy as sa
from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "cached_responses",
        sa.Column("prompt_name", sa.String(length=255), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("cached_responses", "prompt_name")
