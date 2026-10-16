"""Regression tests for the Phase-14c DEMO_MODE data-leak close-out: signals,
backtests, lab runs, and algo (paper trading) had no demo branch and read the
owner's REAL DB-persisted rows. Mirrors `test_demo_seam.py`'s style: pure unit
tests, hand-rolled fakes, no `unittest.mock`, no network/Postgres/Redis
actually touched.

Each domain gets two kinds of proof:
  * demo_mode=True: list/collection reads come back empty, detail/by-id reads
    404 (the SAME NotFound the endpoint already raises for an unknown id),
    account/equity reads come back zeroed, and mutations are rejected — all
    BEFORE any DB/Redis call, so a stub `object()` stands in for redis and no
    real database is required.
  * demo_mode=False: the real path is UNCHANGED. Proven by monkeypatching the
    module's `session_scope` to a stand-in that raises `_DBTouched` the
    instant it's called — reaching it proves the new guard did NOT
    short-circuit and the original DB-bound code still runs.
"""
from __future__ import annotations

from datetime import date

import pytest
from fastapi import HTTPException

import app.routers.algo as algo_router_module
import app.services.algo as algo_module
import app.services.backtest as backtest_module
import app.services.lab as lab_module
import app.services.signals as signals_module
from app.adapters.backtest.base import BacktestConfig
from app.adapters.lab.base import FactorConfig, ModelConfig
from app.config import Settings
from app.errors import DemoModeReadOnly, InvalidRequest, NotFound
from app.routers.algo import (
    StrategyCreate,
    _set_strategy_status,
    create_strategy,
    delete_strategy,
    list_strategies,
    stream,
)
from app.services.algo import AlgoService, OrderSpec
from app.services.backtest import BacktestService
from app.services.lab import LabService
from app.services.signals import SignalEngine


def _settings(**kw: object) -> Settings:
    kw.setdefault("demo_mode", False)
    return Settings(
        postgres_user="u", postgres_password="p", postgres_db="d", redis_password="r", **kw,
    )


class _DBTouched(Exception):
    """Raised the instant the faked `session_scope` is called — proves
    execution reached a real DB read/write site, i.e. the demo_mode guard
    upstream was NOT taken."""


def _boom_session_scope():
    raise _DBTouched()


def _bt_cfg(**kw: object) -> BacktestConfig:
    base = dict(name="t", signal_id="xs_test", start=date(2024, 1, 1), end=date(2024, 12, 31))
    base.update(kw)
    return BacktestConfig(**base)


class _FakeRunner:
    async def validate(self, cfg: object) -> list:
        return []


# ── signals ────────────────────────────────────────────────────────
async def test_signals_catalog_empty_in_demo_mode() -> None:
    engine = SignalEngine(object(), _settings(demo_mode=True))
    assert (await engine.catalog()).signals == []


async def test_signals_catalog_reaches_db_when_demo_off(monkeypatch) -> None:
    monkeypatch.setattr(signals_module, "session_scope", _boom_session_scope)
    engine = SignalEngine(object(), _settings(demo_mode=False))
    with pytest.raises(_DBTouched):
        await engine.catalog()


async def test_signals_grade_404_in_demo_mode() -> None:
    engine = SignalEngine(object(), _settings(demo_mode=True))
    with pytest.raises(NotFound):
        await engine.grade("xs_test")


async def test_signals_cross_section_404_in_demo_mode() -> None:
    engine = SignalEngine(object(), _settings(demo_mode=True))
    with pytest.raises(NotFound):
        await engine.cross_section("xs_test", "2024-01-01")


async def test_signals_load_rows_reaches_db_when_demo_off(monkeypatch) -> None:
    monkeypatch.setattr(signals_module, "session_scope", _boom_session_scope)
    engine = SignalEngine(object(), _settings(demo_mode=False))
    with pytest.raises(_DBTouched):
        await engine._load_rows("xs_test")


