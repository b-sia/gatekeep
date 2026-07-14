"""eval gate tables + request-sample corpus + request_logs prompt/routing columns

Revision ID: 0007
Revises: 0006
Create Date: 2026-07-14
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "request_logs",
        sa.Column("prompt_name", sa.String(length=255), nullable=True),
    )
    op.add_column(
        "request_logs",
        sa.Column("routed_from", sa.String(length=255), nullable=True),
    )
    op.create_table(
        "request_samples",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("key_id", sa.Integer(), sa.ForeignKey("api_keys.id"), nullable=False),
        sa.Column("prompt_name", sa.String(length=255), nullable=True),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("input_messages", postgresql.JSONB(), nullable=False),
        sa.Column("output_text", sa.Text(), nullable=False),
    )
    op.create_table(
        "eval_suites",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("prompt_name", sa.String(length=255), nullable=False, unique=True),
        sa.Column("pass_threshold", sa.Float(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_table(
        "eval_cases",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "suite_id", sa.Integer(), sa.ForeignKey("eval_suites.id"), nullable=False
        ),
        sa.Column("input_messages", postgresql.JSONB(), nullable=False),
        sa.Column("expected", sa.Text(), nullable=True),
        sa.Column("check_type", sa.String(length=32), nullable=False),
        sa.Column("judge_criteria", sa.Text(), nullable=True),
        sa.Column("reviewed", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "source", sa.String(length=32), nullable=False, server_default="manual"
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_table(
        "eval_runs",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "suite_id", sa.Integer(), sa.ForeignKey("eval_suites.id"), nullable=False
        ),
        sa.Column(
            "prompt_version_id",
            sa.Integer(),
            sa.ForeignKey("prompt_versions.id"),
            nullable=False,
        ),
        sa.Column("model", sa.String(length=255), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("report", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("eval_runs")
    op.drop_table("eval_cases")
    op.drop_table("eval_suites")
    op.drop_table("request_samples")
    op.drop_column("request_logs", "routed_from")
    op.drop_column("request_logs", "prompt_name")
