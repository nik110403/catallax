"""Phase 9 (deliverable #1): the signal self-scoring core.

One immutable table — `signals` — logging emitted cross-sectional scores. A signal
row is "at decision date T, this symbol scored `value` (higher = more bullish)".
Grades are NOT stored: they are recomputed lazily from the F0 daily-history backbone
(forward returns of a matured, settled window), so a derived number can never go
stale against a restated bar. The unique (signal_id, as_of_date, symbol) key makes
re-emitting a cross-section an idempotent upsert.

Revision ID: 0005
Revises: 0004
Create Date: 2026-06-24

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "signals",
        sa.Column("id", sa.Integer, primary_key=True),
        # The signal family / producer (a strategy or scanner), e.g. "xs_mom_252_21".
        sa.Column("signal_id", sa.String(64), nullable=False),
        # Decision date T: the last close the emitter could observe. Entry is the
        # NEXT bar (strictly > T) — the lookahead guard lives in the grader.
        sa.Column("as_of_date", sa.Date, nullable=False),
        sa.Column("symbol", sa.String(32), nullable=False),
        # Which F0 price adapter feeds this symbol's forward return (part of identity:
        # the same ticker on two sources is two series).
        sa.Column("source", sa.String(32), nullable=False),
        # Raw cross-sectional score, sign-normalised so higher => higher expected
        # forward return. A score, not money → DOUBLE PRECISION (no Decimal churn).
        sa.Column("value", sa.Double, nullable=False),
        # Emission params for reproducibility (lookback, skip, z-score, …). Never
        # read by the grader.
        sa.Column("meta", JSONB, nullable=False, server_default="{}"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        # Idempotency: re-running a generator upserts one row per (family, date, symbol).
        sa.UniqueConstraint(
            "signal_id", "as_of_date", "symbol", name="uq_signals_id_date_symbol"
        ),
    )
    # Per-cross-section read (grade scans one signal_id across its dates).
    op.create_index("ix_signals_id_date", "signals", ["signal_id", "as_of_date"])
    # F0 join (read the right daily-bar series per symbol/source).
    op.create_index("ix_signals_symbol_source", "signals", ["symbol", "source"])


def downgrade() -> None:
    op.drop_index("ix_signals_symbol_source", table_name="signals")
    op.drop_index("ix_signals_id_date", table_name="signals")
    op.drop_table("signals")
