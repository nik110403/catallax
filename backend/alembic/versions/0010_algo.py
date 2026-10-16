"""Phase 13 (paper-trading algo hub): algo_orders/algo_fills/algo_positions/
algo_strategies/algo_account_snapshots/algo_state.

The API INSERTs orders (manual ticket or strategy runner, status='new') + SELECTs;
the off-API `python -m app.algo_worker` (the trading-engine container, --profile
algo) claims + resolves them, writes fills, and maintains positions/snapshots.
`algo_state` is a tiny key/value DB mirror for the kill switch (survives Redis
flush).

Revision ID: 0010
Revises: 0009
Create Date: 2026-07-13
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "algo_orders",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("strategy_id", sa.Integer, nullable=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("type", sa.String(8), nullable=False, server_default="market"),
        sa.Column("qty", sa.Double, nullable=False),
        sa.Column("limit_price", sa.Double, nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="new"),
        sa.Column("reason", sa.String(256), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_algo_orders_status", "algo_orders", ["status"])
    op.create_index("ix_algo_orders_created_at", "algo_orders", ["created_at"])
    op.create_index("ix_algo_orders_strategy", "algo_orders", ["strategy_id"])

    op.create_table(
        "algo_fills",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("order_id", sa.Integer, nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("side", sa.String(4), nullable=False),
        sa.Column("price", sa.Double, nullable=False),
        sa.Column("qty", sa.Double, nullable=False),
        sa.Column("commission", sa.Double, nullable=False, server_default="0"),
        sa.Column("slippage_bps", sa.Double, nullable=False, server_default="0"),
        sa.Column("filled_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_algo_fills_order", "algo_fills", ["order_id"])
    op.create_index("ix_algo_fills_filled_at", "algo_fills", ["filled_at"])

    op.create_table(
        "algo_positions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("qty", sa.Double, nullable=False, server_default="0"),
        sa.Column("avg_cost", sa.Double, nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("symbol", name="uq_algo_positions_symbol"),
    )

    op.create_table(
        "algo_strategies",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column("config", JSONB, nullable=False, server_default="{}"),
        sa.Column("status", sa.String(16), nullable=False, server_default="paused"),
        sa.Column("last_rebalance_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("name", name="uq_algo_strategies_name"),
    )

    op.create_table(
        "algo_account_snapshots",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("snapshot_date", sa.Date, nullable=False),
        sa.Column("cash", sa.Double, nullable=False),
        sa.Column("positions_value", sa.Double, nullable=False),
        sa.Column("equity", sa.Double, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("snapshot_date", name="uq_algo_snapshots_date"),
    )

    op.create_table(
        "algo_state",
        sa.Column("key", sa.String(32), primary_key=True),
        sa.Column("value", JSONB, nullable=False, server_default="{}"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("algo_state")
    op.drop_table("algo_account_snapshots")
    op.drop_table("algo_strategies")
    op.drop_table("algo_positions")
    op.drop_table("algo_fills")
    op.drop_table("algo_orders")
