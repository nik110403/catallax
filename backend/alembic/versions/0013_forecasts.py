"""Phase 18 (decision objects + calibration): forecasts + outcomes;
shadow_verdicts.horizon_days.

`forecasts` is the append-only ledger of every issued, immutable forecast —
one row per `(issued_by, subject, decision_date, horizon_days)` (the unique
constraint makes a weekly re-issue idempotent and backs the rolling-issuance lookup).
`issued_by` identifies the writer (`shadow:<tool>` or `human` this phase;
`advisor:<rule_id>` is reserved for Phase 19 — no writer produces it yet).
`statement` is a sentence a stranger can judge true/false without more
context; `target`/`resolution_rule` together are everything the resolver
needs to grade it and nothing else.

The **PIT (point-in-time) triple** — `published_at` (when the underlying
evidence came into existence: for a `daily_bars:` forecast, the timestamp of
the last daily bar the reading used, NOT the fetch time), `first_seen_at`
(when Catallax's worker tick first observed that evidence) and `available_at`
(when the forecast row itself became usable by any downstream decision — its
persistence time) — plus `resolution_source` is the point-in-time contract
that Phase 20's `market_events` extends; it is defined once, here. The
invariant `published_at <= first_seen_at <= available_at` is enforced both
in the issuing service and by a CHECK constraint here, since this is a
correctness property of the ledger, not just of one writer.

`outcomes` is the one-per-forecast immutable resolution (`forecast_id` is
UNIQUE — a forecast resolves once; a later restated bar never re-resolves
it, by policy). `hit`/`brier`/`log_score` are NULL until `resolution_status`
is `matured`, or forever NULL on a push, or when the forecast carried no
`probability` (a directional, non-probabilistic forecast). `attribution`
judges direction only in v1 (`thesis`/`sizing` are the literal string
`"n/a"`, waiting on Phase 19's journal links and executed fills
respectively).

`shadow_verdicts.horizon_days` lets the shadow engine record the grading horizon
per-verdict going forward (28 days); historic rows stay NULL and are read
by the engine's fixed `_GRADE_HORIZON_WEEKS * 7` constant.

Revision ID: 0013
Revises: 0012
Create Date: 2026-09-02
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "forecasts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("issued_by", sa.String(32), nullable=False),
        # "shadow:<tool>" | "human"; "advisor:<rule_id>" reserved.
        sa.Column("issuer_version", sa.String(64), nullable=False),
        sa.Column("subject_kind", sa.String(16), nullable=False),
        # symbol | macro | portfolio; "rule" reserved.
        sa.Column("subject", sa.String(64), nullable=False),
        sa.Column("statement", sa.Text, nullable=False),
        sa.Column("kind", sa.String(16), nullable=False),
        # binary in v1; point/interval reserved.
        sa.Column("probability", sa.Double, nullable=True),
        sa.Column("distribution", JSONB, nullable=True),  # reserved
        sa.Column("direction", sa.SmallInteger, nullable=True),  # +1/-1, price forecasts
        sa.Column("target", JSONB, nullable=False),
        sa.Column("horizon_days", sa.Integer, nullable=False),
        sa.Column("decision_date", sa.Date, nullable=False),
        sa.Column("due_date", sa.Date, nullable=False),
        sa.Column("report_week", sa.String(8), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolution_source", sa.String(32), nullable=False),
        # "daily_bars:<adapter-source>" | "manual"
        sa.Column("resolution_rule", JSONB, nullable=False),
        sa.Column("evidence_refs", JSONB, nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        # pending -> resolved | abstained | void
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.UniqueConstraint(
            "issued_by", "subject", "decision_date", "horizon_days",
            name="uq_forecasts_issuer_subject_date_horizon",
        ),
        sa.CheckConstraint(
            "published_at <= first_seen_at AND first_seen_at <= available_at",
            name="ck_forecasts_pit_order",
        ),
        sa.CheckConstraint(
            "probability IS NULL OR (probability >= 0 AND probability <= 1)",
            name="ck_forecasts_probability_range",
        ),
    )
    op.create_index("ix_forecasts_status", "forecasts", ["status"])
    op.create_index("ix_forecasts_due_date", "forecasts", ["due_date"])
    op.create_index("ix_forecasts_report_week", "forecasts", ["report_week"])
    op.create_index(
        "ix_forecasts_issued_by_subject_status", "forecasts",
        ["issued_by", "subject", "status"],
    )

    op.create_table(
        "outcomes",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "forecast_id", sa.Integer,
            sa.ForeignKey("forecasts.id", name="fk_outcomes_forecast_id"),
            nullable=False, unique=True,
        ),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolution_status", sa.String(16), nullable=False),
        # matured | uncovered | manual
        sa.Column("realized", JSONB, nullable=False),
        sa.Column("hit", sa.Boolean, nullable=True),
        sa.Column("brier", sa.Double, nullable=True),
        sa.Column("log_score", sa.Double, nullable=True),
        sa.Column("attribution", JSONB, nullable=False),
        sa.Column("resolver_version", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )

    op.add_column(
        "shadow_verdicts",
        sa.Column("horizon_days", sa.Integer, nullable=True, server_default=None),
    )


def downgrade() -> None:
    op.drop_column("shadow_verdicts", "horizon_days")
    op.drop_table("outcomes")
    op.drop_index("ix_forecasts_issued_by_subject_status", table_name="forecasts")
    op.drop_index("ix_forecasts_report_week", table_name="forecasts")
    op.drop_index("ix_forecasts_due_date", table_name="forecasts")
    op.drop_index("ix_forecasts_status", table_name="forecasts")
    op.drop_table("forecasts")
