"""Phase 12 (AI layer): ai_runs job store + news_intel enriched-article store.

`ai_runs` mirrors `lab_runs`/`backtests` — one row IS both the job and its result;
the API INSERTs status='queued' + SELECTs, the off-API `python -m app.ai_worker`
(the ai-runner container, --profile ai) claims + runs enrichment + writes back.
`news_intel` holds one row per enriched article, upserted idempotently on `link`.

Revision ID: 0009
Revises: 0008
Create Date: 2026-07-09
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "ai_runs",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("job_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(16), nullable=False, server_default="enrich"),
        sa.Column("config", JSONB, nullable=False, server_default="{}"),
        sa.Column("status", sa.String(16), nullable=False, server_default="queued"),
        sa.Column("progress", sa.Double, nullable=False, server_default="0"),
        sa.Column("phase", sa.String(48), nullable=True),
        sa.Column("result", JSONB, nullable=True),
        sa.Column("error", JSONB, nullable=True),
        sa.Column("warnings", JSONB, nullable=False, server_default="[]"),
        sa.Column("worker_id", sa.String(96), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("job_id", name="uq_ai_runs_job_id"),
    )
    op.create_index("ix_ai_runs_status", "ai_runs", ["status"])
    op.create_index("ix_ai_runs_created_at", "ai_runs", ["created_at"])

    op.create_table(
        "news_intel",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("link", sa.String(1024), nullable=False),
        sa.Column("title", sa.String(512), nullable=False),
        sa.Column("source", sa.String(128), nullable=True),
        sa.Column("published", sa.DateTime(timezone=True), nullable=True),
        sa.Column("sentiment", sa.String(16), nullable=True),      # positive|neutral|negative
        sa.Column("sentiment_score", sa.Double, nullable=True),    # -1..1
        sa.Column("topics", JSONB, nullable=False, server_default="[]"),
        sa.Column("country", sa.String(64), nullable=True),
        sa.Column("lat", sa.Double, nullable=True),
        sa.Column("lon", sa.Double, nullable=True),
        sa.Column("summary", sa.Text, nullable=True),
        sa.Column("cluster_id", sa.String(36), nullable=True),
        sa.Column("grounding", sa.String(16), nullable=False, server_default="unverifiable"),
        sa.Column("enriched_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint("link", name="uq_news_intel_link"),
    )
    op.create_index("ix_news_intel_published", "news_intel", ["published"])
    op.create_index("ix_news_intel_cluster", "news_intel", ["cluster_id"])


def downgrade() -> None:
    op.drop_table("news_intel")
    op.drop_table("ai_runs")
