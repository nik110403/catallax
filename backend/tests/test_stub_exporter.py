"""Parity: the real `WealthfolioBridge` pointed at the stub exporter (httpx ASGI
transport, no network) returns the same data as `DemoWealthfolioBridge`.

The bridge's trailing `stale=True` point is not asserted away: the stub's series ends
today, so the bridge appends none and plain equality holds."""
from __future__ import annotations

import httpx
import pytest

from app import stub_exporter
from app.config import Settings
from app.services.demo import WF_ASSETS
from app.services.wealthfolio import DemoWealthfolioBridge, WealthfolioBridge

_RealAsyncClient = httpx.AsyncClient  # captured before the monkeypatch


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, object] = {}

    async def get(self, key: str):
        return self.store.get(key)

    async def set(self, key: str, value, ex=None) -> None:
        self.store[key] = value


def _settings() -> Settings:
    return Settings(
        postgres_user="u", postgres_password="p", postgres_db="d", redis_password="r",
        demo_mode=False, wealthfolio_networth_url="http://stub",
    )


@pytest.fixture
def pair(monkeypatch):
    def factory(*args, **kwargs):
        return _RealAsyncClient(
            transport=httpx.ASGITransport(app=stub_exporter.app), timeout=kwargs.get("timeout"),
        )
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    s = _settings()
    return WealthfolioBridge(_FakeRedis(), s), DemoWealthfolioBridge(_FakeRedis(), s)


async def test_health(monkeypatch) -> None:
    async with _RealAsyncClient(transport=httpx.ASGITransport(app=stub_exporter.app), base_url="http://stub") as c:
        assert (await c.get("/health")).json() == {"ok": True}


@pytest.mark.parametrize("months", [3, 12, 36])
async def test_networth_parity(pair, months) -> None:
    real, demo = pair
    got = await real.networth(months)
    assert got and got == await demo.networth(months)


async def test_holdings_parity(pair) -> None:
    real, demo = pair
    assert await real.holdings() == await demo.holdings()


@pytest.mark.parametrize("symbol", [a.symbol for a in WF_ASSETS])
async def test_positions_history_parity(pair, symbol) -> None:
    real, demo = pair
    got = await real.positions_history(symbol)
    assert got and got == await demo.positions_history(symbol)


async def test_positions_history_unknown_symbol(pair) -> None:
    real, _ = pair
    assert await real.positions_history("NOPE") == []
