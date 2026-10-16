"""UPSTREAM_MODE=cache-only (hosted-demo E3, DEMO-SPEC §5.2): on a cache miss no
adapter/service opens an outbound connection — stale data (or an empty result
marked stale) is served instead. Every outbound entry point is patched to
record-and-raise, so a leaked call fails the test even if an adapter would
swallow the exception. The warm job (`app.demo_warm`) forces live mode."""
from __future__ import annotations

import asyncio
import json
import socket
from datetime import datetime, timedelta, timezone

import httpx
import pytest
import yfinance

from app.adapters.base import Quote
from app.adapters.breadth_adapter import BreadthAdapter
from app.adapters.coingecko_adapter import CoinGeckoAdapter
from app.adapters.events.base import Universe
from app.adapters.events.edgar import EdgarSourceAdapter
from app.adapters.events.rss import RssSourceAdapter
from app.adapters.factory import build_chain
from app.adapters.fundamentals_adapter import FundamentalsAdapter
from app.adapters.macro_adapter import MacroAdapter
from app.adapters.sentiment_adapter import SentimentAdapter
from app.adapters.shiller_adapter import ShillerAdapter
from app.adapters.stooq_adapter import StooqAdapter
from app.adapters.yfinance_adapter import YFinanceAdapter
from app.config import get_settings
from app.errors import UpstreamError
from app.services import markets, news

_NOW = datetime(2026, 1, 2, tzinfo=timezone.utc)


