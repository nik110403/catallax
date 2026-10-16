"""Tests for the `forecasts`/`outcomes` ORM models and
`shadow_verdicts.horizon_days` (Phase 18).

ORM-shape assertions (table name, nullability, constraint/index names) run
everywhere, no DB required. The PIT-order and outcomes-uniqueness
invariants are DB-level CHECK/UNIQUE constraints SQLAlchemy does not
enforce client-side — those two tests need a live Postgres (migration 0013
applied) and skip cleanly when one is not reachable, e.g. on the host
outside docker where `postgres_host` ("postgres") does not resolve
(mirrors `test_report_worker.py`'s `asyncio.run(...)` convention rather
than pytest-asyncio's `async def` tests).
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy import CheckConstraint, UniqueConstraint, insert
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.ext.asyncio import create_async_engine

from app.config import get_settings
from app.models import Forecast, Outcome, ShadowVerdict


# ── ORM-shape tests (no DB) ────────────────────────────────────────────


def test_shadow_verdict_gains_horizon_days_column():
    col = ShadowVerdict.__table__.c.horizon_days
    assert col.nullable is True
    assert isinstance(col.type, sa.Integer)


def test_forecast_table_shape():
    table = Forecast.__table__
    assert table.name == "forecasts"
    cols = table.c
    assert cols.probability.nullable
    assert cols.distribution.nullable
    assert cols.direction.nullable
    assert cols.report_week.nullable
    for required in (
        "issued_by", "issuer_version", "subject_kind", "subject", "statement",
        "kind", "target", "horizon_days", "decision_date", "due_date",
        "published_at", "first_seen_at", "available_at", "resolution_source",
        "resolution_rule", "evidence_refs", "status", "created_at",
    ):
        assert not cols[required].nullable, f"{required} should be NOT NULL"
    assert cols.status.server_default is not None

    unique_names = {c.name for c in table.constraints if isinstance(c, UniqueConstraint)}
    assert "uq_forecasts_issuer_subject_date_horizon" in unique_names

    check_names = {c.name for c in table.constraints if isinstance(c, CheckConstraint)}
    assert "ck_forecasts_pit_order" in check_names
    assert "ck_forecasts_probability_range" in check_names

    index_names = {ix.name for ix in table.indexes}
    assert {
        "ix_forecasts_status", "ix_forecasts_due_date", "ix_forecasts_report_week",
        "ix_forecasts_issued_by_subject_status",
    } <= index_names


def test_outcome_table_shape():
    table = Outcome.__table__
    assert table.name == "outcomes"
    cols = table.c
    assert not cols.forecast_id.nullable
    assert cols.forecast_id.unique
    assert cols.hit.nullable
    assert cols.brier.nullable
    assert cols.log_score.nullable
    assert not cols.realized.nullable
    assert not cols.attribution.nullable
    assert not cols.resolver_version.nullable


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


_BASE_TARGET = {"metric": "forward_return", "comparator": ">", "threshold": 0.0}
_BASE_RULE = {
    "symbol": "TEST", "source": "yfinance",
    "entry": "first close on/after decision_date within stale_days",
    "exit": "first close on/after due_date within stale_days",
    "book_median": None,
}


def _forecast_values(**overrides):
    now = datetime(2026, 9, 2, tzinfo=timezone.utc)
    values = dict(
        issued_by="human",
        issuer_version="human",
        subject_kind="symbol",
        subject="TEST",
        statement="TEST closes higher than today in 28 days.",
        kind="binary",
        probability=0.6,
        target=dict(_BASE_TARGET),
        horizon_days=28,
        decision_date=date(2026, 9, 2),
        due_date=date(2026, 9, 30),
        published_at=now,
        first_seen_at=now,
        available_at=now,
        resolution_source="manual",
        resolution_rule=dict(_BASE_RULE),
        evidence_refs=[{"kind": "issuance_reason", "note": "test fixture"}],
    )
    values.update(overrides)
    return values


def test_available_at_before_first_seen_at_raises_integrity_error():
    engine = _connect_or_skip()

    async def _run():
        bad_now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
        earlier = bad_now - timedelta(hours=1)
        values = _forecast_values(
            issued_by="human", subject="PIT_ORDER_TEST",
            first_seen_at=bad_now, available_at=earlier,
        )
        try:
            async with engine.begin() as conn:
                await conn.execute(insert(Forecast.__table__).values(**values))
        finally:
            await engine.dispose()

    with pytest.raises(IntegrityError):
        asyncio.run(_run())


def test_second_outcome_for_one_forecast_raises_integrity_error():
    engine = _connect_or_skip()

    async def _run():
        try:
            async with engine.begin() as conn:
                result = await conn.execute(
                    insert(Forecast.__table__)
                    .values(**_forecast_values(subject="OUTCOME_UNIQUE_TEST"))
                    .returning(Forecast.__table__.c.id)
                )
                forecast_id = result.scalar_one()
                outcome_values = dict(
                    forecast_id=forecast_id,
                    resolved_at=datetime(2026, 9, 30, tzinfo=timezone.utc),
                    resolution_status="manual",
                    realized={"note": "test fixture"},
                    attribution={"thesis": "n/a", "direction": "n/a", "sizing": "n/a"},
                    resolver_version="test",
                )
                await conn.execute(insert(Outcome.__table__).values(**outcome_values))
                await conn.execute(insert(Outcome.__table__).values(**outcome_values))
        finally:
            await engine.dispose()

    with pytest.raises(IntegrityError):
        asyncio.run(_run())
