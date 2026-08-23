"""composite btree on cached_responses (account_id, model, created_at) for semantic-cache lookups

Revision ID: 0022
Revises: 0021
Create Date: 2026-08-22

find_semantic_match (gatekeep/middleware/cache_semantic.py) filters to a single
tenant+model's non-expired rows (account_id = ?, model = ?, created_at >= cutoff)
and only then sorts that slice by cosine distance. Without an index on the filter
columns, that filter is a full sequential scan of the whole table whose cost grows
with total rows across all tenants (issue #26).

This btree lets Postgres narrow to exactly that one partition first (equality
columns account_id, model lead; the created_at range column comes last so the
cutoff bound is still index-served), after which the exact cosine sort runs over
just the matching rows. An exact sort can never miss a valid match, unlike an
approximate vector index (ivfflat/HNSW), whose nearest-centroid results get
post-filtered by tenant and can silently drop a real hit. If a single tenant+model
partition ever grows large enough that the exact sort is too slow, revisit with a
filtered ANN approach (e.g. HNSW + pgvector iterative index scans).

Built with CREATE INDEX CONCURRENTLY so the build does not hold a lock that blocks
cache writes for its whole duration - important on the potentially large table
issue #26 describes. CONCURRENTLY cannot run inside a transaction, so the
statements run in an autocommit block.
"""

from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(
            "CREATE INDEX CONCURRENTLY "
            "ix_cached_responses_account_model_created_at "
            "ON cached_responses (account_id, model, created_at)"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute("DROP INDEX CONCURRENTLY IF EXISTS ix_cached_responses_account_model_created_at")
