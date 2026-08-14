"""accounts table and api_keys.account_id

Revision ID: 0014
Revises: 0013
Create Date: 2026-08-14

Introduces the tenancy layer (decisions 5, 7, 8): one account per existing
key, account_id backfilled then tightened to NOT NULL, api_keys.name made
unique per (account_id, name), and each key's monthly budget copied onto its
new account (enforcement flips to the account pool in migration 0018).
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "accounts",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("monthly_budget_usd", sa.Float(), nullable=True),
        sa.Column("is_operator", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.add_column("api_keys", sa.Column("account_id", sa.Integer(), nullable=True))
    op.create_foreign_key("fk_api_keys_account_id", "api_keys", "accounts", ["account_id"], ["id"])

    # One account per existing key (decision 8). The account inherits the key's
    # name and monthly budget so today's per-key behavior is reproduced exactly.
    op.execute(
        """
        INSERT INTO accounts (name, monthly_budget_usd, is_operator, created_at)
        SELECT name, monthly_budget_usd, false, now() FROM api_keys ORDER BY id
        """
    )
    # Pair each key with the account created from it. Both were inserted in id
    # order, so row_number() over each lines them up one-to-one.
    op.execute(
        """
        WITH k AS (
            SELECT id AS key_id, row_number() OVER (ORDER BY id) AS rn FROM api_keys
        ),
        a AS (
            SELECT id AS account_id, row_number() OVER (ORDER BY id) AS rn FROM accounts
        )
        UPDATE api_keys SET account_id = a.account_id
        FROM k JOIN a ON k.rn = a.rn
        WHERE api_keys.id = k.key_id
        """
    )
    op.alter_column("api_keys", "account_id", nullable=False)

    # api_keys.name had no unique constraint before (spec problem 2); add the
    # per-account one (decision 7). There is no global constraint to drop.
    op.create_unique_constraint("uq_api_keys_account_id_name", "api_keys", ["account_id", "name"])


def downgrade() -> None:
    op.drop_constraint("uq_api_keys_account_id_name", "api_keys", type_="unique")
    op.drop_constraint("fk_api_keys_account_id", "api_keys", type_="foreignkey")
    op.drop_column("api_keys", "account_id")
    op.drop_table("accounts")
