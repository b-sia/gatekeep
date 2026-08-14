"""eval_cases.account_id provenance tag

Revision ID: 0019
Revises: 0018
Create Date: 2026-08-14

Nullable and not backfilled: existing cases (manual or curated before this
column existed) have no known originating tenant and stay NULL. Only curated
cases created after this migration carry their source sample's account.
"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("eval_cases", sa.Column("account_id", sa.Integer(), nullable=True))
    op.create_foreign_key(
        "fk_eval_cases_account_id", "eval_cases", "accounts", ["account_id"], ["id"]
    )


def downgrade() -> None:
    op.drop_constraint("fk_eval_cases_account_id", "eval_cases", type_="foreignkey")
    op.drop_column("eval_cases", "account_id")
