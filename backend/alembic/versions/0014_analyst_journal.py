"""Phase 19 (analyst journal + notes + cockpit): journal_entries, notes,
cockpit_sessions, cockpit_messages.

`journal_entries` is the ledger of the analyst journal's 3-field quick entry
(`view`/`probability`/`review_date`) plus the progressive-disclosure fields
behind it (`thesis`, `evidence`, `invalidation`, `sizing_note`,
`confidence_note`). `horizon_days` is derived (`review_date - entry_date`)
and stored, not recomputed, so the emitted forecast's horizon never drifts
from the entry that produced it. `forecast_id` is nullable only so a
pack-seeded draft (`source="pack_draft"`, `draft_of_week` set) can exist
before it is signed and emits a `Forecast` row (the journal service writes that link, no
writer exists yet).

`notes` is the research-note/investment-thesis object (`kind` distinguishes
the two), mirroring roadmap §5.3's one-sentence -> evidence progressive
disclosure. `evidence_refs` links out to any of the record kinds the pack
already knows about (`forecast | recommendation | journal_entry |
report_pack | shadow_verdict | url`) — schema only, the notes service writes it.

`cockpit_sessions`/`cockpit_messages` persist the read-only conversational
cockpit (the cockpit service writes them; the cockpit tools are the typed-tool layer they will cite via
`tool_calls`/`citations`). `refused` marks a turn where the assistant
declined a write request — a cockpit correctness signal we want queryable
without parsing `content`.

Revision ID: 0014
Revises: 0013
Create Date: 2026-09-03
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "journal_entries",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("entry_date", sa.Date, nullable=False),
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("view", sa.Text, nullable=False),
        sa.Column("probability", sa.Double, nullable=False),
        sa.Column("review_date", sa.Date, nullable=False),
        sa.Column("horizon_days", sa.Integer, nullable=False),
        sa.Column("subject", sa.String(64), nullable=False),
        sa.Column("thesis", sa.Text, nullable=True),
        sa.Column("evidence", JSONB, nullable=True),  # [{kind, ref, note}]
        sa.Column("invalidation", sa.Text, nullable=True),
        sa.Column("sizing_note", sa.Text, nullable=True),
        sa.Column("confidence_note", sa.Text, nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="open"),
        # open -> reviewed | superseded
        sa.Column("source", sa.String(24), nullable=False, server_default="human"),
        # human | pack_draft
        sa.Column("draft_of_week", sa.String(8), nullable=True),
        sa.Column(
            "forecast_id", sa.Integer,
            sa.ForeignKey("forecasts.id", name="fk_journal_entries_forecast_id"),
            nullable=True,
        ),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("review_note", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.CheckConstraint(
            "probability > 0 AND probability < 1",
            name="ck_journal_entries_probability_range",
        ),
    )
    op.create_index("ix_journal_entries_entry_date", "journal_entries", ["entry_date"])
    op.create_index("ix_journal_entries_status_review_date", "journal_entries", ["status", "review_date"])
    op.create_index("ix_journal_entries_subject", "journal_entries", ["subject"])

    op.create_table(
        "notes",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("slug", sa.String(120), nullable=False, unique=True),
        sa.Column("kind", sa.String(16), nullable=False),
        # research_note | thesis
        sa.Column("title", sa.String(200), nullable=False),
        sa.Column("status", sa.String(16), nullable=False, server_default="draft"),
        # draft | signed
        sa.Column("one_sentence", sa.Text, nullable=False),
        sa.Column("why_it_matters", sa.Text, nullable=True),
        sa.Column("action", sa.Text, nullable=True),
        sa.Column("risk_invalidation", sa.Text, nullable=True),
        sa.Column("body", sa.Text, nullable=True),
        sa.Column("evidence_refs", JSONB, nullable=True),  # [{kind, id, label}]
        sa.Column("subjects", JSONB, nullable=True),  # [str]
        sa.Column("signed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_notes_kind_status", "notes", ["kind", "status"])
    op.create_index("ix_notes_slug", "notes", ["slug"])

    op.create_table(
        "cockpit_sessions",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("session_key", sa.String(64), nullable=False, unique=True),
        # uuid4 hex
        sa.Column("title", sa.String(200), nullable=True),
        # first user message, truncated
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_cockpit_sessions_updated_at", "cockpit_sessions", ["updated_at"])

    op.create_table(
        "cockpit_messages",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "session_id", sa.Integer,
            sa.ForeignKey("cockpit_sessions.id", name="fk_cockpit_messages_session_id"),
            nullable=False,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        # user | assistant | tool
        sa.Column("content", sa.Text, nullable=False),
        sa.Column("tool_calls", JSONB, nullable=True),  # [{tool, args, row_count, record_ids}]
        sa.Column("citations", JSONB, nullable=True),  # [{kind, id}]
        sa.Column("refused", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("job_id", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.text("now()")),
    )
    op.create_index("ix_cockpit_messages_session_id", "cockpit_messages", ["session_id"])
    op.create_index("ix_cockpit_messages_session_id_id", "cockpit_messages", ["session_id", "id"])


def downgrade() -> None:
    op.drop_index("ix_cockpit_messages_session_id_id", table_name="cockpit_messages")
    op.drop_index("ix_cockpit_messages_session_id", table_name="cockpit_messages")
    op.drop_table("cockpit_messages")

    op.drop_index("ix_cockpit_sessions_updated_at", table_name="cockpit_sessions")
    op.drop_table("cockpit_sessions")

    op.drop_index("ix_notes_slug", table_name="notes")
    op.drop_index("ix_notes_kind_status", table_name="notes")
    op.drop_table("notes")

    op.drop_index("ix_journal_entries_subject", table_name="journal_entries")
    op.drop_index("ix_journal_entries_status_review_date", table_name="journal_entries")
    op.drop_index("ix_journal_entries_entry_date", table_name="journal_entries")
    op.drop_table("journal_entries")
