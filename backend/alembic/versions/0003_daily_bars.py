"""Foundations F0: daily-bar history store (the quant data backbone).

`price_cache` is a write-through log of point-in-time *quotes* (one row per
`/prices` hit, `as_of` ≈ now). It is the wrong shape for risk/backtest/ML/signal
code, which needs clean daily OHLC bars. `daily_bars` is that store: one row per
`(symbol, source, bar_date)`, backfilled idempotently from an adapter's
`get_history` and read back without re-hitting upstream.

Revision ID: 0003
Revises: 0002
Create Date: 2026-06-23

"""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "daily_bars",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("asset_type", sa.String(16), nullable=True),
        sa.Column("bar_date", sa.Date, nullable=False),
        sa.Column("open", sa.Numeric(24, 8), nullable=False),
        sa.Column("high", sa.Numeric(24, 8), nullable=False),
        sa.Column("low", sa.Numeric(24, 8), nullable=False),
        sa.Column("close", sa.Numeric(24, 8), nullable=False),
        sa.Column("volume", sa.Numeric(24, 8), nullable=True),
        sa.Column("currency", sa.String(8), nullable=True),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "symbol", "source", "bar_date", name="uq_daily_bars_symbol_source_date"
        ),
    )
    # Range scans are always (symbol, source) filtered then ordered by date.
    op.create_index(
        "ix_daily_bars_symbol_source_date",
        "daily_bars",
        ["symbol", "source", "bar_date"],
    )


def downgrade() -> None:
    op.drop_index("ix_daily_bars_symbol_source_date", table_name="daily_bars")
    op.drop_table("daily_bars")
