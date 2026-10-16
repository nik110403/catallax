"""Baseline schema: holdings, watchlist, price_cache.

Minimal columns to prove the migration pipeline; expanded in Phase 2.

Revision ID: 0001
Revises:
Create Date: 2026-06-11

"""
from alembic import op
import sqlalchemy as sa

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "holdings",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("quantity", sa.Numeric(24, 8), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("account", sa.String(128), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint("symbol", "account", name="uq_holdings_symbol_account"),
    )

    op.create_table(
        "watchlist",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("symbol", sa.String(32), nullable=False, unique=True),
        sa.Column("label", sa.String(128)),
        sa.Column(
            "added_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )

    op.create_table(
        "price_cache",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("price", sa.Numeric(24, 8), nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("as_of", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "fetched_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "symbol", "source", "as_of", name="uq_price_cache_symbol_source_as_of"
        ),
    )
    op.create_index("ix_price_cache_symbol", "price_cache", ["symbol"])


def downgrade() -> None:
    op.drop_index("ix_price_cache_symbol", table_name="price_cache")
    op.drop_table("price_cache")
    op.drop_table("watchlist")
    op.drop_table("holdings")
