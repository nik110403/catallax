"""Warm job for the hosted public demo (DEMO-SPEC E3): `python -m app.demo_warm`.

The demo backend runs `UPSTREAM_MODE=cache-only`, so nothing a visitor does can
reach an upstream. This one-shot job is the ONLY outbound caller: it forces
live mode, fills the Redis caches for a small symbol list and the market-wide
boards, then exits. Scheduling (every 15 min in market hours, hourly otherwise)
lives in deploy/demo/, not here.

Default symbols = the demo persona's holdings (`services/demo.WF_ASSETS`);
override with `DEMO_WARM_SYMBOLS=AAPL,BTC,...`. Individual failures never
stop the run; they are logged and counted, and the exit status is non-zero when
any step failed so the timer unit shows it. It also backfills two years of
daily bars into Postgres (`daily_bars`) through `HistoryStore`, because
cache-only mode cannot backfill and history/risk/signals read from there.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, timezone

from redis.asyncio import Redis

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

logger = logging.getLogger("catallax.demo_warm")

HISTORY_DAYS = 730  # visitors' history/risk/signals windows must be covered by stored bars


def warm_symbols(settings: Settings) -> list[tuple[str, str]]:
    """[(symbol, asset_class)] — configured list, else the persona's holdings."""
    configured = [s.strip().upper() for s in settings.demo_warm_symbols.split(",") if s.strip()]
    by_symbol = {a.symbol: a.asset_class for a in WF_ASSETS}
    if not configured:
        return [(a.symbol, a.asset_class) for a in WF_ASSETS]
    return [(s, by_symbol.get(s, "equity")) for s in configured]


async def _warm_history(redis: Redis, settings: Settings, symbol: str, asset_class: str) -> None:
    """Backfill `daily_bars` through HistoryStore with the same adapter chain the
    API's default price route uses, so the stored (symbol, source) key matches."""
    chain = build_chain(asset_class, redis, settings)
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=HISTORY_DAYS)
    async with session_scope() as db:
        await HistoryStore(db, redis, settings).get_daily(chain, symbol, start, end)


async def warm(redis: Redis, settings: Settings) -> tuple[int, int]:
    """Fetch everything once. Returns (ok, failed). `settings` must be live-mode."""
    ok = failed = 0

    async def step(label: str, coro) -> None:  # noqa: ANN001
        nonlocal ok, failed
        try:
            await coro
            ok += 1
        except Exception as exc:  # one dead source must not stop the rest
            failed += 1
            logger.warning("warm %s failed: %s", label, exc)

    chain = build_chain("equity", redis, settings)
    symbols = warm_symbols(settings)
    for sym, _cls in symbols:
        await step(f"quote {sym}", chain.get_quote(sym))
    fundamentals = FundamentalsAdapter(redis, settings)
    for sym, cls in symbols:
        if cls == "equity":
            await step(f"fundamentals {sym}", fundamentals.statements(sym, asset_class=cls))
    for sym, cls in symbols:
        await step(f"history {sym}", _warm_history(redis, settings, sym, cls))
    sentiment = SentimentAdapter(redis, settings)
    await step("markets", markets.get_overview(redis, settings))
    await step("quakes", markets.get_quakes(redis, settings))
    await step("news", news.get_news(redis, settings))
    await step("shiller", ShillerAdapter(redis, settings).cape())
    await step("breadth", BreadthAdapter(redis, settings).diary())
    await step("fear-greed", sentiment.fear_greed())
    return ok, failed


async def _main() -> int:
    # Override BEFORE the first get_settings() so every code path sees live mode.
    os.environ["UPSTREAM_MODE"] = "live"
    get_settings.cache_clear()
    settings = get_settings()
    redis = Redis.from_url(settings.redis_url, decode_responses=True)
    init_engine()
    try:
        ok, failed = await warm(redis, settings)
    finally:
        await redis.aclose()
        await dispose_engine()
    logger.info("demo warm done: %d ok, %d failed", ok, failed)
    return 1 if failed else 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(asyncio.run(_main()))
