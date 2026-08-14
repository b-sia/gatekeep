"""request_logs.account_id (denormalized)

Revision ID: 0015
Revises: 0014
Create Date: 2026-08-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("request_logs", sa.Column("account_id", sa.Integer(), nullable=True))
    op.execute(
        """
        UPDATE request_logs SET account_id = api_keys.account_id
        FROM api_keys WHERE request_logs.key_id = api_keys.id
        """
    )
    op.alter_column("request_logs", "account_id", nullable=False)
    op.create_foreign_key(
        "fk_request_logs_account_id", "request_logs", "accounts", ["account_id"], ["id"]
    )
    op.create_index(
        "ix_request_logs_account_id_created_at",
        "request_logs",
        ["account_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_request_logs_account_id_created_at", table_name="request_logs")
    op.drop_constraint("fk_request_logs_account_id", "request_logs", type_="foreignkey")
    op.drop_column("request_logs", "account_id")