async def test_signals_emit_rejected_in_demo_mode() -> None:
    engine = SignalEngine(object(), _settings(demo_mode=True))
    with pytest.raises(DemoModeReadOnly):
        await engine.emit([{"signal_id": "sig1", "as_of_date": date(2024, 1, 1), "symbol": "AAPL", "value": 1.0}])


async def test_signals_emit_reaches_db_when_demo_off(monkeypatch) -> None:
    monkeypatch.setattr(signals_module, "session_scope", _boom_session_scope)
    engine = SignalEngine(object(), _settings(demo_mode=False))
    with pytest.raises(_DBTouched):
        await engine.emit([{"signal_id": "sig1", "as_of_date": date(2024, 1, 1), "symbol": "AAPL", "value": 1.0}])


async def test_signals_seed_rejected_in_demo_mode() -> None:
    engine = SignalEngine(object(), _settings(demo_mode=True))
    with pytest.raises(DemoModeReadOnly):
        await engine.seed()


async def test_signals_seed_unchanged_when_demo_off() -> None:
    """Real mode: the pre-existing SIGNAL_SEED_ENABLED=false guard must still
    fire (InvalidRequest, not DemoModeReadOnly) — proves the new demo check
    didn't clobber this existing one."""
    engine = SignalEngine(object(), _settings(demo_mode=False, signal_seed_enabled=False))
    with pytest.raises(InvalidRequest):
        await engine.seed()


# ── backtests ──────────────────────────────────────────────────────
async def test_backtests_list_empty_in_demo_mode() -> None:
    svc = BacktestService(object(), _settings(demo_mode=True))
    assert (await svc.list()).jobs == []


async def test_backtests_list_reaches_db_when_demo_off(monkeypatch) -> None:
    monkeypatch.setattr(backtest_module, "session_scope", _boom_session_scope)
    svc = BacktestService(object(), _settings(demo_mode=False))
    with pytest.raises(_DBTouched):
        await svc.list()


async def test_backtests_get_404_in_demo_mode() -> None:
    svc = BacktestService(object(), _settings(demo_mode=True))
    with pytest.raises(NotFound):
        await svc.get("some-job-id")


async def test_backtests_report_404_in_demo_mode() -> None:
    svc = BacktestService(object(), _settings(demo_mode=True))
    with pytest.raises(NotFound):
        await svc.report("some-job-id")


async def test_backtests_enqueue_rejected_in_demo_mode() -> None:
    svc = BacktestService(object(), _settings(demo_mode=True))
    with pytest.raises(DemoModeReadOnly):
        await svc.enqueue(_bt_cfg())


async def test_backtests_enqueue_reaches_validate_when_demo_off(monkeypatch) -> None:
    monkeypatch.setattr(backtest_module, "session_scope", _boom_session_scope)
    svc = BacktestService(object(), _settings(demo_mode=False))
    with pytest.raises(_DBTouched):
        await svc.enqueue(_bt_cfg())


# ── lab ────────────────────────────────────────────────────────────
async def test_lab_list_empty_in_demo_mode() -> None:
    svc = LabService(object(), _settings(demo_mode=True))
    assert (await svc.list()).jobs == []


async def test_lab_list_reaches_db_when_demo_off(monkeypatch) -> None:
    monkeypatch.setattr(lab_module, "session_scope", _boom_session_scope)
    svc = LabService(object(), _settings(demo_mode=False))
    with pytest.raises(_DBTouched):
        await svc.list()


async def test_lab_get_404_in_demo_mode() -> None:
    svc = LabService(object(), _settings(demo_mode=True))
    with pytest.raises(NotFound):
        await svc.get("some-job-id")


async def test_lab_report_404_in_demo_mode() -> None:
    svc = LabService(object(), _settings(demo_mode=True))
    with pytest.raises(NotFound):
        await svc.report("some-job-id")


async def test_lab_enqueue_factor_rejected_in_demo_mode() -> None:
    svc = LabService(object(), _settings(demo_mode=True))
    with pytest.raises(DemoModeReadOnly):
        await svc.enqueue_factor(FactorConfig(name="t"))


