"""Phase 10a v1.2: market-breadth daily snapshot store.

The WSJ markets-diary feed exposes only today/prev/week-ago per row — not a
history. Breadth *oscillators* (A/D line, McClellan Oscillator/Summation) need an
accumulated daily series, so we persist one snapshot per (date, market) and build
the series forward. Snapshot indicators (A/D ratio, new highs−lows, up/down
volume, TRIN) need no history and read straight from the latest row.

Revision ID: 0007
Revises: 0006
Create Date: 2026-06-26

"""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "market_breadth",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("breadth_date", sa.Date, nullable=False),
        sa.Column("market", sa.String(24), nullable=False),
        sa.Column("issues", sa.Integer, nullable=True),
        sa.Column("advances", sa.Integer, nullable=True),
        sa.Column("declines", sa.Integer, nullable=True),
        sa.Column("unchanged", sa.Integer, nullable=True),
        sa.Column("new_highs", sa.Integer, nullable=True),
        sa.Column("new_lows", sa.Integer, nullable=True),
        sa.Column("adv_volume", sa.BigInteger, nullable=True),
        sa.Column("decl_volume", sa.BigInteger, nullable=True),
        sa.Column("total_volume", sa.BigInteger, nullable=True),
        sa.Column("trin", sa.Double, nullable=True),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "breadth_date", "market", name="uq_market_breadth_date_market"
        ),
    )
    op.create_index(
        "ix_market_breadth_market_date",
        "market_breadth",
        ["market", "breadth_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_market_breadth_market_date", table_name="market_breadth")
    op.drop_table("market_breadth")
