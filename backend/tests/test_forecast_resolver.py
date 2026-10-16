"""Tests for the forecast resolver and the module-level
`shadow.fetch_forward_return` it (and `ShadowEngine._fetch_return`) shares.

No live Postgres/network needed: `fetch_forward_return` tests monkeypatch
`shadow.py`'s `build_adapter`/`HistoryStore`/`session_scope` seams with an
in-memory candle store (mirrors `test_shadow.py`'s own convention of
monkeypatching `_fetch_return` rather than faking SQLAlchemy statement
execution); `resolve_due` tests use a duck-typed fake `ForecastLedger` (no
real DB) plus a monkeypatched `fetch_forward_return` and a fake
`session_scope` for the book-median cross-section query. The one exception
is the integration test at the bottom of this file (a known coverage gap) — REAL Postgres, real `Forecast`/`daily_bars` rows, `skip`
cleanly (not hang) when Postgres is unreachable from the host, mirroring
`test_forecasts.py`'s `_pg_engine_or_skip` convention (duplicated here, not
imported, same no-coupling convention `shadow._wilson_lb` documents).
"""
from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.forecast_resolver as forecast_resolver_module
import app.services.forecasts as forecasts_module
import app.services.shadow as shadow_module
from app.config import Settings
from app.errors import RateLimitExceeded
from app.models import DailyBarRow, Outcome
from app.services.forecast_resolver import _hit, resolve_due
from app.services.forecasts import ForecastLedger
from app.services.shadow import _RATE_LIMIT_WAIT_RETRIES, _score_tool, fetch_forward_return


def _settings(**kw: Any) -> Settings:
    kw.setdefault("demo_mode", False)
    return Settings(postgres_user="u", postgres_password="p", postgres_db="d", redis_password="r", **kw)


def _utc(d: date) -> datetime:
    return datetime.combine(d, datetime.min.time(), tzinfo=timezone.utc)


# ── fetch_forward_return (module-level, shadow.py) ──────────────────────
class _FakeCandle:
    def __init__(self, ts: datetime, close: float) -> None:
        self.ts = ts
        self.close = close


class _FakeEnv:
    def __init__(self, data: list[_FakeCandle]) -> None:
        self.data = data


class _FakeHistoryStore:
    def __init__(self, candles_by_symbol: dict[str, list[_FakeCandle]]) -> None:
        self._candles = candles_by_symbol

    def __call__(self, session: Any, redis: Any, settings: Any) -> "_FakeHistoryStore":
        return self

    async def get_daily(self, adapter: Any, symbol: str, start: datetime, end: datetime) -> _FakeEnv:
        return _FakeEnv([c for c in self._candles.get(symbol, []) if start <= c.ts <= end])


def _wire_history(monkeypatch, candles_by_symbol: dict[str, list[_FakeCandle]]) -> None:
    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        yield object()

    monkeypatch.setattr(shadow_module, "session_scope", _fake_session_scope)
    monkeypatch.setattr(shadow_module, "build_adapter", lambda source, redis, settings: object())
    monkeypatch.setattr(shadow_module, "HistoryStore", _FakeHistoryStore(candles_by_symbol))


def test_fetch_forward_return_pending_when_horizon_not_elapsed():
    decision = date.today()  # exit_date = today + horizon, in the future
    status, d = asyncio.run(
        fetch_forward_return(None, _settings(), "AAA", "yfinance", decision, 28, stale_days=5)
    )
    assert status == "pending"
    assert d is None


def test_fetch_forward_return_matured(monkeypatch):
    decision = date(2026, 1, 5)
    horizon_days = 28
    exit_date = decision + timedelta(days=horizon_days)
    _wire_history(monkeypatch, {"AAA": [
        _FakeCandle(_utc(decision), 100.0), _FakeCandle(_utc(exit_date), 110.0),
    ]})
    status, d = asyncio.run(
        fetch_forward_return(None, _settings(), "AAA", "yfinance", decision, horizon_days, stale_days=5)
    )
    assert status == "matured"
    assert d["value"] == pytest.approx(0.1)
    assert d["entry"] == 100.0 and d["exit"] == 110.0
    assert d["entry_date"] == decision.isoformat() and d["exit_date"] == exit_date.isoformat()


