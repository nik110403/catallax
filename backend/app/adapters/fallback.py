"""Foundations F2 — adapter resilience: ordered fallback chain + 429 backoff.

`FallbackAdapter` wraps an ordered list of `PriceSourceAdapter`s and presents
the same public contract. Each call tries the chain in order; a source that
raises `SymbolNotFound`/`UpstreamError` is skipped to the next, and a
`RateLimitExceeded` (local or upstream 429) is retried with exponential backoff
+ jitter before moving on. The call fails only after every source fails.

Children keep their own rate-limit + cache (applied in their base `get_*`); this
wrapper only sequences them, so it overrides the public methods and leaves the
`_fetch_*` hooks unused.
"""
from __future__ import annotations

import asyncio
import logging
import random

from redis.asyncio import Redis

from app.adapters.base import Candle, PriceSourceAdapter, Quote, SymbolMatch
from app.config import Settings
from app.errors import CatallaxError, RateLimitExceeded, UpstreamError

logger = logging.getLogger("catallax.fallback")


class FallbackAdapter(PriceSourceAdapter):
    def __init__(
        self, chain: list[PriceSourceAdapter], redis: Redis, settings: Settings
    ) -> None:
        super().__init__(redis, settings)
        if not chain:
            raise ValueError("FallbackAdapter requires at least one adapter")
        self._chain = chain
        # Present the primary's identity (the source label on a served quote is
        # set by whichever adapter actually serves it, so it stays accurate).
        self.name = chain[0].name
        self.asset_type = chain[0].asset_type

    @property
    def rate_limit_per_min(self) -> int:  # delegated; children enforce their own
        return self._chain[0].rate_limit_per_min

    # ── public contract: walk the chain ──────────────────────
    async def get_quote(self, symbol: str) -> Quote:
        return await self._walk("get_quote", symbol)

    async def get_history(
        self, symbol, start, end, interval  # noqa: ANN001 — matches base signature
    ) -> list[Candle]:
        return await self._walk("get_history", symbol, start, end, interval)

    async def search(self, query: str) -> list[SymbolMatch]:
        return await self._walk("search", query)

    async def _walk(self, method: str, *args: object):
        last_exc: CatallaxError = UpstreamError("no source served the request")
        for adapter in self._chain:
            try:
                return await self._call_with_backoff(adapter, method, *args)
            except CatallaxError as exc:
                last_exc = exc
                logger.info(
                    "fallback: %s.%s failed (%s); trying next source",
                    adapter.name,
                    method,
                    exc.code,
                )
        raise last_exc

    async def _call_with_backoff(
        self, adapter: PriceSourceAdapter, method: str, *args: object
    ):
        fn = getattr(adapter, method)
        attempts = self._settings.price_backoff_max_retries
        base = self._settings.price_backoff_base_seconds
        for attempt in range(attempts + 1):
            try:
                return await fn(*args)
            except RateLimitExceeded:
                if attempt >= attempts:
                    raise
                delay = base * (2**attempt) + random.uniform(0, base)
                logger.info(
                    "fallback: %s.%s rate-limited; backoff %.2fs (attempt %d/%d)",
                    adapter.name,
                    method,
                    delay,
                    attempt + 1,
                    attempts,
                )
                await asyncio.sleep(delay)

    # ── ABC hooks: unused (public methods are overridden above) ─
    async def _fetch_quote(self, symbol: str) -> Quote:  # pragma: no cover
        raise NotImplementedError

    async def _fetch_history(self, symbol, start, end, interval):  # pragma: no cover
        raise NotImplementedError

    async def _search(self, query: str) -> list[SymbolMatch]:  # pragma: no cover
        raise NotImplementedError
