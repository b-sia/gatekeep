"""add request_logs.provider

Revision ID: 0021
Revises: 0020
Create Date: 2026-08-20

Records the resolved upstream provider (`resolve_route`'s first return value:
"anthropic"/"openai"/"google"/"ollama") each request was billed against.
Pricing is keyed "<provider>/<model>", so `model` alone cannot tell which
provider a cost figure was priced under when a bare model id exists under two
providers - this column makes cost audits and the dashboard unambiguous.
"""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Add the nullable `provider` column.

    Nullable so rows written before this migration stay NULL - their provider
    is unknowable after the fact. As with `outcome` (migration 0013), no index
    accompanies it and no `autocommit_block` is needed: a plain
    `ADD COLUMN ... NULL` is a fast metadata-only change on Postgres.
    """
    op.add_column("request_logs", sa.Column("provider", sa.String(32), nullable=True))


def downgrade() -> None:
    """Drop the `provider` column."""
    op.drop_column("request_logs", "provider")