def test_fetch_forward_return_exact_zero_is_still_matured(monkeypatch):
    decision = date(2026, 1, 5)
    exit_date = decision + timedelta(days=28)
    _wire_history(monkeypatch, {"AAA": [
        _FakeCandle(_utc(decision), 100.0), _FakeCandle(_utc(exit_date), 100.0),
    ]})
    status, d = asyncio.run(
        fetch_forward_return(None, _settings(), "AAA", "yfinance", decision, 28, stale_days=5)
    )
    assert status == "matured"
    assert d["value"] == 0.0


def test_fetch_forward_return_uncovered_no_bars(monkeypatch):
    _wire_history(monkeypatch, {})
    status, d = asyncio.run(
        fetch_forward_return(None, _settings(), "AAA", "yfinance", date(2026, 1, 5), 28, stale_days=5)
    )
    assert status == "uncovered"
    assert d is None


def test_fetch_forward_return_uncovered_when_entry_bar_beyond_stale_days(monkeypatch):
    """M8: passing `entry_stale_days` opts INTO the entry bound (the
    resolver's own usage) — a gap right after `decision` that only turns up
    a bar weeks later must not silently count as "the" entry. The exit bar
    here is timely; only entry is stale. `entry_stale_days=None` (the
    default fix round) means NO entry check at all — see
    `test_fetch_forward_return_entry_stale_scoreboard_vs_resolver` below."""
    decision = date(2026, 1, 5)
    horizon_days = 28
    exit_date = decision + timedelta(days=horizon_days)
    late_entry = decision + timedelta(days=20)  # within the fetch window, but > stale_days(5) from decision
    _wire_history(monkeypatch, {"AAA": [
        _FakeCandle(_utc(late_entry), 100.0), _FakeCandle(_utc(exit_date), 110.0),
    ]})
    status, d = asyncio.run(
        fetch_forward_return(
            None, _settings(), "AAA", "yfinance", decision, horizon_days, stale_days=5, entry_stale_days=5,
        )
    )
    assert status == "uncovered"
    assert d is None


def test_fetch_forward_return_entry_stale_scoreboard_vs_resolver(monkeypatch):
    """Regression: a >5-day-late entry bar used to flip the
    SCOREBOARD's own grading (via `ShadowEngine._fetch_return`, which
    delegates here with `stale_days=5`) to `uncovered` — Phase-17 parity
    requires that path stay legacy (no entry check, `entry_stale_days=
    None`), while the resolver keeps the entry bound (M8) by passing
    `entry_stale_days` explicitly."""
    decision = date(2026, 1, 5)
    horizon_days = 28
    exit_date = decision + timedelta(days=horizon_days)
    late_entry = decision + timedelta(days=20)
    _wire_history(monkeypatch, {"AAA": [
        _FakeCandle(_utc(late_entry), 100.0), _FakeCandle(_utc(exit_date), 110.0),
    ]})

    # scoreboard path (`ShadowEngine._fetch_return`'s own parameterisation)
    status, d = asyncio.run(
        fetch_forward_return(
            None, _settings(), "AAA", "yfinance", decision, horizon_days, stale_days=5, entry_stale_days=None,
        )
    )
    assert status == "matured"
    assert d["value"] == pytest.approx(0.1)

    # resolver path (`forecast_resolver.resolve_due`'s own parameterisation)
    status2, d2 = asyncio.run(
        fetch_forward_return(
            None, _settings(), "AAA", "yfinance", decision, horizon_days, stale_days=5, entry_stale_days=5,
        )
    )
    assert status2 == "uncovered"
    assert d2 is None


def test_fetch_forward_return_uncovered_when_exit_bar_beyond_stale_days(monkeypatch):
    decision = date(2026, 1, 5)
    horizon_days = 28
    exit_date = decision + timedelta(days=horizon_days)
    late_exit = exit_date + timedelta(days=20)
    _wire_history(monkeypatch, {"AAA": [
        _FakeCandle(_utc(decision), 100.0), _FakeCandle(_utc(late_exit), 110.0),
    ]})
    status, d = asyncio.run(
        fetch_forward_return(None, _settings(), "AAA", "yfinance", decision, horizon_days, stale_days=5)
    )
    assert status == "uncovered"


# ── fetch_forward_return: rate-limit wait-and-retry (root-cause fix) ──────
class _FakeRedisTTL:
    def __init__(self, ttl: int) -> None:
        self._ttl = ttl
        self.ttl_calls: list[str] = []

    async def ttl(self, key: str) -> int:
        self.ttl_calls.append(key)
        return self._ttl


