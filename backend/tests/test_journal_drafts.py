"""Tests for the weekly review tick + pack-seeded drafts.

Real Postgres, connect-or-skip — mirrors `test_journal.py`'s
`_with_real_db` convention (`journal_drafts.py`/`journal.py`/`forecasts.py`
each open their own `session_scope()`, so all three module-level names get
monkeypatched onto the same throwaway `async_sessionmaker`). Each test uses
a synthetic, far-future ISO week (`_now_for`) so `report_packs`' PK never
collides with another test in the same run, or with a prior run's
leftovers against a persistent Postgres."""
from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import date, datetime, timedelta, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.forecasts as forecasts_module
import app.services.journal as journal_module
import app.services.journal_drafts as journal_drafts_module
from app.config import Settings
from app.models import JournalEntry, Recommendation, ReportPack, ShadowVerdict
from app.services.advisor_report import iso_week
from app.services.forecasts import ForecastLedger
from app.services.journal import JournalService
from app.services.journal_drafts import run_weekly_tick

_BASE = datetime(2031, 1, 6, tzinfo=timezone.utc)  # a Monday, far past any real data


def _now_for(offset_weeks: int) -> datetime:
    run_seed = int(time.time()) % 500  # varies per suite run — avoids PK collisions across runs
    return _BASE + timedelta(weeks=run_seed * 20 + offset_weeks)


def _settings(**kw) -> Settings:
    kw.setdefault("demo_mode", False)
    return Settings(
        postgres_user="u", postgres_password="p", postgres_db="d", redis_password="r", **kw,
    )


def _with_real_db(monkeypatch, body):
    """Runs `body(settings, session_factory)` inside one `asyncio.run()` —
    same connect-probe-then-reuse-one-loop shape `test_journal.py` uses (an
    asyncpg/SQLAlchemy event-loop-affinity issue across two `asyncio.run()`
    calls, documented there)."""
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

        monkeypatch.setattr(journal_drafts_module, "session_scope", _fake_session_scope)
        monkeypatch.setattr(journal_module, "session_scope", _fake_session_scope)
        monkeypatch.setattr(forecasts_module, "session_scope", _fake_session_scope)
        try:
            await body(settings, session_factory)
        finally:
            await engine.dispose()

    asyncio.run(_runner())


async def _seed_pack(session_factory, report_week: str, tools_stats: dict | None = None) -> None:
    async with session_factory() as session:
        session.add(ReportPack(
            report_week=report_week, status="complete", sections={"shadow": {"tools": tools_stats or {}}},
        ))
        await session.commit()


async def _seed_verdict(
    session_factory, report_week: str, symbol: str, tool: str, verdict: str, direction: int | None,
) -> None:
    async with session_factory() as session:
        session.add(ShadowVerdict(
            report_week=report_week, symbol=symbol, tool=tool, verdict=verdict, direction=direction,
            reading={"source": "yfinance"}, rationale="test fixture",
        ))
        await session.commit()


def _forecast_row(**overrides):
    now = datetime(2031, 1, 2, tzinfo=timezone.utc)
    values = dict(
        issued_by="human", issuer_version="human", subject_kind="symbol", subject="TEST",
        statement="TEST closes higher than today in 28 days.", kind="binary", probability=0.6,
        target={"metric": "forward_return", "comparator": ">", "threshold": 0.0},
        horizon_days=28, decision_date=date(2031, 1, 2), due_date=date(2031, 1, 30),
        published_at=now, first_seen_at=now, available_at=now,
        resolution_source="manual", resolution_rule={"manual": True},
        evidence_refs=[{"kind": "issuance_reason", "value": "test fixture"}],
    )
    values.update(overrides)
    return values


def test_tick_is_idempotent_and_drafts_never_carry_a_forecast_id(monkeypatch):
    now = _now_for(1)
    report_week = iso_week(now)
    symbol = f"S4_IDEMP_{int(time.time() * 1000)}"

    async def _body(settings, session_factory):
        await _seed_pack(session_factory, report_week, {"technicals": {"hit_rate": 0.7, "n_graded": 10, "pushes": 0}})
        await _seed_verdict(session_factory, report_week, symbol, "technicals", "add_watch", 1)

        first = await run_weekly_tick(redis=None, settings=settings, now=now)
        second = await run_weekly_tick(redis=None, settings=settings, now=now)
        assert first["drafts_created"] == 1
        assert second["drafts_created"] == 0  # idempotent re-run, no duplicate

        async with session_factory() as session:
            rows = (await session.execute(
                sa.select(JournalEntry).where(
                    JournalEntry.subject == symbol, JournalEntry.source == "pack_draft",
                    JournalEntry.draft_of_week == report_week,
                )
            )).scalars().all()
        assert len(rows) == 1
        assert rows[0].forecast_id is None
        assert rows[0].status == "open"

    _with_real_db(monkeypatch, _body)


