"""Demo warm coverage: after a warm with fake adapters, every read the cache-only
demo makes for the ticker bar, the market-research reports and the equity-research
screen hits only keys the warm wrote (no UPSTREAM_DISABLED). Holdings-independent
universes were the gap: the warm used to cover the persona's holdings only."""
from __future__ import annotations

import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from app.adapters.base import PriceSourceAdapter, Quote
from app.adapters.breadth_adapter import BreadthAdapter
from app.adapters.envelope import Envelope
from app.adapters.factory import build_adapter, get_price_adapter
from app.adapters.fundamentals_adapter import FundamentalsAdapter
from app.adapters.macro_adapter import MacroAdapter
from app.adapters.sentiment_adapter import SentimentAdapter
from app.adapters.shiller_adapter import ShillerAdapter
from app.config import get_settings
from app.services import markets, news
from app.upstream import UpstreamDisabled
from tests.test_upstream_mode import _FakeRedis

_TICKER_TSX = Path(__file__).resolve().parents[2] / "frontend/src/widgets/TickerBar.tsx"


def test_ticker_feed_matches_frontend_fallback_list() -> None:
    import app.demo_warm as dw

    if not _TICKER_TSX.exists():
        pytest.skip("frontend not in this checkout")
    block = _TICKER_TSX.read_text().split("const FEED", 1)[1].split("];", 1)[0]
    assert tuple(re.findall(r'token:\s*"([^"]+)"', block)) == dw.TICKER_FEED


@pytest.fixture
def warmed(monkeypatch: pytest.MonkeyPatch):
    """Run `warm()` live with fake adapters; yield (redis, stored_history_keys)."""
    import app.demo_warm as dw

    monkeypatch.setenv("UPSTREAM_MODE", "live")
    monkeypatch.delenv("DEMO_WARM_SYMBOLS", raising=False)
    get_settings.cache_clear()
    redis = _FakeRedis()
    stored: set[tuple[str, str]] = set()

    async def _quote(self, symbol):  # noqa: ANN001
        return Quote(symbol=symbol, price=Decimal("1"), currency="USD",
                     as_of=datetime.now(timezone.utc), source=self.name)

    async def _no_limit(self):  # noqa: ANN001
        return None

    @asynccontextmanager
    async def _scope():
        yield object()

    class _Store:
        def __init__(self, db, redis, settings) -> None:  # noqa: ANN001
            pass

        async def get_daily(self, adapter, symbol, start, end):  # noqa: ANN001
            stored.add((adapter.name, adapter.history_symbol(symbol.upper()).upper()))

    async def _noop(*a, **k):  # noqa: ANN002, ANN003
        return None

    for cls in {c for c in PriceSourceAdapter.__subclasses__()}:
        if "_fetch_quote" in cls.__dict__:
            monkeypatch.setattr(cls, "_fetch_quote", _quote)
    monkeypatch.setattr(PriceSourceAdapter, "_enforce_rate_limit", _no_limit)
    monkeypatch.setattr(dw, "session_scope", _scope)
    monkeypatch.setattr(dw, "HistoryStore", _Store)
    for name in ("get_overview", "get_quakes"):
        monkeypatch.setattr(markets, name, _noop)
    monkeypatch.setattr(news, "get_news", _noop)
    real_bundle = dw.MarketResearchEngine.bundle
    monkeypatch.setattr(dw.MarketResearchEngine, "bundle", _noop)
    for cls, name in ((ShillerAdapter, "cape"), (BreadthAdapter, "diary"),
                      (SentimentAdapter, "fear_greed"), (SentimentAdapter, "cot"),
                      (FundamentalsAdapter, "statements")):
        monkeypatch.setattr(cls, name, _noop)

    return dw, redis, stored, real_bundle


@pytest.fixture
async def warm_run(warmed, monkeypatch: pytest.MonkeyPatch):
    dw, redis, stored, real_bundle = warmed
    ok, core, optional = await dw.warm(redis, get_settings())
    assert core == 0 and optional == 0 and ok > 0
    monkeypatch.setattr(dw.MarketResearchEngine, "bundle", real_bundle)
    monkeypatch.setenv("UPSTREAM_MODE", "cache-only")
    get_settings.cache_clear()
    yield redis, stored, get_settings()
    monkeypatch.delenv("UPSTREAM_MODE", raising=False)
    get_settings.cache_clear()


