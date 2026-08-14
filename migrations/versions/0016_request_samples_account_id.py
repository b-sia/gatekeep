"""request_samples.account_id (denormalized)

Revision ID: 0016
Revises: 0015
Create Date: 2026-08-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("request_samples", sa.Column("account_id", sa.Integer(), nullable=True))
    op.execute(
        """
        UPDATE request_samples SET account_id = api_keys.account_id
        FROM api_keys WHERE request_samples.key_id = api_keys.id
        """
    )
    op.alter_column("request_samples", "account_id", nullable=False)
    op.create_foreign_key(
        "fk_request_samples_account_id",
        "request_samples",
        "accounts",
        ["account_id"],
        ["id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_request_samples_account_id", "request_samples", type_="foreignkey")
    op.drop_column("request_samples", "account_id")
