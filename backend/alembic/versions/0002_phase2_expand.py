"""Phase 2: expand baseline tables for prices/holdings/watchlist.

Adds columns the Phase 2 features actually use:
- holdings.cost_basis, holdings.updated_at  (Phase 4 portfolio views)
- watchlist.source, watchlist.sort_order     (drives the WS ticker)
- price_cache.asset_type + (symbol, source) index (TTL by asset class)

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-22

"""
from alembic import op
import sqlalchemy as sa

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("holdings", sa.Column("cost_basis", sa.Numeric(24, 8), nullable=True))
    op.add_column(
        "holdings",
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.add_column("watchlist", sa.Column("source", sa.String(32), nullable=True))
    op.add_column(
        "watchlist",
        sa.Column(
            "sort_order",
            sa.Integer,
            nullable=False,
            server_default="0",
        ),
    )

    op.add_column(
        "price_cache", sa.Column("asset_type", sa.String(16), nullable=True)
    )
    op.create_index(
        "ix_price_cache_symbol_source", "price_cache", ["symbol", "source"]
    )


def downgrade() -> None:
    op.drop_index("ix_price_cache_symbol_source", table_name="price_cache")
    op.drop_column("price_cache", "asset_type")
    op.drop_column("watchlist", "sort_order")
    op.drop_column("watchlist", "source")
    op.drop_column("holdings", "updated_at")
    op.drop_column("holdings", "cost_basis")
