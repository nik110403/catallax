"""Demo sample-seed marker: a single-row table `app.demo_seed` writes only on a database
that held no rows in any seeded table. The hosted demo backend refuses to serve sample
reads unless the row exists and the DB still matches it (fail closed). The table is created
everywhere but stays empty outside the demo.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-16
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "demo_seed_marker",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("counts", JSONB, nullable=False),
        sa.Column("seeded_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("id = 1", name="ck_demo_seed_marker_single"),
    )


def downgrade() -> None:
    op.drop_table("demo_seed_marker")
