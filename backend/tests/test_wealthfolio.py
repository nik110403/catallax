"""Tests for the Wealthfolio bridge's SE4 exporter wiring: `cost_basis` on
`/holdings`, the new `positions_history()` bridge method (per-account-chain
aggregation, fallback-row exclusion, staleness forward-fill), and the demo
bridge's synthetic counterparts. No live egress: `httpx.AsyncClient` is
monkeypatched onto an `httpx.MockTransport` handler (mirrors
`test_macro_adapter.py`'s fake-client style, adapted for the no-injection-seam
constructor pattern used here).
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal

import httpx
import pytest

from app.config import Settings
from app.services import wealthfolio as wealthfolio_module
from app.services.demo import WF_ASSETS
from app.services.wealthfolio import DemoWealthfolioBridge, WealthfolioBridge


def _settings(**kw: object) -> Settings:
    kw.setdefault("demo_mode", False)
    return Settings(
        postgres_user="u", postgres_password="p", postgres_db="d", redis_password="r", **kw,
    )


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, object] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value, ex=None) -> None:
        self.store[key] = value


_RealAsyncClient = httpx.AsyncClient  # captured before any monkeypatch below


def _mock_client(handler):
    def factory(*args, **kwargs):
        return _RealAsyncClient(transport=httpx.MockTransport(handler), timeout=kwargs.get("timeout"))
    return factory


def _bridge(handler, redis=None) -> WealthfolioBridge:
    b = WealthfolioBridge(redis if redis is not None else _FakeRedis(), _settings())
    return b


# ── /holdings cost_basis (SE2 consumption) ────────────────────────────────
async def test_holdings_parses_cost_basis(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "base": "EUR", "as_of": "2026-08-15",
            "totals": {"investments": 100.0, "cash": 10.0, "total": 110.0},
            "by_account": [{"account": "A", "value": 110.0, "investments": 100.0, "cash": 10.0}],
            "by_asset": [
                {"symbol": "BTC", "name": "Bitcoin", "asset_class": "CRYPTO",
                 "quantity": 1.0, "value": 100.0, "cost_basis": 80.0},
            ],
        })
    monkeypatch.setattr(wealthfolio_module.httpx, "AsyncClient", _mock_client(handler))
    holdings = await _bridge(handler).holdings()
    assert holdings.by_asset[0].cost_basis == Decimal("80.0")


async def test_holdings_cost_basis_absent_is_none(monkeypatch) -> None:
    """Absent-tolerant: an old/cached exporter payload predating SE2 must not
    error — `cost_basis` simply stays `None`."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "base": "EUR", "as_of": "2026-08-15",
            "totals": {"investments": 100.0, "cash": 10.0, "total": 110.0},
            "by_account": [{"account": "A", "value": 110.0, "investments": 100.0, "cash": 10.0}],
            "by_asset": [
                {"symbol": "AAPL", "name": "Apple", "asset_class": "EQUITY", "quantity": 1.0, "value": 100.0},
            ],
        })
    monkeypatch.setattr(wealthfolio_module.httpx, "AsyncClient", _mock_client(handler))
    holdings = await _bridge(handler).holdings()
    assert holdings.by_asset[0].cost_basis is None


