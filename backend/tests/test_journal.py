"""Tests for the analyst journal service + router.

Validation (probability bounds, date ordering, horizon range, immutable
PATCH fields, blank title/view) never touches the DB — it runs first inside
`JournalService.create`/`update`, so those tests run everywhere. Emission
(`create` actually issuing a `Forecast`, idempotency, the no-orphan-entry
guarantee on ledger rejection, the due-before window) needs a REAL
Postgres with migration 0014 applied — mirrors `test_forecasts.py`'s
"connect or skip" convention (monkeypatches `session_scope` in BOTH
`app.services.journal` and `app.services.forecasts`, since `create()`
opens its own session and also drives the ledger's, two separate
transactions over the same engine). Router tests exercise the DEMO_MODE
seam directly (async handlers, no DB/service construction under demo),
mirroring `test_forecasts_router.py`.
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import date

import pytest
import sqlalchemy as sa
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.routers.journal as journal_router
import app.services.forecasts as forecasts_module
import app.services.journal as journal_module
from app.config import Settings
from app.errors import DemoModeReadOnly, InvalidRequest, NotFound
from app.models import JournalEntry
from app.services.forecasts import ForecastLedger
from app.services.journal import JournalService


def _settings(**kw) -> Settings:
    kw.setdefault("demo_mode", False)
    return Settings(
        postgres_user="u", postgres_password="p", postgres_db="d", redis_password="r", **kw,
    )


def _payload(**kw):
    values = dict(
        title="Test claim", view="I think this view is true.", probability=0.6,
        review_date=date(2026, 9, 10), subject="S2_DEFAULT", entry_date=date(2026, 9, 2),
    )
    values.update(kw)
    return values


# ── validation — no DB required ─────────────────────────────────────────
def test_probability_zero_and_one_are_rejected():
    svc = JournalService(redis=None, settings=_settings())

    async def _run(p):
        with pytest.raises(InvalidRequest):
            await svc.create(_payload(subject="S2_PROB", probability=p))

    asyncio.run(_run(0.0))
    asyncio.run(_run(1.0))


def test_blank_title_and_view_rejected_after_strip():
    svc = JournalService(redis=None, settings=_settings())

    async def _run():
        with pytest.raises(InvalidRequest):
            await svc.create(_payload(subject="S2_BLANK1", title="   "))
        with pytest.raises(InvalidRequest):
            await svc.create(_payload(subject="S2_BLANK2", view="   "))

    asyncio.run(_run())


def test_review_date_must_be_after_entry_date():
    svc = JournalService(redis=None, settings=_settings())

    async def _run():
        with pytest.raises(InvalidRequest):
            await svc.create(_payload(
                subject="S2_DATEORDER", entry_date=date(2026, 9, 5), review_date=date(2026, 9, 1),
            ))

    asyncio.run(_run())


def test_horizon_outside_configured_range_rejected():
    settings = _settings(journal_min_horizon_days=5, journal_max_horizon_days=10)
    svc = JournalService(redis=None, settings=settings)

    async def _run():
        with pytest.raises(InvalidRequest):
            # horizon = 2 days, below the configured floor of 5.
            await svc.create(_payload(
                subject="S2_HORIZON", entry_date=date(2026, 9, 2), review_date=date(2026, 9, 4),
            ))

    asyncio.run(_run())


def test_update_rejects_immutable_fields():
    svc = JournalService(redis=None, settings=_settings())

    async def _run():
        with pytest.raises(InvalidRequest):
            await svc.update(1, {"probability": 0.9})
        with pytest.raises(InvalidRequest):
            await svc.update(1, {"title": "new claim"})

    asyncio.run(_run())


def test_review_rejects_unknown_status():
    svc = JournalService(redis=None, settings=_settings())

    async def _run():
        with pytest.raises(InvalidRequest):
            await svc.review(1, "note", outcome_hint="bogus")

    asyncio.run(_run())


# ── issue()/idempotency/no-orphan — real Postgres, skip if unreachable ───
def _with_real_db(monkeypatch, body):
    """Runs `body(settings)` (an async callable) inside ONE `asyncio.run()`
    that also owns the connect-or-skip probe. `test_forecasts.py`'s own
    `_pg_engine_or_skip()` pattern pings on one `asyncio.run()` call and
    then reuses the same `AsyncEngine` from a SECOND, separate
    `asyncio.run()` for the test body — an asyncpg/SQLAlchemy
    event-loop-affinity bug when a Postgres is actually reachable (verified
    here: `test_forecasts.py`'s own DB tests fail identically, same
    symptom, against the same throwaway container — pre-existing, not
    introduced by this change). Doing the
    probe, the session use and the dispose all on one loop sidesteps it."""
    settings = _settings()

    async def _runner():
        engine = create_async_engine(settings.database_url, connect_args={"timeout": 3})
        try:
            async with engine.connect() as conn:
                await conn.execute(sa.text("SELECT 1"))
        except Exception as exc:  # OperationalError, OSError/gaierror, TimeoutError, …
            await engine.dispose()
            pytest.skip(f"Postgres unreachable from host ({exc.__class__.__name__}): {exc}")

        session_factory = async_sessionmaker(engine, expire_on_commit=False)

        @contextlib.asynccontextmanager
        async def _fake_session_scope():
            async with session_factory() as session:
                yield session

        monkeypatch.setattr(journal_module, "session_scope", _fake_session_scope)
        monkeypatch.setattr(forecasts_module, "session_scope", _fake_session_scope)
        try:
            await body(settings)
        finally:
            await engine.dispose()

    asyncio.run(_runner())


def test_create_emits_exactly_one_forecast_with_the_right_natural_key(monkeypatch):
    subject = f"S2_EMIT_{int(time.time() * 1000)}"
    entry_date, review_date = date(2026, 9, 2), date(2026, 9, 10)

    async def _body(settings):
        svc = JournalService(redis=None, settings=settings)
        result = await svc.create(_payload(subject=subject, entry_date=entry_date, review_date=review_date))
        ledger = ForecastLedger(redis=None, settings=settings)
        forecast = await ledger.get_by_natural_key(
            issued_by="human:journal", subject=subject, decision_date=entry_date, horizon_days=8,
        )
        assert forecast is not None
        assert forecast["status"] == "pending"
        assert result["entry"]["forecast_id"] == forecast["id"] == result["forecast"]["id"]

    _with_real_db(monkeypatch, _body)


def test_recreate_same_natural_key_is_idempotent_and_links_existing_forecast(monkeypatch):
    subject = f"S2_IDEMP_{int(time.time() * 1000)}"
    entry_date, review_date = date(2026, 9, 2), date(2026, 9, 10)

    async def _body(settings):
        svc = JournalService(redis=None, settings=settings)
        first = await svc.create(_payload(subject=subject, entry_date=entry_date, review_date=review_date))
        second = await svc.create(_payload(
            subject=subject, entry_date=entry_date, review_date=review_date, title="Restated claim",
        ))
        assert first["forecast"]["id"] == second["forecast"]["id"]
        assert first["entry"]["id"] != second["entry"]["id"]  # two entries, one forecast

    _with_real_db(monkeypatch, _body)


def test_ledger_rejection_leaves_no_orphan_entry_row(monkeypatch):
    subject = f"S2_ORPHAN_{int(time.time() * 1000)}"
    title = f"orphan-check-{subject}"
    # Before `forecast_ledger_start_date` (2026-08-31) — the ledger rejects
    # this into `errors`, `create()` must never commit the entry it flushed.
    entry_date, review_date = date(2026, 8, 1), date(2026, 8, 15)

    async def _body(settings):
        svc = JournalService(redis=None, settings=settings)
        with pytest.raises(InvalidRequest):
            await svc.create(_payload(
                subject=subject, title=title, entry_date=entry_date, review_date=review_date,
            ))
        async with journal_module.session_scope() as session:
            row = (await session.execute(
                sa.select(JournalEntry).where(JournalEntry.title == title)
            )).scalar_one_or_none()
        assert row is None

    _with_real_db(monkeypatch, _body)


def test_due_before_window_filters_on_review_date(monkeypatch):
    stamp = int(time.time() * 1000)
    near, far = f"S2_DUE_NEAR_{stamp}", f"S2_DUE_FAR_{stamp}"
    entry_date = date(2026, 9, 2)

    async def _body(settings):
        svc = JournalService(redis=None, settings=settings)
        await svc.create(_payload(subject=near, entry_date=entry_date, review_date=date(2026, 9, 5)))
        await svc.create(_payload(subject=far, entry_date=entry_date, review_date=date(2026, 12, 1)))
        rows = await svc.due(before=date(2026, 9, 10))
        subjects = {r["subject"] for r in rows}
        assert near in subjects
        assert far not in subjects

    _with_real_db(monkeypatch, _body)


# ── router — DEMO_MODE seam, no DB/service construction ────────────────
def _create_body(**kw) -> journal_router.JournalEntryCreateRequest:
    values = dict(
        title="t", view="v", probability=0.5, review_date=date(2026, 9, 10), subject="X",
    )
    values.update(kw)
    return journal_router.JournalEntryCreateRequest(**values)


async def test_router_create_demo_mode_raises_before_any_db_touch():
    settings = _settings(demo_mode=True)
    with pytest.raises(DemoModeReadOnly):
        await journal_router.create_entry(_create_body(), settings=settings, redis=object())


async def test_router_list_entries_demo_mode_returns_empty():
    settings = _settings(demo_mode=True)
    result = await journal_router.list_entries(settings=settings, redis=object())
    assert result == {"entries": [], "count": 0}


async def test_router_due_entries_demo_mode_returns_empty():
    settings = _settings(demo_mode=True)
    result = await journal_router.due_entries(settings=settings, redis=object())
    assert result == {"entries": [], "count": 0}


async def test_router_get_entry_demo_mode_404s():
    settings = _settings(demo_mode=True)
    with pytest.raises(NotFound):
        await journal_router.get_entry(entry_id=1, settings=settings, redis=object())


async def test_router_update_entry_demo_mode_raises():
    settings = _settings(demo_mode=True)
    body = journal_router.JournalEntryUpdateRequest(thesis="x")
    with pytest.raises(DemoModeReadOnly):
        await journal_router.update_entry(body, entry_id=1, settings=settings, redis=object())


async def test_router_review_entry_demo_mode_raises():
    settings = _settings(demo_mode=True)
    body = journal_router.JournalReviewRequest(note="n")
    with pytest.raises(DemoModeReadOnly):
        await journal_router.review_entry(body, entry_id=1, settings=settings, redis=object())


def test_create_request_model_rejects_probability_zero_and_one():
    with pytest.raises(ValidationError):
        _create_body(probability=0.0)
    with pytest.raises(ValidationError):
        _create_body(probability=1.0)


def test_update_request_model_forbids_immutable_extra_fields():
    with pytest.raises(ValidationError):
        journal_router.JournalEntryUpdateRequest(probability=0.9)