def test_cap_is_respected(monkeypatch):
    now = _now_for(2)
    report_week = iso_week(now)
    stamp = int(time.time() * 1000)
    symbols = [f"S4_CAP_{stamp}_{i}" for i in range(3)]

    async def _body(settings, session_factory):
        await _seed_pack(session_factory, report_week, {"technicals": {"hit_rate": 0.9, "n_graded": 20, "pushes": 0}})
        for i, sym in enumerate(symbols):
            # Distinct tools -> distinct (deterministic) convictions via distinct hit rates
            await _seed_verdict(session_factory, report_week, sym, "technicals", "add_watch", 1)

        capped_settings = settings.model_copy(update={"journal_draft_max_per_week": 1})
        result = await run_weekly_tick(redis=None, settings=capped_settings, now=now)
        assert result["drafts_created"] == 1
        assert result["candidates"] == 3

        async with session_factory() as session:
            rows = (await session.execute(
                sa.select(JournalEntry).where(
                    JournalEntry.source == "pack_draft", JournalEntry.draft_of_week == report_week,
                )
            )).scalars().all()
        assert len(rows) == 1

    _with_real_db(monkeypatch, _body)


def test_subject_with_open_human_forecast_is_skipped(monkeypatch):
    now = _now_for(3)
    report_week = iso_week(now)
    symbol = f"S4_HUMAN_{int(time.time() * 1000)}"

    async def _body(settings, session_factory):
        await _seed_pack(session_factory, report_week, {"technicals": {"hit_rate": 0.7, "n_graded": 10, "pushes": 0}})
        await _seed_verdict(session_factory, report_week, symbol, "technicals", "add_watch", 1)

        ledger = ForecastLedger(redis=None, settings=settings)
        issued = await ledger.issue([_forecast_row(subject=symbol)])
        assert issued["issued"] == 1

        result = await run_weekly_tick(redis=None, settings=settings, now=now)
        assert result["drafts_created"] == 0

        async with session_factory() as session:
            rows = (await session.execute(
                sa.select(JournalEntry).where(JournalEntry.subject == symbol, JournalEntry.source == "pack_draft")
            )).scalars().all()
        assert rows == []

    _with_real_db(monkeypatch, _body)


def test_worker_health_reports_journal_due(monkeypatch):
    now = _now_for(4)
    stamp = int(time.time() * 1000)
    subject_a, subject_b = f"S4_DUE_A_{stamp}", f"S4_DUE_B_{stamp}"

    async def _body(settings, session_factory):
        svc = JournalService(redis=None, settings=settings)
        entry_date = now.date() - timedelta(days=3)
        review_date = now.date() - timedelta(days=1)  # already due relative to `now`
        await svc.create(dict(
            title="t", view="v", probability=0.6, subject=subject_a,
            entry_date=entry_date, review_date=review_date,
        ))
        await svc.create(dict(
            title="t2", view="v2", probability=0.6, subject=subject_b,
            entry_date=entry_date, review_date=review_date,
        ))

        result = await run_weekly_tick(redis=None, settings=settings, now=now)
        assert result["journal_due"] >= 2

    _with_real_db(monkeypatch, _body)


def test_no_pack_this_week_is_a_no_op_not_an_error(monkeypatch):
    now = _now_for(5)  # no report_packs row seeded for this week at all

    async def _body(settings, session_factory):
        result = await run_weekly_tick(redis=None, settings=settings, now=now)
        assert result["drafts_created"] == 0
        assert result["pack_status"] == "missing"
        assert result["errors"] == []

    _with_real_db(monkeypatch, _body)


def test_proposed_recommendation_with_no_citation_is_drafted(monkeypatch):
    now = _now_for(6)
    report_week = iso_week(now)
    symbol = f"S4_REC_{int(time.time() * 1000)}"

    async def _body(settings, session_factory):
        await _seed_pack(session_factory, report_week)
        async with session_factory() as session:
            session.add(Recommendation(
                rule_id="weight_band_trim", as_of=now.date(), report_week=report_week, action="trim",
                sizing=1000.0, input_snapshot={"symbol": symbol}, status="proposed",
            ))
            await session.commit()

        result = await run_weekly_tick(redis=None, settings=settings, now=now)
        assert result["drafts_created"] == 1

        async with session_factory() as session:
            rows = (await session.execute(
                sa.select(JournalEntry).where(JournalEntry.subject == symbol, JournalEntry.source == "pack_draft")
            )).scalars().all()
        assert len(rows) == 1
        assert rows[0].forecast_id is None
        assert rows[0].probability == journal_drafts_module._RECOMMENDATION_DRAFT_PLACEHOLDER_PROBABILITY

    _with_real_db(monkeypatch, _body)