def _wire_flaky_history(monkeypatch, candles_by_symbol, fail_times: int) -> dict[str, int]:
    """Same seam as `_wire_history`, but `get_daily` raises `RateLimitExceeded`
    the first `fail_times` calls before serving candles normally."""
    calls = {"n": 0}

    class _FlakyHistoryStore:
        def __call__(self, session, redis, settings):
            return self

        async def get_daily(self, adapter, symbol, start, end):
            calls["n"] += 1
            if calls["n"] <= fail_times:
                raise RateLimitExceeded("yfinance: rate limit of 30/min exceeded")
            return _FakeEnv([
                c for c in candles_by_symbol.get(symbol, []) if start <= c.ts <= end
            ])

    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        yield object()

    monkeypatch.setattr(shadow_module, "session_scope", _fake_session_scope)
    monkeypatch.setattr(
        shadow_module, "build_adapter", lambda source, redis, settings: SimpleNamespace(name=source)
    )
    monkeypatch.setattr(shadow_module, "HistoryStore", _FlakyHistoryStore())
    return calls


def test_fetch_forward_return_rate_limit_retries_then_succeeds(monkeypatch):
    decision = date(2026, 1, 5)
    horizon_days = 28
    exit_date = decision + timedelta(days=horizon_days)
    calls = _wire_flaky_history(monkeypatch, {"AAA": [
        _FakeCandle(_utc(decision), 100.0), _FakeCandle(_utc(exit_date), 110.0),
    ]}, fail_times=1)

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(shadow_module.asyncio, "sleep", fake_sleep)
    redis = _FakeRedisTTL(ttl=12)

    status, d = asyncio.run(
        fetch_forward_return(redis, _settings(), "AAA", "yfinance", decision, horizon_days, stale_days=5)
    )
    assert status == "matured"
    assert d["value"] == pytest.approx(0.1)
    assert sleeps == [13.0]  # ttl(12) + 1, no real sleep happened
    assert redis.ttl_calls == ["ratelimit:yfinance"]
    assert calls["n"] == 2  # one failed attempt, one successful retry


def test_fetch_forward_return_rate_limit_uses_60s_fallback_when_ttl_not_positive(monkeypatch):
    decision = date(2026, 1, 5)
    horizon_days = 28
    exit_date = decision + timedelta(days=horizon_days)
    _wire_flaky_history(monkeypatch, {"AAA": [
        _FakeCandle(_utc(decision), 100.0), _FakeCandle(_utc(exit_date), 110.0),
    ]}, fail_times=1)

    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr(shadow_module.asyncio, "sleep", fake_sleep)
    redis = _FakeRedisTTL(ttl=-1)  # TTL missing/expired

    status, d = asyncio.run(
        fetch_forward_return(redis, _settings(), "AAA", "yfinance", decision, horizon_days, stale_days=5)
    )
    assert status == "matured"
    assert sleeps == [61.0]  # fallback 60 + 1


def test_fetch_forward_return_rate_limit_exhausts_retries_and_reraises(monkeypatch):
    decision = date(2026, 1, 5)
    horizon_days = 28
    calls = _wire_flaky_history(monkeypatch, {}, fail_times=_RATE_LIMIT_WAIT_RETRIES + 1)

    async def fake_sleep(seconds: float) -> None:
        pass

    monkeypatch.setattr(shadow_module.asyncio, "sleep", fake_sleep)
    redis = _FakeRedisTTL(ttl=1)

    with pytest.raises(RateLimitExceeded):
        asyncio.run(
            fetch_forward_return(redis, _settings(), "AAA", "yfinance", decision, horizon_days, stale_days=5)
        )
    # first attempt + _RATE_LIMIT_WAIT_RETRIES retries, all failing
    assert calls["n"] == _RATE_LIMIT_WAIT_RETRIES + 1


# ── _hit (pure) ──────────────────────────────────────────────────────────
def test_hit_exact_zero_is_a_push():
    assert _hit(0.0, ">", 0.0) is None
    assert _hit(0.0, "<", 0.0) is None


def test_hit_greater_than_comparator():
    assert _hit(0.05, ">", 0.0) is True
    assert _hit(-0.05, ">", 0.0) is False


