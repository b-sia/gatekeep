"""cached_responses table (semantic cache) with pgvector extension

Revision ID: 0003
Revises: 0002
Create Date: 2026-07-10
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "cached_responses",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("exact_hash", sa.String(length=64), nullable=False, unique=True),
        sa.Column("user_messages_text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(384), nullable=False),
        sa.Column("response_text", sa.Text(), nullable=False),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("cached_responses")
