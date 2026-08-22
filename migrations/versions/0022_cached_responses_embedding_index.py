"""ivfflat vector index on cached_responses.embedding for semantic-cache lookups

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-22

find_semantic_match (gatekeep/middleware/cache_semantic.py) orders by cosine
distance against every non-expired row for an account+model on every
cache-miss request. Without a vector index, pgvector has no way to do that
except a full sequential scan computing cosine distance row by row, and the
scan cost grows with the table (issue #26). `lists=100` is a reasonable
starting point per the pgvector docs (~sqrt(row_count) is the usual guidance)
and can be tuned by a later migration once real row counts are known.
"""

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "CREATE INDEX ix_cached_responses_embedding_cosine "
        "ON cached_responses "
        "USING ivfflat (embedding vector_cosine_ops) "
        "WITH (lists = 100)"
    )


def downgrade() -> None:
    op.drop_index("ix_cached_responses_embedding_cosine", table_name="cached_responses")
