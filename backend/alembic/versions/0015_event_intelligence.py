"""Phase 20: event-intelligence persistence only; no runtime activation.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-05
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "market_events",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column("source", sa.String(32), nullable=False),
        sa.Column("feed", sa.String(64), nullable=False),
        sa.Column("source_key", sa.String(255), nullable=False),
        sa.Column("revision", sa.Integer, nullable=False, server_default="1"),
        sa.Column(
            "supersedes_id",
            sa.Integer,
            sa.ForeignKey("market_events.id", name="fk_market_events_supersedes_id"),
        ),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("form_type", sa.String(16)),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("summary", sa.Text),
        sa.Column("url", sa.String(1000), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("first_seen_at", sa.DateTime(timezone=True), nullable=False),
        # A later ingest writer supplies max(first_seen_at, observed clock) explicitly.
        sa.Column(
            "available_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.Column(
            "published_at_estimated",
            sa.Boolean,
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("entity_hints", JSONB, nullable=False, server_default="[]"),
        sa.Column("story_key", sa.String(40)),
        sa.Column("processed_at", sa.DateTime(timezone=True)),
        sa.Column("raw", JSONB, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "source",
            "feed",
            "source_key",
            "revision",
            name="uq_market_events_source_feed_key_revision",
        ),
        sa.CheckConstraint("revision >= 1", name="ck_market_events_revision_positive"),
        sa.CheckConstraint(
            "published_at <= first_seen_at AND first_seen_at <= available_at",
            name="ck_market_events_pit_order",
        ),
    )
    op.create_index(
        "ix_market_events_published_at", "market_events", [sa.text("published_at DESC")]
    )
    op.create_index(
        "ix_market_events_event_type_published_at",
        "market_events",
        ["event_type", "published_at"],
    )
    op.create_index(
        "ix_market_events_story_key_published_at",
        "market_events",
        ["story_key", "published_at"],
    )
    op.create_index(
        "ix_market_events_unprocessed_first_seen_at",
        "market_events",
        ["first_seen_at"],
        postgresql_where=sa.text("processed_at IS NULL"),
    )

    op.create_table(
        "event_entities",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "event_id",
            sa.Integer,
            sa.ForeignKey("market_events.id", name="fk_event_entities_event_id"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(16), nullable=False),
        sa.Column("value", sa.String(64), nullable=False),
        sa.Column("symbol", sa.String(32)),
        sa.Column("method", sa.String(24), nullable=False),
        sa.Column("confidence", sa.Double, nullable=False),
        sa.Column("relevance", JSONB, nullable=False, server_default="[]"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "event_id", "kind", "value", name="uq_event_entities_event_kind_value"
        ),
        sa.CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_event_entities_confidence_range",
        ),
    )
    op.create_index("ix_event_entities_symbol", "event_entities", ["symbol"])

    op.create_table(
        "alerts",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "event_id",
            sa.Integer,
            sa.ForeignKey("market_events.id", name="fk_alerts_event_id"),
            nullable=False,
        ),
        sa.Column("rule", sa.String(48), nullable=False),
        sa.Column("score", sa.Double, nullable=False),
        sa.Column("components", JSONB, nullable=False),
        sa.Column("why", JSONB, nullable=False),
        sa.Column("symbol", sa.String(32)),
        sa.Column("relevance_kind", sa.String(16)),
        sa.Column("cooldown_key", sa.String(128), nullable=False),
        sa.Column("mode", sa.String(12), nullable=False),
        sa.Column("status", sa.String(12), nullable=False, server_default="open"),
        sa.Column("disposition", sa.String(16)),
        sa.Column("snoozed_until", sa.DateTime(timezone=True)),
        sa.Column("acked_at", sa.DateTime(timezone=True)),
        sa.Column("fired_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("event_id", "rule", name="uq_alerts_event_rule"),
        sa.CheckConstraint(
            "disposition IS NULL OR disposition IN ('useful', 'false_positive')",
            name="ck_alerts_disposition",
        ),
    )
    op.create_index("ix_alerts_status_fired_at", "alerts", ["status", "fired_at"])
    op.create_index(
        "ix_alerts_cooldown_key_fired_at", "alerts", ["cooldown_key", "fired_at"]
    )

    op.create_table(
        "alert_deliveries",
        sa.Column("id", sa.Integer, primary_key=True),
        sa.Column(
            "alert_id",
            sa.Integer,
            sa.ForeignKey("alerts.id", name="fk_alert_deliveries_alert_id"),
        ),
        sa.Column(
            "event_id",
            sa.Integer,
            sa.ForeignKey("market_events.id", name="fk_alert_deliveries_event_id"),
            nullable=False,
        ),
        sa.Column("rule", sa.String(48), nullable=False),
        sa.Column("channel", sa.String(16), nullable=False),
        sa.Column("action", sa.String(16), nullable=False),
        sa.Column("attempted_at", sa.DateTime(timezone=True)),
        sa.Column("ok", sa.Boolean, nullable=False),
        sa.Column("http_status", sa.Integer),
        sa.Column("detail", JSONB),
        sa.CheckConstraint(
            "alert_id IS NOT NULL OR (channel = 'audit' AND action = 'suppress')",
            name="ck_alert_deliveries_alert_or_suppress",
        ),
    )
    op.create_index("ix_alert_deliveries_alert_id", "alert_deliveries", ["alert_id"])
    op.create_index("ix_alert_deliveries_event_id", "alert_deliveries", ["event_id"])
    op.create_index(
        "ix_alert_deliveries_action_attempted_at",
        "alert_deliveries",
        ["action", "attempted_at"],
    )

    op.create_table(
        "source_cursors",
        sa.Column("source", sa.String(32), primary_key=True),
        sa.Column("feed", sa.String(64), primary_key=True),
        sa.Column("cursor", JSONB, nullable=False, server_default="{}"),
        sa.Column("etag", sa.String),
        sa.Column("last_modified", sa.String),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("last_success_at", sa.DateTime(timezone=True)),
        sa.Column("last_failure_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text),
        sa.Column(
            "consecutive_failures", sa.Integer, nullable=False, server_default="0"
        ),
        sa.Column("dead_letter_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("events_seen", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("events_new", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column("events_revised", sa.BigInteger, nullable=False, server_default="0"),
        sa.Column(
            "events_duplicate", sa.BigInteger, nullable=False, server_default="0"
        ),
        sa.Column("events_rejected", sa.BigInteger, nullable=False, server_default="0"),
    )


def downgrade() -> None:
    op.drop_table("source_cursors")
    op.drop_index(
        "ix_alert_deliveries_action_attempted_at", table_name="alert_deliveries"
    )
    op.drop_index("ix_alert_deliveries_event_id", table_name="alert_deliveries")
    op.drop_index("ix_alert_deliveries_alert_id", table_name="alert_deliveries")
    op.drop_table("alert_deliveries")
    op.drop_index("ix_alerts_cooldown_key_fired_at", table_name="alerts")
    op.drop_index("ix_alerts_status_fired_at", table_name="alerts")
    op.drop_table("alerts")
    op.drop_index("ix_event_entities_symbol", table_name="event_entities")
    op.drop_table("event_entities")
    op.drop_index(
        "ix_market_events_unprocessed_first_seen_at", table_name="market_events"
    )
    op.drop_index("ix_market_events_story_key_published_at", table_name="market_events")
    op.drop_index(
        "ix_market_events_event_type_published_at", table_name="market_events"
    )
    op.drop_index("ix_market_events_published_at", table_name="market_events")
    op.drop_table("market_events")
