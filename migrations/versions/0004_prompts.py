"""prompts and prompt_versions tables (prompt registry and versioning)

Revision ID: 0004
Revises: 0003
Create Date: 2026-07-10
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "prompts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False, unique=True),
        sa.Column("active_version_id", sa.Integer(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_table(
        "prompt_versions",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("prompt_id", sa.Integer(), sa.ForeignKey("prompts.id"), nullable=False),
        sa.Column("version_num", sa.Integer(), nullable=False),
        sa.Column("template", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("created_by", sa.String(length=255), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_foreign_key(
        "fk_prompts_active_version_id",
        "prompts",
        "prompt_versions",
        ["active_version_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_prompts_active_version_id", "prompts", type_="foreignkey")
    op.drop_table("prompt_versions")
    op.drop_table("prompts")