class _FakeRedis:
    """Dict-backed stand-in for the few redis.asyncio calls used."""

    def __init__(self) -> None:
        self.kv: dict[str, str] = {}

    async def get(self, key: str):
        return self.kv.get(key)

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.kv[key] = value

    async def incr(self, key: str) -> int:
        self.kv[key] = str(int(self.kv.get(key, "0")) + 1)
        return int(self.kv[key])

    async def ttl(self, key: str) -> int:
        return 60

    async def expire(self, key: str, seconds: int) -> None:
        return None


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Patch every outbound entry point to record + raise; returns the record."""
    seen: list[str] = []

    def _trap(name: str):
        def _raise(*a, **k):  # noqa: ANN002, ANN003
            seen.append(name)
            raise AssertionError(f"outbound call via {name}")

        return _raise

    async def _atrap(*a, **k):  # noqa: ANN002, ANN003
        seen.append("httpx")
        raise AssertionError("outbound call via httpx")

    monkeypatch.setattr(httpx.AsyncClient, "send", _atrap)
    monkeypatch.setattr(httpx.AsyncClient, "request", _atrap)
    monkeypatch.setattr(yfinance, "Ticker", _trap("yfinance.Ticker"))
    monkeypatch.setattr(yfinance, "download", _trap("yfinance.download"))
    monkeypatch.setattr(socket.socket, "connect", _trap("socket.connect"))
    monkeypatch.setattr(asyncio.BaseEventLoop, "create_connection", _atrap)
    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", _atrap)
    # modules that did `import yfinance as yf` share the same module object
    return seen


@pytest.fixture
def settings(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("UPSTREAM_MODE", "cache-only")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


def _quote(sym: str, source: str) -> Quote:
    return Quote(symbol=sym, price="10.5", currency="USD", as_of=_NOW, source=source)


def test_default_mode_is_live() -> None:
    get_settings.cache_clear()
    assert get_settings().upstream_mode == "live"


@pytest.mark.parametrize("cls", [YFinanceAdapter, CoinGeckoAdapter, StooqAdapter])
async def test_price_adapters_never_fetch(cls, settings, calls) -> None:
    redis = _FakeRedis()
    ad = cls(redis, settings)
    with pytest.raises(UpstreamError):
        await ad.get_quote("AAPL")  # miss, no stale
    with pytest.raises(UpstreamError):
        await ad.get_history("AAPL", _NOW - timedelta(days=5), _NOW, "1d")
    assert await ad.search("apple") == []
    # a stale quote is served; a fresh-key hit too
    await redis.set(ad._stale_key("AAPL"), _quote("AAPL", ad.name).model_dump_json())
    assert (await ad.get_quote("AAPL")).symbol == "AAPL"
    assert calls == []


async def test_fallback_chain_never_fetches(settings, calls) -> None:
    redis = _FakeRedis()
    chain = build_chain("equity", redis, settings)
    with pytest.raises(UpstreamError):
        await chain.get_quote("MSFT")
    with pytest.raises(UpstreamError):
        await chain.get_history("MSFT", _NOW - timedelta(days=5), _NOW, "1d")
    assert calls == []


async def test_ws_broadcast_path_serves_cache_only(settings, calls) -> None:
    # routers/ws.py streams via build_adapter(...).get_quote — same seam as above.
    from app.adapters.factory import build_adapter

    redis = _FakeRedis()
    ad = build_adapter("yfinance", redis, settings)
    await redis.set(ad._quote_key("BTC"), _quote("BTC", "yfinance").model_dump_json())
    assert (await ad.get_quote("BTC")).price == 10.5
    with pytest.raises(UpstreamError):
        await ad.get_quote("ETH")
    assert calls == []


async def test_cached_json_adapters_return_empty_stale(settings, calls) -> None:
    redis = _FakeRedis()
    assert await ShillerAdapter(redis, settings).cape() == ({}, True)
    assert await BreadthAdapter(redis, settings).diary() == ({}, True)
    sent = SentimentAdapter(redis, settings)
    assert await sent.fear_greed() == ({}, True)
    assert await sent.cot() == ({}, True)
    assert await FundamentalsAdapter(redis, settings).statements("AAPL") == ({}, True)
    assert calls == []


async def test_cached_json_adapters_serve_stale_copy(settings, calls) -> None:
    redis = _FakeRedis()
    blob = {"points": [{"date": "2026-01-01", "cape": 30.0}], "cape": 30.0}
    await redis.set("shiller:cape:stale", json.dumps(blob))
    assert await ShillerAdapter(redis, settings).cape() == (blob, True)
    assert calls == []


async def test_macro_never_fetches(settings, calls) -> None:
    settings.openbb_fred_api_key = "k"  # else calendar() short-circuits before any fetch
    ad = MacroAdapter(_FakeRedis(), settings)
    for coro in (
        ad.yield_curve("US"), ad.rates(), ad.indicators(), ad.series("gdp"), ad.calendar(),
    ):
        with pytest.raises(UpstreamError):
            await coro
    assert calls == []


async def test_markets_and_news_serve_empty_or_stale(settings, calls) -> None:
    redis = _FakeRedis()
    ov = await markets.get_overview(redis, settings)
    assert ov.groups == [] and ov.composite.label == "No data"
    assert "markets:overview" not in redis.kv  # an empty answer is never cached
    assert (await markets.get_quakes(redis, settings))["features"] == []
    assert await news.get_news(redis, settings) == []
    await redis.set("markets:quakes:stale", json.dumps({"type": "FeatureCollection", "features": [1]}))
    assert (await markets.get_quakes(redis, settings))["features"] == [1]
    assert calls == []


async def test_event_pollers_never_fetch(settings, calls) -> None:
    rss = RssSourceAdapter(
        source="s", feed="f", url="https://example.invalid/feed", event_type="t",
        hints=[], settings=settings,
    )
    edgar = EdgarSourceAdapter(settings, _FakeRedis(), Universe())
    for ad in (rss, edgar):
        with pytest.raises(UpstreamError):
            await ad.poll({}, None, None)
    assert calls == []


async def test_live_mode_still_reaches_the_client(monkeypatch, calls) -> None:
    """Control: with the default live mode the same trap IS hit, so the tests
    above prove the guard and not a broken trap."""
    get_settings.cache_clear()
    s = get_settings()
    assert s.upstream_mode == "live"
    await ShillerAdapter(_FakeRedis(), s).cape()
    assert "httpx" in calls or "socket.connect" in calls


def test_warm_symbols_default_and_override(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.demo_warm import warm_symbols
    from app.services.demo import WF_ASSETS

    monkeypatch.delenv("DEMO_WARM_SYMBOLS", raising=False)
    get_settings.cache_clear()
    assert [s for s, _ in warm_symbols(get_settings())] == [a.symbol for a in WF_ASSETS]
    monkeypatch.setenv("DEMO_WARM_SYMBOLS", "aapl, btc,zzz")
    get_settings.cache_clear()
    assert warm_symbols(get_settings()) == [("AAPL", "equity"), ("BTC", "crypto"), ("ZZZ", "equity")]
    get_settings.cache_clear()


async def test_warm_job_forces_live_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.demo_warm as dw

    monkeypatch.setenv("UPSTREAM_MODE", "cache-only")
    get_settings.cache_clear()
    seen: dict[str, str] = {}

    async def _fake_warm(redis, settings):  # noqa: ANN001
        seen["mode"] = settings.upstream_mode
        return 0, 0

    class _R:
        @staticmethod
        def from_url(*a, **k):  # noqa: ANN002, ANN003
            class _C:
                async def aclose(self) -> None: ...
            return _C()

    monkeypatch.setattr(dw, "warm", _fake_warm)
    monkeypatch.setattr(dw, "Redis", _R)
    assert await dw._main() == 0
    assert seen["mode"] == "live"
    monkeypatch.delenv("UPSTREAM_MODE", raising=False)
    get_settings.cache_clear()