def test_hit_less_than_comparator():
    assert _hit(-0.05, "<", 0.0) is True
    assert _hit(0.05, "<", 0.0) is False


# ── resolve_due (duck-typed fake ledger, no real DB) ─────────────────────
class _FakeLedger:
    def __init__(self, pending_rows: list[Any]) -> None:
        self._pending = pending_rows
        self.pending_calls: list[tuple[Any, int]] = []
        self.record_outcome_calls: list[dict[str, Any]] = []

    async def pending(self, due_before: date, limit: int) -> list[Any]:
        self.pending_calls.append((due_before, limit))
        return self._pending

    async def record_outcome(
        self, forecast_id: int, *, resolution_status: str, realized: dict[str, Any],
        hit: bool | None, resolver_version: str, manual_note: str | None = None,
    ) -> dict[str, Any]:
        self.record_outcome_calls.append({
            "forecast_id": forecast_id, "resolution_status": resolution_status,
            "realized": realized, "hit": hit, "resolver_version": resolver_version,
        })
        return {}


def _forecast(id: int, **overrides: Any) -> SimpleNamespace:
    base = dict(
        id=id, issued_by="shadow:technicals", resolution_source="daily_bars:yfinance",
        resolution_rule={"symbol": "AAA"}, subject="AAA", decision_date=date(2026, 1, 5),
        horizon_days=28, target={"metric": "forward_return", "comparator": ">", "threshold": 0.0},
        report_week="2026-W01",
    )
    base.update(overrides)
    return SimpleNamespace(**base)


_NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)


def test_resolve_due_asks_the_ledger_for_forecasts_due_by_now(monkeypatch):
    ledger = _FakeLedger([])
    settings = _settings()
    asyncio.run(resolve_due(None, settings, ledger, now=_NOW))
    assert ledger.pending_calls == [(_NOW.date(), settings.forecast_resolver_batch_size)]


def test_resolve_due_matured_hit(monkeypatch):
    async def _fake_fetch(redis, settings, symbol, source, decision, horizon_days, *, stale_days, entry_stale_days=None):
        return "matured", {
            "entry": 100.0, "entry_date": "2026-01-05", "exit": 110.0, "exit_date": "2026-02-02",
            "value": 0.1, "priced_as_of": "t",
        }

    monkeypatch.setattr(forecast_resolver_module, "fetch_forward_return", _fake_fetch)
    ledger = _FakeLedger([_forecast(1)])

    result = asyncio.run(resolve_due(None, _settings(), ledger, now=_NOW))

    assert result == {"matured": 1, "uncovered": 0, "pushes": 0, "skipped": 0, "errors": []}
    call = ledger.record_outcome_calls[0]
    assert call == {
        "forecast_id": 1, "resolution_status": "matured", "hit": True, "resolver_version": "forecast-resolver-v1",
        "realized": {
            "entry": 100.0, "entry_date": "2026-01-05", "exit": 110.0, "exit_date": "2026-02-02",
            "value": 0.1, "priced_as_of": "t", "book_median": None, "cross_section_n": None,
        },
    }


def test_resolve_due_push_on_exact_zero_return(monkeypatch):
    async def _fake_fetch(redis, settings, symbol, source, decision, horizon_days, *, stale_days, entry_stale_days=None):
        return "matured", {
            "entry": 100.0, "entry_date": "2026-01-05", "exit": 100.0, "exit_date": "2026-02-02",
            "value": 0.0, "priced_as_of": "t",
        }

    monkeypatch.setattr(forecast_resolver_module, "fetch_forward_return", _fake_fetch)
    ledger = _FakeLedger([_forecast(1)])

    result = asyncio.run(resolve_due(None, _settings(), ledger, now=_NOW))

    assert result["pushes"] == 1
    assert result["matured"] == 0
    call = ledger.record_outcome_calls[0]
    assert call["hit"] is None
    assert call["resolution_status"] == "matured"  # a push still MATURED, never abstained


def test_resolve_due_uncovered(monkeypatch):
    async def _fake_fetch(redis, settings, symbol, source, decision, horizon_days, *, stale_days, entry_stale_days=None):
        return "uncovered", None

    monkeypatch.setattr(forecast_resolver_module, "fetch_forward_return", _fake_fetch)
    ledger = _FakeLedger([_forecast(1)])

    result = asyncio.run(resolve_due(None, _settings(), ledger, now=_NOW))

    assert result["uncovered"] == 1
    call = ledger.record_outcome_calls[0]
    assert call["resolution_status"] == "uncovered"
    assert call["hit"] is None


