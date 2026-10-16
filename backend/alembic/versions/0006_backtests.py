"""Phase 9 (engine): the backtester job + result store.

One table — `backtests` — is BOTH the job record and the result record (1:1). The
FastAPI API process only ever INSERTs a row (status='queued') and SELECTs; the
off-API `python -m app.worker` process claims a queued row (atomic UPDATE), runs
the engine, and writes the result back. The worker is structurally READ-ONLY on
the F0 `daily_bars` / `signals` backbone (it only reads them via HistoryStore +
SELECT) and writes ONLY this table — a worker bug can never corrupt F0.

`job_id` is a server-minted UUID (the public handle in every route + the Redis
progress channel name) so a client can never subscribe-hijack another job's
stream on the no-auth LAN. `config`/`result`/`error`/`warnings` are JSONB.

Revision ID: 0006
Revises: 0005
Create Date: 2026-06-24

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "backtests",
        sa.Column("id", sa.Integer, primary_key=True),
        # Public handle (route param + Redis progress channel). Server-minted UUID
        # so a client cannot guess/subscribe to another job's stream.
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("engine", sa.String(32), nullable=False),
        # The signal family the strategy ranks (the join surface to deliverable #1).
        sa.Column("signal_id", sa.String(64), nullable=False),
        # The full validated BacktestConfig (reproducibility).
        sa.Column("config", JSONB, nullable=False),
        # queued -> running -> done | failed.
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("progress", sa.Double, nullable=False, server_default="0"),
        sa.Column("phase", sa.String(48), nullable=True),
        # Set by the worker that claimed the job; heartbeat_at lets a boot-time
        # sweep reclaim a job whose worker died mid-run (crash recovery).
        sa.Column("worker_id", sa.String(64), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        # The BacktestReport (equity/drawdown/metrics/warnings); null until done.
        sa.Column("result", JSONB, nullable=True),
        # The CatallaxError envelope on failure; null otherwise.
        sa.Column("error", JSONB, nullable=True),
        sa.Column("warnings", JSONB, nullable=False, server_default="[]"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            onupdate=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint("job_id", name="uq_backtests_job_id"),
    )
    # List view (newest first) + the worker's queued-job scan.
    op.create_index("ix_backtests_status", "backtests", ["status"])
    op.create_index("ix_backtests_created_at", "backtests", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_backtests_created_at", table_name="backtests")
    op.drop_index("ix_backtests_status", table_name="backtests")
    op.drop_table("backtests")
