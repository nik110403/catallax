"""Phase 11 (Quant/ML Lab): the lab run job + result store.

One table — `lab_runs` — is BOTH the job record and the result record (1:1),
exactly like `backtests`. The FastAPI API process only ever INSERTs a
row (status='queued') and SELECTs; the off-API `python -m app.lab_worker` process
(the heavy `qlib-lab` container, --profile lab) claims a queued row (atomic
UPDATE), runs the factor study / model train-eval, and writes the result back.
The worker is structurally READ-ONLY on the F0 `daily_bars` / `signals` backbone
(it only reads them via HistoryStore + SELECT) and writes ONLY this table — a
lab-worker bug can never corrupt F0.

`kind` discriminates the run type (`factor` | `model`); `engine` is the factory
key (e.g. `sklearn-ensemble`, `qlib-zoo`). `job_id` is a server-minted UUID (the
public handle in every route + the Redis progress channel name) so a client can
never subscribe-hijack another job's stream on the no-auth LAN.
`config`/`result`/`error`/`warnings` are JSONB.

Revision ID: 0008
Revises: 0007
Create Date: 2026-06-29

"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lab_runs",
        sa.Column("id", sa.Integer, primary_key=True),
        # public handle (server-minted UUID) + Redis progress channel name
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),  # factor | model
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("engine", sa.String(32), nullable=False),  # factory key
        sa.Column("config", JSONB, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("progress", sa.Double, nullable=False, server_default="0"),
        sa.Column("phase", sa.String(48), nullable=True),
        sa.Column("worker_id", sa.String(64), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", JSONB, nullable=True),
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
        sa.UniqueConstraint("job_id", name="uq_lab_runs_job_id"),
    )
    op.create_index("ix_lab_runs_status", "lab_runs", ["status"])
    op.create_index("ix_lab_runs_created_at", "lab_runs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_lab_runs_created_at", table_name="lab_runs")
    op.drop_index("ix_lab_runs_status", table_name="lab_runs")
    op.drop_table("lab_runs")
