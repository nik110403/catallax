"""Phase 16 (portfolio-ops): portfolio_snapshots + recommendations.

`portfolio_snapshots` is the weekly-advisor persistence layer — one row per
held symbol per `report_week` (the idempotent weekly key), denormalized with
the account-level cash/nav/regime figures for that snapshot so the row is
self-contained. `cost_basis` is nullable until the exporter patch lands
cost basis on `/holdings`. The (report_week, symbol) unique constraint makes
a re-run on the same week a no-op, matching the design's catch-up-on-boot
requirement.

`recommendations` is append-only by policy (audit trail): the service layer
never UPDATEs a row — a status transition (evaluated -> proposed -> accepted
-> overridden -> executed) INSERTs a new row whose `prior_id` points at the
row it supersedes. `prior_id` is a nullable self-FK, NULL on the first
`evaluated` row of a rule's lifecycle.

Revision ID: 0011
Revises: 0010
Create Date: 2026-08-15
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "portfolio_snapshots",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("report_week", sa.String(16), nullable=False),
        sa.Column("date", sa.Date, nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("qty", sa.Numeric(24, 8), nullable=False),
        sa.Column("value_eur", sa.Numeric(24, 8), nullable=False),
        sa.Column("cost_basis", sa.Numeric(24, 8), nullable=True),
        sa.Column("cash", sa.Numeric(24, 8), nullable=False),
        sa.Column("nav", sa.Numeric(24, 8), nullable=False),
        sa.Column("regime_composite", sa.Double, nullable=True),
        sa.Column("regime_component_set_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("report_week", "symbol", name="uq_portfolio_snapshots_week_symbol"),
    )
    op.create_index("ix_portfolio_snapshots_report_week", "portfolio_snapshots", ["report_week"])
    op.create_index("ix_portfolio_snapshots_date", "portfolio_snapshots", ["date"])
    op.create_index("ix_portfolio_snapshots_symbol", "portfolio_snapshots", ["symbol"])

    op.create_table(
        "recommendations",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("rule_id", sa.String(64), nullable=False),
        sa.Column("as_of", sa.Date, nullable=False),
        sa.Column("report_week", sa.String(16), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("sizing", sa.Double, nullable=True),
        sa.Column("input_snapshot", JSONB, nullable=False),
        sa.Column("regime_component_set_id", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="evaluated"),
        # evaluated -> proposed -> accepted -> overridden -> executed
        sa.Column("tax_cost_arithmetic", JSONB, nullable=True),
        sa.Column("note", sa.Text, nullable=True),
        sa.Column(
            "prior_id",
            sa.Integer,
            sa.ForeignKey("recommendations.id", name="fk_recommendations_prior_id"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_recommendations_rule_id", "recommendations", ["rule_id"])
    op.create_index("ix_recommendations_report_week", "recommendations", ["report_week"])
    op.create_index("ix_recommendations_status", "recommendations", ["status"])
    op.create_index("ix_recommendations_prior_id", "recommendations", ["prior_id"])


def downgrade() -> None:
    op.drop_table("recommendations")
    op.drop_table("portfolio_snapshots")
