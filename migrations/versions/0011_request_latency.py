"""add latency columns (duration_ms, provider_ms, ttft_ms) to request_logs

Revision ID: 0011
Revises: 0010
Create Date: 2026-07-31
"""

import sqlalchemy as sa
from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("request_logs", sa.Column("duration_ms", sa.Float(), nullable=True))
    op.add_column("request_logs", sa.Column("provider_ms", sa.Float(), nullable=True))
    op.add_column("request_logs", sa.Column("ttft_ms", sa.Float(), nullable=True))


def downgrade() -> None:
    op.drop_column("request_logs", "ttft_ms")
    op.drop_column("request_logs", "provider_ms")
    op.drop_column("request_logs", "duration_ms")
