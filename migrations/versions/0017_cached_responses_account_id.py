"""cached_responses.account_id + per-account exact_hash uniqueness (decision 1)

Revision ID: 0017
Revises: 0016
Create Date: 2026-08-14

Existing rows have no derivable owner (no key_id on the table) and the cache is
disposable (TTL'd, wiped on promotion), so they are deleted rather than
backfilled. Redis exact-cache keys are re-namespaced by the application; stale
global keys simply age out.
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("DELETE FROM cached_responses")
    op.add_column("cached_responses", sa.Column("account_id", sa.Integer(), nullable=False))
    op.create_foreign_key(
        "fk_cached_responses_account_id",
        "cached_responses",
        "accounts",
        ["account_id"],
        ["id"],
    )
    op.drop_constraint("cached_responses_exact_hash_key", "cached_responses", type_="unique")
    op.create_unique_constraint(
        "uq_cached_responses_account_id_exact_hash",
        "cached_responses",
        ["account_id", "exact_hash"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_cached_responses_account_id_exact_hash", "cached_responses", type_="unique"
    )
    op.create_unique_constraint(
        "cached_responses_exact_hash_key", "cached_responses", ["exact_hash"]
    )
    op.drop_constraint("fk_cached_responses_account_id", "cached_responses", type_="foreignkey")
    op.drop_column("cached_responses", "account_id")
