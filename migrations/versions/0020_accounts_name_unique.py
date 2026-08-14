"""accounts.name globally unique

Revision ID: 0020
Revises: 0019
Create Date: 2026-08-14

`Account.name` is the human-facing identifier `gatekeep key set-budget <name>`
looks accounts up by. Migration 0014 minted one account per pre-existing key,
copying the key's (never-unique) name, so duplicates can exist. Before adding
the constraint, every duplicate name is deterministically disambiguated: the
lowest-id account in each name group keeps its name unchanged, and every other
account in that group is renamed to "<name>-<id>" (id is globally unique, so
this can never collide with another account's name).

This is a one-way rename: downgrade drops the constraint but does not restore
the original duplicate names, matching the irreversible-by-design precedent in
migration 0017 (which deletes rather than reconstructs stale cached_responses
rows).
"""

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        WITH ranked AS (
            SELECT id, row_number() OVER (PARTITION BY name ORDER BY id) AS rn
            FROM accounts
        )
        UPDATE accounts
        SET name = accounts.name || '-' || accounts.id
        FROM ranked
        WHERE accounts.id = ranked.id AND ranked.rn > 1
        """
    )
    op.create_unique_constraint("accounts_name_key", "accounts", ["name"])


def downgrade() -> None:
    op.drop_constraint("accounts_name_key", "accounts", type_="unique")