async def test_holdings_fallback_asset_maps_cost_basis_to_none(monkeypatch) -> None:
    """I1 (money-path hole): an asset with `fallback: true` (no quote at all —
    the exporter priced it off cost_basis) must never carry a real
    `cost_basis` into the advisor — that would make `value - cost_basis == 0`
    look like a genuine zero gain instead of an unpriceable one. Mapping to
    `None` routes it through the existing absent-tolerant conservative path."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "base": "EUR", "as_of": "2026-08-15",
            "totals": {"investments": 100.0, "cash": 10.0, "total": 110.0},
            "by_account": [{"account": "A", "value": 110.0, "investments": 100.0, "cash": 10.0}],
            "by_asset": [
                {"symbol": "OBSCURE", "name": "No Quote Asset", "asset_class": "EQUITY",
                 "quantity": 5.0, "value": 100.0, "cost_basis": 100.0, "fallback": True},
            ],
        })
    monkeypatch.setattr(wealthfolio_module.httpx, "AsyncClient", _mock_client(handler))
    holdings = await _bridge(handler).holdings()
    assert holdings.by_asset[0].cost_basis is None


# ── /positions-history (SE3 consumption) ──────────────────────────────────
async def test_positions_history_aggregates_per_account_chains_by_date(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["symbol"] == "BTC"
        return httpx.Response(200, json=[
            {"date": "2024-01-01", "symbol": "BTC", "quantity": 1.0, "value": 100.0, "cost_basis": 80.0, "fallback": False},
            {"date": "2024-01-01", "symbol": "BTC", "quantity": 0.5, "value": 50.0, "cost_basis": 40.0, "fallback": False},
            {"date": "2024-01-02", "symbol": "BTC", "quantity": 1.5, "value": 160.0, "cost_basis": 120.0, "fallback": False},
        ])
    monkeypatch.setattr(wealthfolio_module.httpx, "AsyncClient", _mock_client(handler))
    points = await _bridge(handler).positions_history("BTC", as_of=date(2024, 1, 2))
    assert len(points) == 2
    day1 = points[0]
    assert day1.date == date(2024, 1, 1)
    assert day1.quantity == Decimal("1.5")
    assert day1.value == Decimal("150.0")
    assert day1.cost_basis == Decimal("120.0")
    assert day1.stale is False


async def test_positions_history_excludes_fallback_rows(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            {"date": "2024-01-01", "symbol": "BTC", "quantity": 1.0, "value": 100.0, "cost_basis": 80.0, "fallback": False},
            {"date": "2024-01-02", "symbol": "BTC", "quantity": 999.0, "value": 9999.0, "cost_basis": 9999.0, "fallback": True},
        ])
    monkeypatch.setattr(wealthfolio_module.httpx, "AsyncClient", _mock_client(handler))
    # only the non-fallback 2024-01-01 row survives; no forward-fill needed
    # since it already reaches as_of.
    points = await _bridge(handler).positions_history("BTC", as_of=date(2024, 1, 1))
    assert len(points) == 1
    assert points[0].date == date(2024, 1, 1)
    assert points[0].value == Decimal("100.0")


async def test_positions_history_forward_fills_stale_tail(monkeypatch) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[
            {"date": "2024-01-01", "symbol": "BTC", "quantity": 1.0, "value": 100.0, "cost_basis": 80.0, "fallback": False},
        ])
    monkeypatch.setattr(wealthfolio_module.httpx, "AsyncClient", _mock_client(handler))
    points = await _bridge(handler).positions_history("BTC", as_of=date(2024, 1, 10))
    assert len(points) == 2
    assert points[0].stale is False
    tail = points[1]
    assert tail.date == date(2024, 1, 10)
    assert tail.stale is True
    assert tail.value == Decimal("100.0")  # carried forward from the last real row
    assert tail.cost_basis == Decimal("80.0")


# ── demo bridge synthetic basis/history (SE4) ─────────────────────────────
async def test_demo_holdings_carry_synthetic_cost_basis() -> None:
    bridge = DemoWealthfolioBridge(_FakeRedis(), _settings(demo_mode=True))
    holdings = await bridge.holdings()
    by_symbol = {a.symbol: a for a in holdings.by_asset}
    assert by_symbol["BTC"].cost_basis == next(a.cost_basis for a in WF_ASSETS if a.symbol == "BTC")
    # AAPL is fixture-seeded as an unrealised LOSS (cost_basis > value).
    assert by_symbol["AAPL"].cost_basis > by_symbol["AAPL"].value


async def test_demo_positions_history_pins_last_point_to_current_holding() -> None:
    bridge = DemoWealthfolioBridge(_FakeRedis(), _settings(demo_mode=True))
    points = await bridge.positions_history("BTC")
    assert len(points) > 100  # a multi-year daily series
    btc = next(a for a in WF_ASSETS if a.symbol == "BTC")
    assert points[-1].value == btc.value
    assert points[-1].cost_basis == btc.cost_basis
    assert all(p.stale is False for p in points)  # demo series always reaches today


async def test_demo_positions_history_unknown_symbol_is_empty() -> None:
    bridge = DemoWealthfolioBridge(_FakeRedis(), _settings(demo_mode=True))
    assert await bridge.positions_history("NOTASYMBOL") == []
