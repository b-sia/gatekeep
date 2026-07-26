"""index cached_responses(model, prompt_version_num) for A/B-scoped semantic cache lookups

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-26
"""

from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_cached_responses_model_prompt_version_num",
        "cached_responses",
        ["model", "prompt_version_num"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_cached_responses_model_prompt_version_num", table_name="cached_responses"
    )
