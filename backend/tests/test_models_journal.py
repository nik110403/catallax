"""Tests for the `journal_entries`/`notes`/`cockpit_sessions`/
`cockpit_messages` ORM models (Phase 19).

ORM-shape assertions (table name, nullability, constraint/index names) run
everywhere, no DB required. The probability-range CHECK constraint is a
DB-level invariant SQLAlchemy does not enforce client-side — that test
needs a live Postgres (migration 0014 applied) and skips cleanly when one
is not reachable, e.g. on the host outside docker (mirrors
`test_models_forecasts.py`'s `asyncio.run(...)` convention).
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy import CheckConstraint, UniqueConstraint, insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.models import CockpitMessage, CockpitSession, JournalEntry, Note


# ── ORM-shape tests (no DB) ────────────────────────────────────────────


def test_journal_entry_table_shape():
    table = JournalEntry.__table__
    assert table.name == "journal_entries"
    cols = table.c
    for required in (
        "entry_date", "title", "view", "probability", "review_date",
        "horizon_days", "subject", "status", "source", "created_at",
        "updated_at",
    ):
        assert not cols[required].nullable, f"{required} should be NOT NULL"
    for optional in (
        "thesis", "evidence", "invalidation", "sizing_note",
        "confidence_note", "draft_of_week", "forecast_id", "reviewed_at",
        "review_note",
    ):
        assert cols[optional].nullable, f"{optional} should be nullable"
    assert cols.status.server_default is not None
    assert cols.source.server_default is not None

    check_names = {c.name for c in table.constraints if isinstance(c, CheckConstraint)}
    assert "ck_journal_entries_probability_range" in check_names

    index_names = {ix.name for ix in table.indexes}
    assert {
        "ix_journal_entries_entry_date", "ix_journal_entries_status_review_date",
        "ix_journal_entries_subject",
    } <= index_names


def test_note_table_shape():
    table = Note.__table__
    assert table.name == "notes"
    cols = table.c
    assert not cols.slug.nullable
    assert cols.slug.unique
    assert not cols.kind.nullable
    assert not cols.title.nullable
    assert not cols.status.nullable
    assert not cols.one_sentence.nullable
    for optional in (
        "why_it_matters", "action", "risk_invalidation", "body",
        "evidence_refs", "subjects", "signed_at",
    ):
        assert cols[optional].nullable, f"{optional} should be nullable"
    assert cols.status.server_default is not None

    index_names = {ix.name for ix in table.indexes}
    assert {"ix_notes_kind_status", "ix_notes_slug"} <= index_names


def test_cockpit_session_table_shape():
    table = CockpitSession.__table__
    assert table.name == "cockpit_sessions"
    cols = table.c
    assert not cols.session_key.nullable
    assert cols.session_key.unique
    assert cols.title.nullable

    index_names = {ix.name for ix in table.indexes}
    assert "ix_cockpit_sessions_updated_at" in index_names


def test_cockpit_message_table_shape():
    table = CockpitMessage.__table__
    assert table.name == "cockpit_messages"
    cols = table.c
    assert not cols.session_id.nullable
    assert not cols.role.nullable
    assert not cols.content.nullable
    assert cols.tool_calls.nullable
    assert cols.citations.nullable
    assert not cols.refused.nullable
    assert cols.refused.server_default is not None
    assert cols.job_id.nullable

    index_names = {ix.name for ix in table.indexes}
    assert {
        "ix_cockpit_messages_session_id", "ix_cockpit_messages_session_id_id",
    } <= index_names


# ── DB-dependent constraint tests (skip cleanly if Postgres unreachable) ──


def _connect_or_skip():
    settings = get_settings()
    # `postgres` is only DNS-resolvable inside the compose network; from the
    # host this connect attempt would otherwise hang on OS-level TCP retry —
    # fail fast so skip is quick, not a ~1-minute stall per test.
    engine = create_async_engine(settings.database_url, connect_args={"timeout": 3})

    async def _ping():
        async with engine.connect() as conn:
            await conn.execute(sa.text("SELECT 1"))

    try:
        asyncio.run(_ping())
    except Exception as exc:  # OperationalError, OSError/gaierror, TimeoutError, …
        asyncio.run(engine.dispose())
        pytest.skip(f"Postgres unreachable from host ({exc.__class__.__name__}): {exc}")
    return engine


def _journal_entry_values(**overrides):
    values = dict(
        entry_date=date(2026, 9, 3),
        title="Test entry",
        view="Test subject closes higher over the horizon.",
        probability=0.6,
        review_date=date(2026, 10, 3),
        horizon_days=30,
        subject="TEST",
    )
    values.update(overrides)
    return values


def test_probability_out_of_range_raises_integrity_error():
    engine = _connect_or_skip()

    async def _run():
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    insert(JournalEntry.__table__).values(
                        **_journal_entry_values(subject="PROB_RANGE_TEST", probability=1.5)
                    )
                )
        finally:
            await engine.dispose()

    with pytest.raises(IntegrityError):
        asyncio.run(_run())


def test_probability_boundary_zero_raises_integrity_error():
    engine = _connect_or_skip()

    async def _run():
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    insert(JournalEntry.__table__).values(
                        **_journal_entry_values(subject="PROB_ZERO_TEST", probability=0.0)
                    )
                )
        finally:
            await engine.dispose()

    with pytest.raises(IntegrityError):
        asyncio.run(_run())


def test_journal_entry_insert_and_cockpit_message_fk_round_trip():
    engine = _connect_or_skip()

    async def _run():
        try:
            async with engine.begin() as conn:
                result = await conn.execute(
                    insert(JournalEntry.__table__)
                    .values(**_journal_entry_values(subject="FK_ROUNDTRIP_TEST"))
                    .returning(JournalEntry.__table__.c.id)
                )
                assert result.scalar_one() is not None

                session_result = await conn.execute(
                    insert(CockpitSession.__table__)
                    .values(session_key="test-session-key-s1")
                    .returning(CockpitSession.__table__.c.id)
                )
                session_id = session_result.scalar_one()

                await conn.execute(
                    insert(CockpitMessage.__table__).values(
                        session_id=session_id,
                        role="user",
                        content="What is the current book?",
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(_run())
