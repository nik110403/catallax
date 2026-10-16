"""FundamentalsAdapter routes through the shared Yahoo mapping.

A raw WF symbol sent to Yahoo came back 404 (quote not found) or "possibly
delisted", and the fundamentals build failed — `_build` called `yf.Ticker(symbol)` on the raw WF symbol, bypassing
YFINANCE_SYMBOL_ALIASES and CRYPTO_YF_SYMBOLS. Coins have no equity
statements: they must be skipped with no Yahoo call at all.
"""
from __future__ import annotations

import sys
import types
from typing import Any

import pytest

from app.adapters.fundamentals_adapter import FundamentalsAdapter
from app.config import Settings


def _settings(**kw: Any) -> Settings:
    kw.setdefault("demo_mode", False)
    return Settings(postgres_user="u", postgres_password="p", postgres_db="d", redis_password="r", **kw)


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.store.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value


@pytest.fixture
def fake_yf(monkeypatch):
    calls: list[str] = []

    class _Ticker:
        def __init__(self, sym: str) -> None:
            calls.append(sym)
            self.income_stmt = None
            self.balance_sheet = None
            self.cashflow = None
            self.info = {"longName": "Nestle SA", "currency": "CHF"}

    mod = types.ModuleType("yfinance")
    mod.Ticker = _Ticker  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "yfinance", mod)
    return calls


_S = dict(
    yfinance_symbol_aliases="SAP=SAP.DE,EXMPLCOIN=EXMPL2-USD",
    crypto_yf_symbols="BTC,EXMPL5,EXMPL6,EXMPL7,EXMPL8",
)


@pytest.mark.parametrize("sym", ["EXMPL5", "EXMPL6", "EXMPL7", "EXMPL8", "EXMPLCOIN", "btc"])
async def test_crypto_holdings_skip_fundamentals_without_calling_yahoo(fake_yf, sym):
    adapter = FundamentalsAdapter(_FakeRedis(), _settings(**_S))
    data, stale = await adapter.statements(sym)
    assert fake_yf == []  # no yf.Ticker at all
    assert stale is False  # not an outage — equity statements simply don't exist
    assert data["fiscal_years"] == [] and data["income"] == {}
    assert data["symbol"] == sym.upper()
    assert data["not_applicable"]  # explicit, human-readable reason


async def test_equity_fundamentals_use_the_aliased_yahoo_ticker(fake_yf):
    adapter = FundamentalsAdapter(_FakeRedis(), _settings(**_S))
    data = adapter._build("SAP")
    assert fake_yf == ["SAP.DE"]
    assert data["symbol"] == "SAP"  # caller-facing symbol unchanged
    assert data["name"] == "Nestle SA"


async def test_plain_equity_passes_through(fake_yf):
    adapter = FundamentalsAdapter(_FakeRedis(), _settings(**_S))
    adapter._build("AAPL")
    assert fake_yf == ["AAPL"]


# ── review-225d5f4 finding 4: skip by known asset class, not ticker spelling ──
_SOL = dict(crypto_yf_symbols="BTC,SOL,EXMPL5", yfinance_symbol_aliases="SAP=SAP.DE")


async def test_equity_whose_ticker_collides_with_a_coin_gets_real_fundamentals(fake_yf):
    """A holding WF classes as an equity but spelled like a listed coin
    (`SOL` is in CRYPTO_YF_SYMBOLS) must NOT be 'not applicable' — and Yahoo
    must be asked for the equity ticker, not `SOL-USD`."""
    adapter = FundamentalsAdapter(_FakeRedis(), _settings(**_SOL))
    data, _stale = await adapter.statements("SOL", asset_class="EQUITY")
    assert fake_yf == ["SOL"]
    assert "not_applicable" not in data


async def test_known_crypto_class_skips_even_when_the_ticker_is_not_listed(fake_yf):
    adapter = FundamentalsAdapter(_FakeRedis(), _settings(**_SOL))
    data, stale = await adapter.statements("EXMPL7",asset_class="crypto")
    assert fake_yf == [] and stale is False
    assert data["not_applicable"]


async def test_unknown_class_falls_back_to_the_ticker_heuristic(fake_yf):
    adapter = FundamentalsAdapter(_FakeRedis(), _settings(**_SOL))
    data, _ = await adapter.statements("SOL")  # class unknown (e.g. Research tab lookup)
    assert fake_yf == [] and data["not_applicable"]
    data, _ = await adapter.statements("SOL", asset_class="")  # blank class == unknown
    assert fake_yf == [] and data["not_applicable"]


async def test_alias_still_wins_for_a_known_equity(fake_yf):
    adapter = FundamentalsAdapter(_FakeRedis(), _settings(**_SOL))
    await adapter.statements("SAP", asset_class="EQUITY")
    assert fake_yf == ["SAP.DE"]
