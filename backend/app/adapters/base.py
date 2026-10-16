"""Adapter interface + shared cross-cutting concerns.

The public `get_quote`/`get_history`/`search` methods (the contract consumers
use, see `skills/data-adapters.md`) live here and apply rate limiting and
Redis quote caching *in the base class* — concrete adapters only implement the
`_fetch_*` methods. Handlers never think about limits or caching.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel
from redis.asyncio import Redis

from app.config import Settings
from app.errors import RateLimitExceeded
from app.upstream import guard, is_cache_only


class Quote(BaseModel):
    symbol: str
    price: Decimal
    currency: str
    as_of: datetime
    source: str  # adapter name, e.g. "yfinance"


class Candle(BaseModel):
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None = None


class SymbolMatch(BaseModel):
    symbol: str
    name: str
    asset_type: str  # "equity" | "etf" | "crypto" | ...


class PriceSourceAdapter(ABC):
    """Base for all price sources.

    Subclasses set `name` / `asset_type`, declare `rate_limit_per_min`, and
    implement `_fetch_quote` / `_fetch_history` / `_search`.
    """

    name: str = "base"
    asset_type: str = "equity"

    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings

    # ── declared by subclasses ───────────────────────────────
    @property
    @abstractmethod
    def rate_limit_per_min(self) -> int: ...

    @abstractmethod
    async def _fetch_quote(self, symbol: str) -> Quote: ...

    @abstractmethod
    async def _fetch_history(
        self, symbol: str, start: datetime, end: datetime, interval: str
    ) -> list[Candle]: ...

    @abstractmethod
    async def _search(self, query: str) -> list[SymbolMatch]: ...

    # ── public contract (consumers call these) ───────────────
    async def get_quote(self, symbol: str) -> Quote:
        cached = await self._cached_quote(symbol)
        if cached is not None:
            return cached
        if is_cache_only(self._settings):
            # Demo: never fetch on a miss. Serve the last good quote, else fail
            # so the fallback chain / caller treats the symbol as unavailable.
            stale = await self._stale_quote(symbol)
            if stale is not None:
                return stale
            guard(f"{self.name}.get_quote({symbol})", self._settings)
        try:
            await self._enforce_rate_limit()
            quote = await self._fetch_quote(symbol)
        except RateLimitExceeded:
            # Transient throttling (our local window or an upstream 429): serve the
            # last good quote within the stale window rather than failing the symbol
            # outright. Hard failures (UpstreamError/SymbolNotFound) still propagate
            # so a fallback chain can advance to the next source.
            stale = await self._stale_quote(symbol)
            if stale is not None:
                return stale
            raise
        await self._store_quote(quote)
        return quote

    async def get_history(
        self, symbol: str, start: datetime, end: datetime, interval: str
    ) -> list[Candle]:
        guard(f"{self.name}.get_history({symbol})", self._settings)
        await self._enforce_rate_limit()
        return await self._fetch_history(symbol, start, end, interval)

    def history_symbol(self, symbol: str) -> str:
        """Return the canonical storage key for a history request."""
        return symbol

    async def search(self, query: str) -> list[SymbolMatch]:
        if is_cache_only(self._settings):
            return []
        await self._enforce_rate_limit()
        return await self._search(query)

    # ── cross-cutting: rate limiting (Redis fixed-window) ────
    async def _enforce_rate_limit(self) -> None:
        # Fixed 60s window per adapter. INCR then (re)assert the TTL whenever it
        # is missing — first hit (count==1) or a prior EXPIRE that never landed
        # (ttl < 0). Without the re-assert, a lost EXPIRE would leave a
        # TTL-less counter that wedges the adapter permanently.
        key = f"ratelimit:{self.name}"
        count = await self._redis.incr(key)
        if count == 1 or await self._redis.ttl(key) < 0:
            await self._redis.expire(key, 60)
        if count > self.rate_limit_per_min:
            raise RateLimitExceeded(
                f"{self.name}: rate limit of {self.rate_limit_per_min}/min exceeded"
            )

    # ── cross-cutting: Redis quote cache (TTL by asset class) ─
    def _quote_key(self, symbol: str) -> str:
        return f"quote:{self.name}:{symbol.upper()}"

    def _quote_ttl(self) -> int:
        if self.asset_type == "crypto":
            return self._settings.quote_cache_ttl_crypto
        return self._settings.quote_cache_ttl_equity

    def _stale_key(self, symbol: str) -> str:
        return f"quote:stale:{self.name}:{symbol.upper()}"

    async def _cached_quote(self, symbol: str) -> Quote | None:
        raw = await self._redis.get(self._quote_key(symbol))
        if raw is None:
            return None
        try:
            return Quote.model_validate_json(raw)
        except Exception:
            return None

    async def _stale_quote(self, symbol: str) -> Quote | None:
        raw = await self._redis.get(self._stale_key(symbol))
        if raw is None:
            return None
        try:
            return Quote.model_validate_json(raw)
        except Exception:
            return None

    async def _store_quote(self, quote: Quote) -> None:
        payload = quote.model_dump_json()
        # Fresh key (short TTL, drives normal cache hits) + a longer stale key that
        # outlives rate-limit windows for the 429 fallback above.
        await self._redis.set(self._quote_key(quote.symbol), payload, ex=self._quote_ttl())
        await self._redis.set(
            self._stale_key(quote.symbol),
            payload,
            ex=self._settings.quote_stale_ttl_seconds,
        )
