"""Warm job for the hosted public demo (DEMO-SPEC E3): `python -m app.demo_warm`.

The demo backend runs `UPSTREAM_MODE=cache-only`, so nothing a visitor does can
reach an upstream. This one-shot job is the ONLY outbound caller: it forces
live mode, fills the Redis caches for a small symbol list and the market-wide
boards, then exits. Scheduling (every 15 min in market hours, hourly otherwise)
lives in deploy/demo/, not here.

Default symbols = the demo persona's holdings (`services/demo.WF_ASSETS`);
override with `DEMO_WARM_SYMBOLS=AAPL,BTC,...`. On top of those it warms the
holdings-independent read paths: the ticker-bar feed quotes, the market-research
ETF/vol universe bars, the equity-research screen universe (bars + fundamentals).
Quotes survive between runs through the stale-copy TTL (`QUOTE_STALE_TTL_SECONDS`,
2 days in the demo), bars live in Postgres, fundamentals/sentiment/bundles have
6-24h TTLs, all longer than the hourly off-hours interval. Individual failures never
stop the run; they are logged and counted. The exit status is non-zero only when
a CORE step failed (quotes, daily bars), so the timer unit shows real breakage and
not expected gaps (ETFs have no fundamentals). It also backfills two years of
daily bars into Postgres (`daily_bars`) through `HistoryStore`, because
cache-only mode cannot backfill and history/risk/signals read from there. The bar
keys come from the risk and signals engines' own resolution (crypto -> yfinance
`BTC-USD`, FX `EURUSD=X`, `^GSPC` and `BTC-USD` benchmarks), never a copy of it.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

from redis.asyncio import Redis

from app.adapters.base import PriceSourceAdapter
from app.adapters.breadth_adapter import BreadthAdapter
from app.adapters.factory import ADAPTERS, build_adapter, build_chain
from app.adapters.yfinance_adapter import YFinanceAdapter
from app.adapters.fundamentals_adapter import FundamentalsAdapter
from app.adapters.sentiment_adapter import SentimentAdapter
from app.adapters.shiller_adapter import ShillerAdapter
from app.config import Settings, get_settings
from app.db import dispose_engine, init_engine, session_scope
from app.errors import RateLimitExceeded
from app.services import markets, news
from app.services.demo import WF_ASSETS
from app.services.history import HistoryStore
from app.services.market_research import MarketResearchEngine
from app.services.research import ResearchEngine
from app.services.risk import RiskEngine
from app.services.signals import SignalEngine

logger = logging.getLogger("catallax.demo_warm")

HISTORY_DAYS = 730  # visitors' history/risk/signals windows must be covered by stored bars
# The yfinance guard is 30 calls/min; ~100 daily-bar series exceed it, so a rate-limited
# backfill waits out the window instead of failing the series.
RATE_LIMIT_RETRIES = 4
RATE_LIMIT_WAIT_SECONDS = 20.0

# Ticker-bar feed: mirrors `FEED` in frontend/src/widgets/TickerBar.tsx (the demo's
# watchlist API is empty, so the frontend falls back to this list and asks the
# websocket / REST for each token: "SYM" or "SYM:source"). A test pins the two in sync.
TICKER_FEED = (
    "^GSPC", "^IXIC", "^GDAXI", "^STOXX50E", "^FTSE", "^N225", "EURUSD=X", "GBPUSD=X",
    "GC=F", "SI=F", "CL=F", "AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "GOOGL", "META",
    "BTC:coingecko", "ETH:coingecko", "SOL:coingecko",
)


def quote_targets(redis: Redis, settings: Settings, symbols: list[tuple[str, str]]) -> list[tuple[PriceSourceAdapter, str]]:
    """(reader, symbol) per quote the demo serves: the holdings via the equity chain
    (as before), plus every ticker-bar token and the market-research benchmark through
    the same adapter the websocket / `/prices` route resolves it to (explicit
    `SYM:source` -> that adapter, else the default source's chain). Cached under the
    stored key `quote:<adapter>:<SYM>`, so duplicates collapse."""
    out: dict[tuple[str, str], tuple[PriceSourceAdapter, str]] = {}
    chain = build_chain("equity", redis, settings)
    for sym, _cls in symbols:
        out.setdefault((chain.name, sym.upper()), (chain, sym))
    default_cls = ADAPTERS.get(settings.price_source_default, YFinanceAdapter)
    for token in (*TICKER_FEED, settings.market_research_benchmark):
        sym, _, src = token.partition(":")
        reader = build_adapter(src.lower(), redis, settings) if src else build_chain(default_cls.asset_type, redis, settings)
        out.setdefault((reader.name, sym.upper()), (reader, sym))
    return list(out.values())


def warm_symbols(settings: Settings) -> list[tuple[str, str]]:
    """[(symbol, asset_class)] — configured list, else the persona's holdings."""
    configured = [s.strip().upper() for s in settings.demo_warm_symbols.split(",") if s.strip()]
    by_symbol = {a.symbol: a.asset_class for a in WF_ASSETS}
    if not configured:
        return [(a.symbol, a.asset_class) for a in WF_ASSETS]
    return [(s, by_symbol.get(s, "equity")) for s in configured]


def _dedupe(targets: list[tuple[PriceSourceAdapter, str]]) -> list[tuple[PriceSourceAdapter, str]]:
    seen: dict[tuple[str, str], tuple[PriceSourceAdapter, str]] = {}
    for adapter, sym in targets:
        seen.setdefault((adapter.name, adapter.history_symbol(sym.upper()).upper()), (adapter, sym))
    return list(seen.values())


def _key(adapter: PriceSourceAdapter, sym: str) -> tuple[str, str]:
    return adapter.name, adapter.history_symbol(sym.upper()).upper()


def core_history_targets(redis: Redis, settings: Settings, symbols: list[tuple[str, str]]) -> list[tuple[PriceSourceAdapter, str]]:
    """Every (adapter, symbol) the risk lens and the signal engine read daily bars
    through for these holdings, de-duplicated by the stored key. Derived from their
    own resolution code (not re-implemented here) so the keys the warm writes are
    the keys the cache-only backend reads: crypto as `BTC-USD`/yfinance, the EURUSD=X
    FX leg and both benchmarks included."""
    return _dedupe(RiskEngine(redis, settings).history_targets(symbols) + SignalEngine(redis, settings).history_targets(symbols))


def history_targets(redis: Redis, settings: Settings, symbols: list[tuple[str, str]]) -> list[tuple[PriceSourceAdapter, str]]:
    """`core_history_targets` plus the holdings-independent universes: the
    market-research reports (sector/factor/cross-asset ETFs, benchmark, vol indices)
    and the equity-research screen. Each engine supplies its own list, so the warmed
    keys are the keys its reads use."""
    return _dedupe(
        core_history_targets(redis, settings, symbols)
        + MarketResearchEngine(redis, settings).history_targets()
        + ResearchEngine(redis, settings).history_targets()
    )


async def _warm_history(settings: Settings, redis: Redis, adapter: PriceSourceAdapter, symbol: str) -> None:
    """Backfill `daily_bars` for one key through HistoryStore."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=HISTORY_DAYS)
    for attempt in range(RATE_LIMIT_RETRIES + 1):
        try:
            async with session_scope() as db:
                await HistoryStore(db, redis, settings).get_daily(adapter, symbol, start, end)
            return
        except RateLimitExceeded:
            if attempt >= RATE_LIMIT_RETRIES:
                raise
            await asyncio.sleep(RATE_LIMIT_WAIT_SECONDS)


async def warm(redis: Redis, settings: Settings) -> tuple[int, int, int]:
    """Fetch everything once. Returns (ok, core_failed, optional_failed).
    `settings` must be live-mode. Core = quotes and every daily-bar series the
    risk/signals lenses read (holdings, FX, benchmarks); a core failure leaves a
    tab empty. Optional = fundamentals (ETFs have none), boards, news, sentiment."""
    ok = core_failed = optional_failed = 0

    async def step(label: str, coro, core: bool = False) -> None:  # noqa: ANN001
        nonlocal ok, core_failed, optional_failed
        try:
            await coro
            ok += 1
        except Exception as exc:  # one dead source must not stop the rest
            if core:
                core_failed += 1
            else:
                optional_failed += 1
            logger.warning("warm %s failed (%s): %s", label, "CORE" if core else "optional", exc)

    chain = build_chain("equity", redis, settings)
    symbols = warm_symbols(settings)
    # Core = the exact (adapter, symbol) the holdings path reads; a ticker-bar token for
    # the same symbol on another adapter (BTC:coingecko) is optional.
    held = {(chain.name, s.upper()) for s, _c in symbols}
    for reader, sym in quote_targets(redis, settings, symbols):
        # Holdings are core (a missing quote empties a tab); ticker-bar extras are not.
        await step(f"quote {reader.name}:{sym}", reader.get_quote(sym), core=(reader.name, sym.upper()) in held)
    fundamentals = FundamentalsAdapter(redis, settings)
    for sym, cls in symbols:
        if cls == "equity":
            await step(f"fundamentals {sym}", fundamentals.statements(sym, asset_class=cls))
    # Screen universe: `ResearchEngine.screen()` reads statements(sym) with no class hint.
    done = {s.upper() for s, c in symbols if c == "equity"}
    for sym, _label in settings.research_universe_map[: settings.research_screen_max_symbols]:
        if sym.upper() not in done:
            await step(f"fundamentals {sym}", fundamentals.statements(sym))
    core_keys = {_key(a, s) for a, s in core_history_targets(redis, settings, symbols)}
    for adapter, sym in history_targets(redis, settings, symbols):
        # Risk/signals series are core; the research universes only blank their own reports.
        await step(f"history {adapter.name}:{sym}", _warm_history(settings, redis, adapter, sym), core=_key(adapter, sym) in core_keys)
    sentiment = SentimentAdapter(redis, settings)
    await step("markets", markets.get_overview(redis, settings))
    await step("quakes", markets.get_quakes(redis, settings))
    await step("news", news.get_news(redis, settings))
    await step("shiller", ShillerAdapter(redis, settings).cape())
    await step("breadth", BreadthAdapter(redis, settings).diary())
    await step("fear-greed", sentiment.fear_greed())
    await step("cot", sentiment.cot())
    # Recompute the market-research bundle from the bars just stored: a bundle cached
    # earlier from empty history would otherwise keep the null reports for its 6h TTL.
    await step("market-research bundle", MarketResearchEngine(redis, settings).bundle(refresh=True))
    # Last research step (needs the bars + fundamentals above): overwrite the day's screen
    # cache so a degraded result computed by an early visitor doesn't stick for its 6h TTL.
    await step("research screen", ResearchEngine(redis, settings).screen({}, refresh=True))
    return ok, core_failed, optional_failed


async def _main() -> int:
    # Override BEFORE the first get_settings() so every code path sees live mode.
    os.environ["UPSTREAM_MODE"] = "live"
    get_settings.cache_clear()
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    init_engine()
    try:
        ok, core_failed, optional_failed = await warm(redis, settings)
    finally:
        await redis.aclose()
        await dispose_engine()
    logger.info("demo warm done: %d ok, %d core failed, %d optional failed", ok, core_failed, optional_failed)
    return 1 if core_failed else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(asyncio.run(_main()))
