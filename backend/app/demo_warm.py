"""Warm job for the hosted public demo (DEMO-SPEC E3): `python -m app.demo_warm`.

The demo backend runs `UPSTREAM_MODE=cache-only`, so nothing a visitor does can
reach an upstream. This one-shot job is the ONLY outbound caller: it forces
live mode, fills the Redis caches for a small symbol list and the market-wide
boards, then exits. Scheduling (every 15 min in market hours, hourly otherwise)
lives in deploy/demo/, not here.

Default symbols = the demo persona's holdings (`services/demo.WF_ASSETS`);
override with `DEMO_WARM_SYMBOLS=AAPL,BTC,...`. Exit status is 0 even when
individual fetches fail (a throttled source must not crash-loop the timer);
failures are logged and counted.
"""
from __future__ import annotations

import asyncio
import logging
import os
import sys

from redis.asyncio import Redis

from app.adapters.breadth_adapter import BreadthAdapter
from app.adapters.factory import build_chain
from app.adapters.fundamentals_adapter import FundamentalsAdapter
from app.adapters.sentiment_adapter import SentimentAdapter
from app.adapters.shiller_adapter import ShillerAdapter
from app.config import Settings, get_settings
from app.services import markets, news
from app.services.demo import WF_ASSETS

logger = logging.getLogger("catallax.demo_warm")


def warm_symbols(settings: Settings) -> list[tuple[str, str]]:
    """[(symbol, asset_class)] — configured list, else the persona's holdings."""
    configured = [s.strip().upper() for s in settings.demo_warm_symbols.split(",") if s.strip()]
    by_symbol = {a.symbol: a.asset_class for a in WF_ASSETS}
    if not configured:
        return [(a.symbol, a.asset_class) for a in WF_ASSETS]
    return [(s, by_symbol.get(s, "equity")) for s in configured]


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
    try:
        ok, failed = await warm(redis, settings)
    finally:
        await redis.aclose()
    logger.info("demo warm done: %d ok, %d failed", ok, failed)
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    sys.exit(asyncio.run(_main()))
