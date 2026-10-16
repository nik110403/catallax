"""Warm job for the hosted public demo (DEMO-SPEC E3): `python -m app.demo_warm`.

The demo backend runs `UPSTREAM_MODE=cache-only`, so nothing a visitor does can
reach an upstream. This one-shot job is the ONLY outbound caller: it forces
live mode, fills the Redis caches for a small symbol list and the market-wide
boards, then exits. Scheduling (every 15 min in market hours, hourly otherwise)
lives in deploy/demo/, not here.

Default symbols = the demo persona's holdings (`services/demo.WF_ASSETS`);
override with `DEMO_WARM_SYMBOLS=AAPL,BTC,...`. Individual failures never
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
from app.adapters.factory import build_chain
from app.adapters.fundamentals_adapter import FundamentalsAdapter
from app.adapters.sentiment_adapter import SentimentAdapter
from app.adapters.shiller_adapter import ShillerAdapter
from app.config import Settings, get_settings
from app.db import dispose_engine, init_engine, session_scope
from app.services import markets, news
from app.services.demo import WF_ASSETS
from app.services.history import HistoryStore
from app.services.risk import RiskEngine
from app.services.signals import SignalEngine

logger = logging.getLogger("catallax.demo_warm")

HISTORY_DAYS = 730  # visitors' history/risk/signals windows must be covered by stored bars


def warm_symbols(settings: Settings) -> list[tuple[str, str]]:
    """[(symbol, asset_class)] — configured list, else the persona's holdings."""
    configured = [s.strip().upper() for s in settings.demo_warm_symbols.split(",") if s.strip()]
    by_symbol = {a.symbol: a.asset_class for a in WF_ASSETS}
    if not configured:
        return [(a.symbol, a.asset_class) for a in WF_ASSETS]
    return [(s, by_symbol.get(s, "equity")) for s in configured]


def history_targets(redis: Redis, settings: Settings, symbols: list[tuple[str, str]]) -> list[tuple[PriceSourceAdapter, str]]:
    """Every (adapter, symbol) the risk lens and the signal engine read daily bars
    through for these holdings, de-duplicated by the stored key. Derived from their
    own resolution code (not re-implemented here) so the keys the warm writes are
    the keys the cache-only backend reads: crypto as `BTC-USD`/yfinance, the EURUSD=X
    FX leg and both benchmarks included."""
    targets = RiskEngine(redis, settings).history_targets(symbols) + SignalEngine(redis, settings).history_targets(symbols)
    seen: dict[tuple[str, str], tuple[PriceSourceAdapter, str]] = {}
    for adapter, sym in targets:
        seen.setdefault((adapter.name, adapter.history_symbol(sym.upper()).upper()), (adapter, sym))
    return list(seen.values())


async def _warm_history(settings: Settings, redis: Redis, adapter: PriceSourceAdapter, symbol: str) -> None:
    """Backfill `daily_bars` for one key through HistoryStore."""
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=HISTORY_DAYS)
    async with session_scope() as db:
        await HistoryStore(db, redis, settings).get_daily(adapter, symbol, start, end)


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
    for sym, _cls in symbols:
        await step(f"quote {sym}", chain.get_quote(sym), core=True)
    fundamentals = FundamentalsAdapter(redis, settings)
    for sym, cls in symbols:
        if cls == "equity":
            await step(f"fundamentals {sym}", fundamentals.statements(sym, asset_class=cls))
    for adapter, sym in history_targets(redis, settings, symbols):
        await step(f"history {adapter.name}:{sym}", _warm_history(settings, redis, adapter, sym), core=True)
    sentiment = SentimentAdapter(redis, settings)
    await step("markets", markets.get_overview(redis, settings))
    await step("quakes", markets.get_quakes(redis, settings))
    await step("news", news.get_news(redis, settings))
    await step("shiller", ShillerAdapter(redis, settings).cape())
    await step("breadth", BreadthAdapter(redis, settings).diary())
    await step("fear-greed", sentiment.fear_greed())
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
