"""Price router — quotes, history, and symbol search.

Handlers depend only on the `PriceSourceAdapter` interface via
`get_price_adapter` (default source from config, `?source=` overrides). Quotes
are written through to the `price_cache` table (best effort) in addition to the
adapter's Redis cache.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, Path, Query, Response
from redis.asyncio import Redis
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import Candle, PriceSourceAdapter, Quote, SymbolMatch
from app.adapters.envelope import Envelope
from app.adapters.factory import get_price_adapter
from app.config import Settings, get_settings
from app.db import get_db
from app.errors import InvalidRequest
from app.models import PriceCacheRow
from app.redis import get_redis
from app.services.history import HistoryStore

logger = logging.getLogger("catallax.prices")
router = APIRouter(tags=["prices"])

# Symbols span equities/ETFs/FX/futures/indices/crypto: AAPL, VWCE.DE, ^GSPC,
# BRK-B, EURUSD=X, ES=F. Bound + allowlist so a symbol can't truncate the
# `price_cache`/`daily_bars` String(32) columns, poison a Redis key, or reach an
# upstream malformed.
_SYMBOL_PATTERN = r"^[A-Za-z0-9.^_=\-]+$"
_SYMBOL = Path(..., min_length=1, max_length=32, pattern=_SYMBOL_PATTERN)


def _scrub(value: str) -> str:
    """Strip CR/LF so a runtime string can never split a response header."""
    return value.replace("\r", "").replace("\n", "")


async def _write_through(db: AsyncSession, quote: Quote, asset_type: str) -> None:
    """Persist a quote to price_cache; never let a cache write break the response."""
    try:
        stmt = (
            insert(PriceCacheRow)
            .values(
                symbol=quote.symbol,
                source=quote.source,
                asset_type=asset_type,
                price=quote.price,
                currency=quote.currency,
                as_of=quote.as_of,
            )
            .on_conflict_do_nothing(
                constraint="uq_price_cache_symbol_source_as_of"
            )
        )
        await db.execute(stmt)
        await db.commit()
    except Exception as exc:
        logger.debug("price_cache write-through failed for %s: %s", quote.symbol, exc)
        await db.rollback()


@router.get("/prices/{symbol}")
async def get_price(
    symbol: str = _SYMBOL,
    adapter: PriceSourceAdapter = Depends(get_price_adapter),
    db: AsyncSession = Depends(get_db),
) -> Quote:
    quote = await adapter.get_quote(symbol)
    await _write_through(db, quote, adapter.asset_type)
    return quote


def _set_provenance(resp: Response, env: Envelope) -> None:
    """Surface F1 provenance as headers so the daily-history body can stay a
    plain `list[Candle]` (backward-compatible) while the UI can still show which
    source served it and whether it is a cache hit / stale."""
    resp.headers["X-Catallax-Source"] = _scrub(env.source)
    resp.headers["X-Catallax-Fetched-At"] = env.fetched_at.isoformat()
    resp.headers["X-Catallax-Cache-Hit"] = "true" if env.cache_hit else "false"
    resp.headers["X-Catallax-Stale"] = "true" if env.stale else "false"
    if env.warnings:
        resp.headers["X-Catallax-Warnings"] = _scrub("; ".join(env.warnings))


def _aware(dt: datetime) -> datetime:
    """Treat a naive datetime as UTC so mixed-tz arithmetic can't TypeError."""
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


@router.get("/prices/{symbol}/history")
async def get_history(
    response: Response,
    symbol: str = _SYMBOL,
    from_: datetime | None = Query(None, alias="from"),
    to: datetime | None = Query(None, alias="to"),
    start: datetime | None = None,  # back-compat aliases for from/to
    end: datetime | None = None,
    interval: str = Query("1d", max_length=8, pattern=r"^[A-Za-z0-9]+$"),
    adapter: PriceSourceAdapter = Depends(get_price_adapter),
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
) -> list[Candle]:
    end = _aware(to or end or datetime.now(timezone.utc))
    start = _aware(from_ or start or (end - timedelta(days=30)))
    # Bound the range so a single request can't trigger a decades-long upstream
    # fetch + bulk DB write + huge Redis blob (the per-adapter rate limit caps
    # call rate, not data volume).
    if (end - start).days > settings.history_max_range_days:
        raise InvalidRequest(
            f"Date range exceeds the {settings.history_max_range_days}-day limit"
        )

    # Daily bars go through the F0 history store (Postgres-backed, read-through,
    # idempotent backfill). Intraday intervals aren't persisted -> passthrough.
    if interval == "1d":
        env = await HistoryStore(db, redis, settings).get_daily(
            adapter, symbol, start, end
        )
        _set_provenance(response, env)
        return env.data

    candles = await adapter.get_history(symbol, start, end, interval)
    response.headers["X-Catallax-Source"] = _scrub(adapter.name)
    return candles


@router.get("/search")
async def search_symbols(
    q: str = Query(..., min_length=1),
    adapter: PriceSourceAdapter = Depends(get_price_adapter),
) -> list[SymbolMatch]:
    return await adapter.search(q)
