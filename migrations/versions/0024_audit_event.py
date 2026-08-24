"""audit_events append-only fleet-wide audit log

Revision ID: 0024
Revises: 0023
Create Date: 2026-08-24

Adds the generic audit_events table. Prompt and eval mutations are its
first producers; account/key producers can be added later with no schema
change. Append-only: no update/delete paths in the application.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("actor_account_id", sa.Integer(), nullable=True),
        sa.Column("actor_label", sa.String(length=255), nullable=False),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_ref", sa.String(length=255), nullable=True),
        sa.Column("version_num", sa.Integer(), nullable=True),
        sa.Column("result", sa.String(length=16), nullable=False),
        sa.Column("details", JSONB(), nullable=False, server_default="{}"),
    )
    op.create_foreign_key(
        "fk_audit_events_actor_account_id",
        "audit_events",
        "accounts",
        ["actor_account_id"],
        ["id"],
    )
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])
    op.create_index(
        "ix_audit_events_entity",
        "audit_events",
        ["entity_type", "entity_ref", "created_at"],
    )
    op.create_index(
        "ix_audit_events_action_created_at",
        "audit_events",
        ["action", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_audit_events_action_created_at", table_name="audit_events")
    op.drop_index("ix_audit_events_entity", table_name="audit_events")
    op.drop_index("ix_audit_events_created_at", table_name="audit_events")
    op.drop_constraint("fk_audit_events_actor_account_id", "audit_events", type_="foreignkey")
    op.drop_table("audit_events")
