"""add cached_responses.max_tokens and .stop_sequences

Revision ID: 0023
Revises: 0022
Create Date: 2026-08-23

find_semantic_match (gatekeep/middleware/cache_semantic.py) previously matched
on embedding similarity + account_id + model only, unlike the exact cache
(gatekeep/middleware/cache_exact.py's hash_request), which also keys on
max_tokens and stop_sequences because they bound what a valid cached response
can look like: a response truncated under a low max_tokens must not be served
to a request that allows a longer completion, and a response that never hit a
stop_sequence must not be served to a request that would have stopped it
early. This adds the columns needed to enforce the same rule in the semantic
cache (issue #29, sub-finding 2).
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the nullable `max_tokens`/`stop_sequences` columns.

    Nullable so rows written before this migration stay NULL - what
    constraints they were generated under is unknowable after the fact.
    find_semantic_match excludes NULL-max_tokens rows from matching rather
    than treating them as unconstrained. A plain `ADD COLUMN ... NULL` is a
    fast metadata-only change on Postgres, so no autocommit_block is needed.
    """
    op.add_column("cached_responses", sa.Column("max_tokens", sa.Integer(), nullable=True))
    op.add_column(
        "cached_responses", sa.Column("stop_sequences", postgresql.JSONB(), nullable=True)
    )


def downgrade() -> None:
    """Drop the `max_tokens`/`stop_sequences` columns."""
    op.drop_column("cached_responses", "stop_sequences")
    op.drop_column("cached_responses", "max_tokens")
