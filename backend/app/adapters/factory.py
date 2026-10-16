"""Adapter registry + the `get_price_adapter` FastAPI dependency.

Route handlers depend only on `PriceSourceAdapter`; they never name a concrete
class. With no `?source=` override the dependency returns a `FallbackAdapter`
walking the configured chain for the default source's asset class. An
explicit `?source=` pins a single adapter (no fallback — an intentional override).
"""
from __future__ import annotations

import logging

from fastapi import Depends, Query
from redis.asyncio import Redis

from app.adapters.base import PriceSourceAdapter
from app.adapters.coingecko_adapter import CoinGeckoAdapter
from app.adapters.fallback import FallbackAdapter
from app.adapters.stooq_adapter import StooqAdapter
from app.adapters.yfinance_adapter import YFinanceAdapter
from app.config import Settings, get_settings
from app.errors import AdapterNotFound
from app.redis import get_redis

logger = logging.getLogger("catallax.adapters")

ADAPTERS: dict[str, type[PriceSourceAdapter]] = {
    "yfinance": YFinanceAdapter,
    "coingecko": CoinGeckoAdapter,
    "stooq": StooqAdapter,
}


def build_adapter(
    name: str, redis: Redis, settings: Settings
) -> PriceSourceAdapter:
    cls = ADAPTERS.get(name)
    if cls is None:
        raise AdapterNotFound(
            f"Unknown price source '{name}'. Known: {', '.join(sorted(ADAPTERS))}"
        )
    return cls(redis, settings)


def build_chain(
    asset_type: str, redis: Redis, settings: Settings
) -> PriceSourceAdapter:
    """A FallbackAdapter over the configured chain for an asset class."""
    adapters: list[PriceSourceAdapter] = []
    for sid in settings.fallback_chain(asset_type):
        cls = ADAPTERS.get(sid)
        if cls is None:
            logger.warning("fallback chain references unknown source '%s'; skipping", sid)
            continue
        adapters.append(cls(redis, settings))
    if not adapters:
        adapters = [build_adapter(settings.price_source_default, redis, settings)]
    return FallbackAdapter(adapters, redis, settings)


def get_price_adapter(
    source: str | None = Query(None, max_length=32),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> PriceSourceAdapter:
    if source:
        return build_adapter(source, redis, settings)
    default_cls = ADAPTERS.get(settings.price_source_default, YFinanceAdapter)
    return build_chain(default_cls.asset_type, redis, settings)