async def test_lab_enqueue_model_rejected_in_demo_mode() -> None:
    svc = LabService(object(), _settings(demo_mode=True))
    with pytest.raises(DemoModeReadOnly):
        await svc.enqueue_model(ModelConfig(name="t"))


async def test_lab_enqueue_factor_reaches_db_when_demo_off(monkeypatch) -> None:
    monkeypatch.setattr(lab_module, "session_scope", _boom_session_scope)
    svc = LabService(object(), _settings(demo_mode=False))
    with pytest.raises(_DBTouched):
        await svc.enqueue_factor(FactorConfig(name="t"))


# ── algo: orders / positions / account / kill switch ────────────────
async def test_algo_list_orders_empty_in_demo_mode() -> None:
    svc = AlgoService(object(), _settings(demo_mode=True))
    assert await svc.list_orders() == []


async def test_algo_list_fills_empty_in_demo_mode() -> None:
    svc = AlgoService(object(), _settings(demo_mode=True))
    assert await svc.list_fills() == []


async def test_algo_positions_empty_in_demo_mode() -> None:
    svc = AlgoService(object(), _settings(demo_mode=True))
    assert await svc.positions() == []


async def test_algo_account_zeroed_in_demo_mode() -> None:
    svc = AlgoService(object(), _settings(demo_mode=True))
    assert await svc.account() == {
        "cash": 0.0, "equity": 0.0, "positions_value": 0.0, "day_pnl": None, "snapshots": [],
    }


async def test_algo_reconciliation_clean_in_demo_mode() -> None:
    svc = AlgoService(object(), _settings(demo_mode=True))
    assert await svc.reconciliation() == {
        "clean": True, "positions": [], "cash": {"stored": 0.0, "replayed": 0.0, "match": True},
    }


async def test_algo_submit_order_rejected_in_demo_mode() -> None:
    svc = AlgoService(object(), _settings(demo_mode=True))
    with pytest.raises(HTTPException) as exc_info:
        await svc.submit_order(OrderSpec(symbol="AAPL", side="buy", qty=1))
    assert exc_info.value.status_code == 403
    assert exc_info.value.detail["code"] == "DEMO_MODE_READONLY"


async def test_algo_cancel_order_rejected_in_demo_mode() -> None:
    svc = AlgoService(object(), _settings(demo_mode=True))
    with pytest.raises(HTTPException) as exc_info:
        await svc.cancel_order(1)
    assert exc_info.value.status_code == 403


async def test_algo_kill_rejected_in_demo_mode() -> None:
    svc = AlgoService(object(), _settings(demo_mode=True))
    with pytest.raises(HTTPException) as exc_info:
        await svc.kill()
    assert exc_info.value.status_code == 403


async def test_algo_resume_rejected_in_demo_mode() -> None:
    svc = AlgoService(object(), _settings(demo_mode=True))
    with pytest.raises(HTTPException) as exc_info:
        await svc.resume()
    assert exc_info.value.status_code == 403


async def test_algo_positions_reaches_db_when_demo_off(monkeypatch) -> None:
    monkeypatch.setattr(algo_module, "session_scope", _boom_session_scope)
    svc = AlgoService(object(), _settings(demo_mode=False))
    with pytest.raises(_DBTouched):
        await svc.positions()


async def test_algo_submit_order_reaches_db_when_demo_off(monkeypatch) -> None:
    monkeypatch.setattr(algo_module, "session_scope", _boom_session_scope)
    svc = AlgoService(object(), _settings(demo_mode=False))
    with pytest.raises(_DBTouched):
        await svc.submit_order(OrderSpec(symbol="AAPL", side="buy", qty=1))


async def test_algo_kill_reaches_db_when_demo_off(monkeypatch) -> None:
    monkeypatch.setattr(algo_module, "session_scope", _boom_session_scope)
    svc = AlgoService(object(), _settings(demo_mode=False))
    with pytest.raises(_DBTouched):
        await svc.kill()


