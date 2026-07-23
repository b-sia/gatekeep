"""prompt A/B candidate traffic split + version tagging on logs/cache

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
        "prompts",
        sa.Column("candidate_version_id", sa.Integer(), nullable=True),
    )
    op.create_foreign_key(
        "fk_prompts_candidate_version_id",
        "prompts",
        "prompt_versions",
        ["candidate_version_id"],
        ["id"],
    )
    op.add_column(
        "prompts",
        sa.Column("candidate_traffic_pct", sa.Float(), nullable=True),
    )
    op.add_column(
        "request_logs",
        sa.Column("prompt_version_num", sa.Integer(), nullable=True),
    )
    op.add_column(
        "cached_responses",
        sa.Column("prompt_version_num", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("cached_responses", "prompt_version_num")
    op.drop_column("request_logs", "prompt_version_num")
    op.drop_constraint("fk_prompts_candidate_version_id", "prompts", type_="foreignkey")
    op.drop_column("prompts", "candidate_traffic_pct")
    op.drop_column("prompts", "candidate_version_id")