async def test_cache_only_ticker_and_prices_reads_hit_warmed_quotes(warm_run) -> None:
    import app.demo_warm as dw

    redis, _stored, s = warm_run
    for token in (*dw.TICKER_FEED, s.market_research_benchmark):
        sym, _, src = token.partition(":")
        # websocket ticker: `build_adapter(src or default)` per symbol (routers/ws.py)
        ws_adapter = build_adapter(src or s.price_source_default, redis, s)
        assert (await ws_adapter.get_quote(sym)).price == Decimal("1"), token
        # REST /prices/{symbol}[?source=]: the `get_price_adapter` dependency
        rest_adapter = get_price_adapter(src or None, redis, s)
        assert (await rest_adapter.get_quote(sym)).price == Decimal("1"), token
    for sym in ("SPY", "^GSPC", "EURUSD=X", "GC=F", "GOOGL", "AMZN", "TSLA"):
        assert (await get_price_adapter(None, redis, s).get_quote(sym)).price == Decimal("1")


async def test_cache_only_market_research_and_screen_read_only_warmed_bars(warm_run, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.services.market_research as mr
    import app.services.research as rs

    redis, stored, s = warm_run
    read: list[tuple[str, str]] = []
    missed: list[tuple[str, str]] = []

    @asynccontextmanager
    async def _scope():
        yield object()

    class _Store:
        def __init__(self, db, redis, settings) -> None:  # noqa: ANN001
            pass

        async def get_daily(self, adapter, symbol, start, end):  # noqa: ANN001
            key = (adapter.name, adapter.history_symbol(symbol.upper()).upper())
            read.append(key)
            if key not in stored:
                missed.append(key)
                raise UpstreamDisabled(f"history {key}")
            return Envelope.fresh([], source=adapter.name)

    async def _none(*a, **k):  # noqa: ANN002, ANN003
        return {}, True

    async def _breadth(self):  # noqa: ANN001
        return {}, [], True

    for mod in (mr, rs):
        monkeypatch.setattr(mod, "session_scope", _scope)
        monkeypatch.setattr(mod, "HistoryStore", _Store)
    for cls, name in ((SentimentAdapter, "fear_greed"), (SentimentAdapter, "cot"),
                      (ShillerAdapter, "cape"), (MacroAdapter, "series"),
                      (FundamentalsAdapter, "statements")):
        monkeypatch.setattr(cls, name, _none)
    monkeypatch.setattr(mr.MarketResearchEngine, "_breadth_diary_and_history", _breadth)

    await mr.MarketResearchEngine(redis, s).bundle(refresh=True)
    n_mr = len(read)
    assert n_mr >= 30 and {"SPY", "XLK", "^VIX", "BTC-USD"} <= {k[1] for k in read}
    await rs.ResearchEngine(redis, s).screen({})
    assert len(read) > n_mr and ("yfinance", "GOOGL") in read
    assert not missed, f"cache-only reads of unwarmed bars: {missed}"


def test_history_targets_cover_market_research_and_screen() -> None:
    import app.demo_warm as dw
    from app.services.market_research import MarketResearchEngine
    from app.services.research import ResearchEngine

    get_settings.cache_clear()
    s, r = get_settings(), _FakeRedis()

    def keys(targets):  # noqa: ANN001
        return {(a.name, a.history_symbol(sym.upper()).upper()) for a, sym in targets}

    warmed = keys(dw.history_targets(r, s, dw.warm_symbols(s)))
    assert keys(MarketResearchEngine(r, s).history_targets()) <= warmed
    assert keys(ResearchEngine(r, s).history_targets()) <= warmed
    assert {("yfinance", "XLK"), ("yfinance", "SPY"), ("yfinance", "^VIX"), ("yfinance", "GOOGL")} <= warmed


async def test_history_warm_waits_out_a_rate_limit(monkeypatch: pytest.MonkeyPatch) -> None:
    import app.demo_warm as dw
    from app.errors import RateLimitExceeded

    attempts = {"n": 0}
    sleeps: list[float] = []

    @asynccontextmanager
    async def _scope():
        yield object()

    class _Store:
        def __init__(self, *a) -> None:  # noqa: ANN002
            pass

        async def get_daily(self, *a):  # noqa: ANN002
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise RateLimitExceeded("yfinance: rate limit")

    async def _sleep(d):  # noqa: ANN001
        sleeps.append(d)

    monkeypatch.setattr(dw, "session_scope", _scope)
    monkeypatch.setattr(dw, "HistoryStore", _Store)
    monkeypatch.setattr(dw.asyncio, "sleep", _sleep)
    await dw._warm_history(get_settings(), _FakeRedis(), build_adapter("yfinance", _FakeRedis(), get_settings()), "SPY")
    assert attempts["n"] == 3 and sleeps == [dw.RATE_LIMIT_WAIT_SECONDS] * 2