def test_resolve_due_skips_manual_forecasts_never_auto_resolved(monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("a manual forecast must never be auto-resolved")

    monkeypatch.setattr(forecast_resolver_module, "fetch_forward_return", _boom)
    ledger = _FakeLedger([_forecast(1, resolution_source="manual")])

    result = asyncio.run(resolve_due(None, _settings(), ledger, now=_NOW))

    assert result["skipped"] == 1
    assert ledger.record_outcome_calls == []


def test_resolve_due_untouched_when_fetch_reports_still_pending(monkeypatch):
    """A due-by-date-column forecast that `fetch_forward_return` itself
    says hasn't actually elapsed yet (defensive: `ledger.pending()` filters
    on `due_date`, but this belt-and-braces check means a clock skew or a
    stale `due_date` never produces a spurious resolution)."""
    async def _fake_fetch(redis, settings, symbol, source, decision, horizon_days, *, stale_days, entry_stale_days=None):
        return "pending", None

    monkeypatch.setattr(forecast_resolver_module, "fetch_forward_return", _fake_fetch)
    ledger = _FakeLedger([_forecast(1)])

    result = asyncio.run(resolve_due(None, _settings(), ledger, now=_NOW))

    assert result["skipped"] == 1
    assert ledger.record_outcome_calls == []


def test_resolve_due_one_bad_forecast_never_sinks_the_batch(monkeypatch):
    calls = {"n": 0}

    async def _fake_fetch(redis, settings, symbol, source, decision, horizon_days, *, stale_days, entry_stale_days=None):
        calls["n"] += 1
        if symbol == "BOOM":
            raise RuntimeError("adapter exploded")
        return "matured", {
            "entry": 100.0, "entry_date": "2026-01-05", "exit": 105.0, "exit_date": "2026-02-02",
            "value": 0.05, "priced_as_of": "t",
        }

    monkeypatch.setattr(forecast_resolver_module, "fetch_forward_return", _fake_fetch)
    ledger = _FakeLedger([
        _forecast(1, subject="BOOM", resolution_rule={"symbol": "BOOM"}),
        _forecast(2, subject="AAA", resolution_rule={"symbol": "AAA"}),
    ])

    result = asyncio.run(resolve_due(None, _settings(), ledger, now=_NOW))

    assert result["matured"] == 1
    assert len(result["errors"]) == 1
    assert result["errors"][0]["forecast_id"] == 1
    assert calls["n"] == 2  # the second forecast was still priced


# ── cross-sectional book median (M7) + parity with shadow._score_tool ──────
class _VerdictListResult:
    def __init__(self, rows: list[tuple[str, dict[str, Any]]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[str, dict[str, Any]]]:
        return self._rows


def test_book_median_and_hit_parity_with_score_tool(monkeypatch):
    """One-week fixture, one cross-sectional tool, full price coverage
    (events == rows): AAA top tercile (+1), BBB bottom tercile (-1), CCC
    mid/hold (None) — every `outcomes.hit` this resolver would assign must
    equal the direction hit `shadow._score_tool` assigns from the SAME
    underlying forward returns (acceptance, M7 parity test)."""
    returns = {"AAA": 0.10, "BBB": -0.10, "CCC": 0.0}
    session_calls = {"n": 0}

    async def _fake_fetch(redis, settings, symbol, source, decision, horizon_days, *, stale_days, entry_stale_days=None):
        v = returns[symbol]
        return "matured", {
            "entry": 100.0, "entry_date": "2026-01-05", "exit": 100.0 * (1 + v),
            "exit_date": "2026-02-02", "value": v, "priced_as_of": "t",
        }

    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        session_calls["n"] += 1
        yield SimpleNamespace(execute=_fake_execute)

    async def _fake_execute(stmt: Any) -> _VerdictListResult:
        return _VerdictListResult([("AAA", {}), ("BBB", {}), ("CCC", {})])

    monkeypatch.setattr(forecast_resolver_module, "fetch_forward_return", _fake_fetch)
    monkeypatch.setattr(forecast_resolver_module, "session_scope", _fake_session_scope)

    ledger = _FakeLedger([
        _forecast(1, issued_by="shadow:factor", subject="AAA", resolution_rule={"symbol": "AAA"},
                  target={"metric": "excess_return_vs_book_median", "comparator": ">", "threshold": 0.0}),
        _forecast(2, issued_by="shadow:factor", subject="BBB", resolution_rule={"symbol": "BBB"},
                  target={"metric": "excess_return_vs_book_median", "comparator": "<", "threshold": 0.0}),
    ])

    result = asyncio.run(resolve_due(None, _settings(), ledger, now=_NOW))

    assert result["matured"] == 2
    for call in ledger.record_outcome_calls:
        assert call["hit"] is True
        assert call["realized"]["book_median"] == pytest.approx(0.0)
        assert call["realized"]["cross_section_n"] == 3
    # memoized: ONE cross-section query backs BOTH forecasts' book median
    assert session_calls["n"] == 1

    # ── parity: the SAME observations, graded through the frozen
    #    `_score_tool`, must agree on hit/miss ─────────────────────────────
    observations = [
        {"symbol": "AAA", "report_week": "2026-W01", "direction": 1, "forward_return": 0.10, "exited": False},
        {"symbol": "BBB", "report_week": "2026-W01", "direction": -1, "forward_return": -0.10, "exited": False},
    ]
    full_cross_section = observations + [
        {"symbol": "CCC", "report_week": "2026-W01", "direction": None, "forward_return": 0.0, "exited": False},
    ]
    score = _score_tool(
        "factor", n_events=2, n_ungraded_null=1, n_pending=0, n_uncovered=0,
        observations=observations, full_cross_section=full_cross_section,
        weeks_of_history=1, min_weeks=1, horizon_days=28,
    )
    assert score["hit_rate"] == pytest.approx(1.0)  # both AAA and BBB hit — matches every resolver call above


def test_book_median_uncovered_when_no_priced_cross_section(monkeypatch):
    """The forecast's OWN symbol (AAA) prices fine, but every OTHER symbol
    the cross-section query returns fails to price — `book_median` comes
    back `None` (no priced cross-section to grade a relative call against),
    a pricing-coverage failure distinct from AAA's own resolution."""
    async def _fake_fetch(redis, settings, symbol, source, decision, horizon_days, *, stale_days, entry_stale_days=None):
        if symbol == "AAA":
            return "matured", {
                "entry": 100.0, "entry_date": "2026-01-05", "exit": 105.0,
                "exit_date": "2026-02-02", "value": 0.05, "priced_as_of": "t",
            }
        return "uncovered", None  # every cross-section symbol fails to price

    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        yield SimpleNamespace(execute=_fake_execute)

    async def _fake_execute(stmt: Any) -> _VerdictListResult:
        return _VerdictListResult([("OTHER", {})])  # AAA's own row not in this week's cross-section

    monkeypatch.setattr(forecast_resolver_module, "fetch_forward_return", _fake_fetch)
    monkeypatch.setattr(forecast_resolver_module, "session_scope", _fake_session_scope)

    ledger = _FakeLedger([
        _forecast(1, issued_by="shadow:factor", subject="AAA", resolution_rule={"symbol": "AAA"},
                  target={"metric": "excess_return_vs_book_median", "comparator": ">", "threshold": 0.0}),
    ])

    result = asyncio.run(resolve_due(None, _settings(), ledger, now=_NOW))

    assert result["uncovered"] == 1
    call = ledger.record_outcome_calls[0]
    assert call["resolution_status"] == "uncovered"
    assert "book median" in call["realized"]["reason"]


# ── resolve_due against REAL Postgres rows ─────────────
class _FakeRedis:
    """No-op cache seam — `HistoryStore` only needs `get`/`set`; the fixture
    bars below cover the whole `[decision-stale, exit+stale]` window so
    `_needs_backfill` never fires and no adapter/network call happens
    either, keeping this test Postgres-gated only, not network-gated."""

    async def get(self, key: str) -> None:
        return None

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        return None


def _pg_engine_or_skip():
    settings = Settings(
        postgres_user="u", postgres_password="p", postgres_db="d", redis_password="r", demo_mode=False,
    )
    engine = create_async_engine(settings.database_url, connect_args={"timeout": 3})

    async def _ping():
        async with engine.connect() as conn:
            await conn.execute(sa.text("SELECT 1"))

    try:
        asyncio.run(_ping())
    except Exception as exc:  # OperationalError, OSError/gaierror, TimeoutError, …
        asyncio.run(engine.dispose())
        pytest.skip(f"Postgres unreachable from host ({exc.__class__.__name__}): {exc}")
    return settings, engine


def test_resolve_due_against_real_forecast_and_daily_bars_rows():
    """No test exercised `resolve_due`/`fetch_forward_return` against a
    REAL `Forecast`/`daily_bars` row before this item (a known coverage gap) — inserts both for a synthetic symbol, runs
    `resolve_due` unmonkeypatched, asserts a real `outcomes` row lands
    `matured` with the correct `hit`."""
    settings, engine = _pg_engine_or_skip()
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        async with session_factory() as session:
            yield session

    symbol = f"F4TEST{int(time.time() * 1000)}"
    source = "yfinance"
    # `decision`/`horizon_days` are pinned at the `forecast_ledger_start_date`
    # floor with a 1-day horizon (not a "days ago" offset from real wall-clock
    # time) — `fetch_forward_return`'s own maturity check
    # (`exit_date > datetime.now(timezone.utc).date()`) reads the REAL clock
    # regardless of the `now` this test passes to `resolve_due`, so the fixed
    # `decision`/`exit_date` must already be in the past on any day this test
    # runs, not just "when it was written" — pinning to the floor guarantees
    # that permanently (the floor only ever moves forward, never back).
    decision = date(2026, 8, 31)
    horizon_days = 1
    exit_date = decision + timedelta(days=horizon_days)
    stale = settings.forecast_resolver_stale_days
    start_d = decision - timedelta(days=stale)
    end_d = exit_date + timedelta(days=stale)
    now = datetime.now(timezone.utc)
    published = datetime(2026, 8, 31, tzinfo=timezone.utc)

    row = dict(
        issued_by="human", issuer_version="human", subject_kind="symbol", subject=symbol,
        statement=f"{symbol} forward return over the next {horizon_days} days is greater than 0.",
        kind="binary", probability=0.6, direction=1,
        target={"metric": "forward_return", "comparator": ">", "threshold": 0.0},
        horizon_days=horizon_days, decision_date=decision, due_date=exit_date,
        report_week=None, published_at=published, first_seen_at=published, available_at=published,
        resolution_source=f"daily_bars:{source}",
        resolution_rule={
            "symbol": symbol, "source": source,
            "entry": "first close on/after decision_date within stale_days",
            "exit": "first close on/after due_date within stale_days", "book_median": None,
        },
        evidence_refs=[{"kind": "issuance_reason", "value": "test fixture"}],
    )

    async def _run():
        async with session_factory() as session:
            for d, close in ((start_d, 100.0), (decision, 100.0), (exit_date, 110.0), (end_d, 110.0)):
                session.add(DailyBarRow(
                    symbol=symbol, source=source, asset_type="equity", bar_date=d,
                    open=close, high=close, low=close, close=close, volume=0,
                ))
            await session.commit()

        ledger = ForecastLedger(redis=None, settings=settings)
        issued = await ledger.issue([row])
        assert issued == {"issued": 1, "duplicates": 0, "errors": []}
        forecast = await ledger.get_by_natural_key(
            issued_by="human", subject=symbol, decision_date=decision, horizon_days=horizon_days,
        )

        result = await resolve_due(_FakeRedis(), settings, ledger, now=now)

        async with session_factory() as session:
            outcome = (await session.execute(
                select(Outcome).where(Outcome.forecast_id == forecast["id"])
            )).scalar_one()
        return result, outcome

    monkeypatch_targets = [
        (forecasts_module, "session_scope", _fake_session_scope),
        (forecast_resolver_module, "session_scope", _fake_session_scope),
        (shadow_module, "session_scope", _fake_session_scope),
    ]
    originals = [(mod, name, getattr(mod, name)) for mod, name, _ in monkeypatch_targets]
    for mod, name, fake in monkeypatch_targets:
        setattr(mod, name, fake)
    try:
        result, outcome = asyncio.run(_run())
    finally:
        for mod, name, original in originals:
            setattr(mod, name, original)
        asyncio.run(engine.dispose())

    assert result == {"matured": 1, "uncovered": 0, "pushes": 0, "skipped": 0, "errors": []}
    assert outcome.resolution_status == "matured"
    assert outcome.hit is True
    assert outcome.realized["value"] == pytest.approx(0.1)
