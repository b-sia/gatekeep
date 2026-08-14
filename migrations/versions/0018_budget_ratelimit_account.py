"""drop api_keys.monthly_budget_usd (budget pooled at the account, decision 5)

Revision ID: 0018
Revises: 0017
Create Date: 2026-08-14

The account budget pool was seeded from each key's cap in migration 0014, so
the per-key column is now redundant and is removed. Rate limiting moves to an
account-keyed Redis bucket, which is application state with no schema change.
"""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("api_keys", "monthly_budget_usd")


def downgrade() -> None:
    op.add_column("api_keys", sa.Column("monthly_budget_usd", sa.Float(), nullable=True))
    op.execute(
        """
        UPDATE api_keys SET monthly_budget_usd = accounts.monthly_budget_usd
        FROM accounts WHERE api_keys.account_id = accounts.id
        """
    )