# ── algo: strategy CRUD (router-level — no service seam of its own) ─
async def test_algo_list_strategies_empty_in_demo_mode() -> None:
    assert await list_strategies(_settings(demo_mode=True)) == {"strategies": []}


async def test_algo_list_strategies_reaches_db_when_demo_off(monkeypatch) -> None:
    monkeypatch.setattr(algo_router_module, "session_scope", _boom_session_scope)
    with pytest.raises(_DBTouched):
        await list_strategies(_settings(demo_mode=False))


async def test_algo_create_strategy_rejected_in_demo_mode() -> None:
    body = StrategyCreate(name="t", signal_id="xs_test", universe=["AAPL"])
    with pytest.raises(HTTPException) as exc_info:
        await create_strategy(body, _FakeRunner(), _settings(demo_mode=True))
    assert exc_info.value.status_code == 403


async def test_algo_create_strategy_reaches_db_when_demo_off(monkeypatch) -> None:
    monkeypatch.setattr(algo_router_module, "session_scope", _boom_session_scope)
    body = StrategyCreate(name="t", signal_id="xs_test", universe=["AAPL"])
    with pytest.raises(_DBTouched):
        await create_strategy(body, _FakeRunner(), _settings(demo_mode=False))


async def test_algo_set_strategy_status_rejected_in_demo_mode() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await _set_strategy_status(1, "active", _settings(demo_mode=True))
    assert exc_info.value.status_code == 403


async def test_algo_set_strategy_status_reaches_db_when_demo_off(monkeypatch) -> None:
    monkeypatch.setattr(algo_router_module, "session_scope", _boom_session_scope)
    with pytest.raises(_DBTouched):
        await _set_strategy_status(1, "active", _settings(demo_mode=False))


async def test_algo_delete_strategy_rejected_in_demo_mode() -> None:
    with pytest.raises(HTTPException) as exc_info:
        await delete_strategy(1, _settings(demo_mode=True))
    assert exc_info.value.status_code == 403


async def test_algo_delete_strategy_reaches_db_when_demo_off(monkeypatch) -> None:
    monkeypatch.setattr(algo_router_module, "session_scope", _boom_session_scope)
    with pytest.raises(_DBTouched):
        await delete_strategy(1, _settings(demo_mode=False))


# ── algo: SSE stream (leaks real portfolio data if not gated) ─────
class _PubsubReached(Exception):
    """Raised by `_FakeRedis.pubsub()` so a test can prove the real
    (non-demo) subscribe path was reached, without needing to fake the rest
    of the redis-py PubSub API (subscribe/get_message/unsubscribe/aclose)."""


class _FakeRedis:
    def __init__(self) -> None:
        self.pubsub_called = False

    def pubsub(self) -> None:
        self.pubsub_called = True
        raise _PubsubReached()


class _FakeRequest:
    """`is_disconnected()` is False on the first call, so the demo loop's
    heartbeat body runs once, then True on every call after — so neither the
    loop nor the test can hang on `asyncio.sleep(15.0)`."""

    def __init__(self) -> None:
        self._calls = 0

    async def is_disconnected(self) -> bool:
        self._calls += 1
        return self._calls > 1


async def test_algo_stream_heartbeat_only_in_demo_mode() -> None:
    redis = _FakeRedis()
    response = await stream(_FakeRequest(), redis, _settings(demo_mode=True))
    chunk = await anext(response.body_iterator)
    assert chunk == ": ping\n\n"
    assert redis.pubsub_called is False


async def test_algo_stream_reaches_pubsub_when_demo_off() -> None:
    redis = _FakeRedis()
    response = await stream(_FakeRequest(), redis, _settings(demo_mode=False))
    with pytest.raises(_PubsubReached):
        await anext(response.body_iterator)
    assert redis.pubsub_called is True
