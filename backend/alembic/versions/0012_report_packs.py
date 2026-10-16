"""Phase 17 (weekly report pack): report_packs + shadow_verdicts.

`report_packs` is the per-ISO-week job/status row the report-worker owns:
`report_week` is the PK (the idempotent weekly key, matching Phase 16's
`portfolio_snapshots`/`recommendations` convention), `status` tracks the
build lifecycle (building|complete|failed), `attempts` is the retry
counter — the worker stops retrying a week once it hits
`REPORT_PACK_MAX_ATTEMPTS` (config, default 5), leaving a terminal `failed`
row that closes the gate for that week. `sections`/`sources` are the
assembled report body and its per-chapter provenance (engine, fetched_at,
stale flag, error) respectively.

`shadow_verdicts` is the per-(week, symbol, tool) grading row laid down by
a later task for shadow-mode tool grading; `direction` is nullable
because not every verdict is gradeable (NULL = ungradeable). Schema only
here — no writer exists yet.

Revision ID: 0012
Revises: 0011
Create Date: 2026-08-16
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "report_packs",
        sa.Column("report_week", sa.String(8), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("status", sa.String(16), nullable=False, server_default="building"),
        # building -> complete | failed
        sa.Column("attempts", sa.Integer, nullable=False, server_default="0"),
        sa.Column("sections", JSONB, nullable=True),
        sa.Column("sources", JSONB, nullable=True),
        sa.Column("pdf_path", sa.String, nullable=True),
        sa.Column("narrated", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("factor_run_id", sa.String, nullable=True),
        sa.Column("intel_job_id", sa.String, nullable=True),
    )

    op.create_table(
        "shadow_verdicts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("report_week", sa.String(8), nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        sa.Column("tool", sa.String(32), nullable=False),
        sa.Column("verdict", sa.String(16), nullable=False),
        # add_watch | hold | trim_watch | flag
        sa.Column("direction", sa.SmallInteger, nullable=True),
        # +1/-1 grading direction, NULL = ungradeable
        sa.Column("reading", JSONB, nullable=False),
        sa.Column("rationale", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("report_week", "symbol", "tool", name="uq_shadow_verdicts_week_symbol_tool"),
    )
    op.create_index("ix_shadow_verdicts_report_week", "shadow_verdicts", ["report_week"])


def downgrade() -> None:
    op.drop_table("shadow_verdicts")
    op.drop_table("report_packs")
