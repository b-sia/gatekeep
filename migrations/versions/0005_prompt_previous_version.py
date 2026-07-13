"""add previous_version_id to prompts (real promotion history for rollback)

Revision ID: 0005
Revises: 0004
Create Date: 2026-07-10
"""

import sqlalchemy as sa
from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "prompts",
        sa.Column(
            "previous_version_id",
            sa.Integer(),
            sa.ForeignKey("prompt_versions.id", name="fk_prompts_previous_version_id"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_constraint("fk_prompts_previous_version_id", "prompts", type_="foreignkey")
    op.drop_column("prompts", "previous_version_id")
