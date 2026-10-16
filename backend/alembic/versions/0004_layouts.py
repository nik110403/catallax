"""Foundations F4: server-side layout/workspace persistence.

Replaces localStorage-only UI state with a backend key→JSON store so a user's
workspaces + per-tab layout presets follow them across devices (laptop ↔ server)
instead of living in one browser. One row per state bucket — `workspaces` and
`presets:<namespace>` — keyed by a short allowlisted string.

Revision ID: 0004
Revises: 0003
Create Date: 2026-06-23

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "layouts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("key", sa.String(128), nullable=False),
        sa.Column("data", JSONB, nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("key", name="uq_layouts_key"),
    )


def downgrade() -> None:
    op.drop_table("layouts")
