"""Foundations F0 — the daily price-history read path (quant data backbone).

`HistoryStore` serves a normalised daily-bar series for an adapter-resolved
`(symbol, source)` from Postgres (`daily_bars`), backfilling idempotently from
the adapter's
`get_history` only when the stored tail is missing or stale. Risk, backtest, ML
and signal code reads from here instead of re-hitting upstream every time.

Read path:
  1. Redis result cache (`history:{source}:{canonical_symbol}:{from}:{to}`, short TTL) —
     a hit returns immediately without touching Postgres or upstream.
  2. On miss, read `daily_bars` for the range. If coverage is incomplete (no
     rows, range start uncovered, or the newest bar is older than the freshness
     window), backfill the range from the adapter and upsert.
  3. Re-read, wrap in an `Envelope` with provenance, cache, return.

Upstream failure during backfill degrades gracefully: if anything is already
stored we serve it flagged `stale=True` rather than erroring.
"""
from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.base import Candle, PriceSourceAdapter
from app.adapters.envelope import Envelope
from app.config import Settings
from app.errors import CatallaxError, SymbolNotFound
from app.models import DailyBarRow

logger = logging.getLogger("catallax.history")


def _as_date(value: datetime | date) -> date:
    return value.date() if isinstance(value, datetime) else value


def _aggregate_daily(candles: list[Candle]) -> dict[date, dict[str, Decimal | None]]:
    """Collapse (possibly intraday) candles into one OHLC bar per calendar day.

    yfinance daily bars are already one-per-day; CoinGecko `/ohlc` returns
    several points per day — aggregating makes the store source-agnostic.
    """
    bars: dict[date, dict[str, Decimal | None]] = {}
    for c in candles:
        d = _as_date(c.ts)
        cur = bars.get(d)
        if cur is None:
            bars[d] = {
                "open": c.open,
                "high": c.high,
                "low": c.low,
                "close": c.close,
                "volume": c.volume,
            }
        else:
            cur["high"] = max(cur["high"], c.high)  # type: ignore[type-var]
            cur["low"] = min(cur["low"], c.low)  # type: ignore[type-var]
            cur["close"] = c.close  # last wins (candles arrive in order)
            if c.volume is not None:
                cur["volume"] = (cur["volume"] or Decimal(0)) + c.volume
    return bars


class HistoryStore:
    def __init__(self, db: AsyncSession, redis: Redis, settings: Settings) -> None:
        self._db = db
        self._redis = redis
        self._settings = settings

    # ── public read path ─────────────────────────────────────
    async def get_daily(
        self,
        adapter: PriceSourceAdapter,
        symbol: str,
        start: datetime,
        end: datetime,
    ) -> Envelope[list[Candle]]:
        request_symbol = symbol.upper()
        symbol = adapter.history_symbol(request_symbol).upper()
        source = adapter.name
        start_d, end_d = _as_date(start), _as_date(end)

        cache_key = f"history:{source}:{symbol}:{start_d.isoformat()}:{end_d.isoformat()}"
        cached = await self._redis.get(cache_key)
        if cached is not None:
            try:
                env = Envelope[list[Candle]].model_validate_json(cached)
                env.cache_hit = True
                return env
            except Exception as exc:
                # Corrupt/incompatible cached blob: log so a silently-poisoned
                # key can't cause permanent cache bypass unnoticed, then rebuild.
                logger.debug("history cache deserialise failed for %s: %s", cache_key, exc)

        rows = await self._read_rows(symbol, source, start_d, end_d)
        warnings: list[str] = []
        stale = False

        if self._needs_backfill(rows, start_d, end_d):
            try:
                await self._backfill(adapter, request_symbol, symbol, source, start, end)
                rows = await self._read_rows(symbol, source, start_d, end_d)
            except SymbolNotFound:
                if not rows:
                    raise
                warnings.append("upstream had no new data; serving stored bars")
                stale = True
            except CatallaxError as exc:
                if not rows:
                    raise
                logger.warning("history backfill failed for %s/%s: %s", source, symbol, exc)
                warnings.append(f"upstream unavailable ({exc.code}); serving stored bars")
                stale = True

        candles = [
            Candle(
                ts=datetime.combine(r.bar_date, datetime.min.time(), tzinfo=timezone.utc),
                open=r.open,
                high=r.high,
                low=r.low,
                close=r.close,
                volume=r.volume,
            )
            for r in rows
        ]
        env = Envelope.fresh(
            candles, source=source, cache_hit=False, stale=stale, warnings=warnings
        )
        await self._redis.set(
            cache_key,
            env.model_dump_json(),
            ex=self._settings.history_cache_ttl_seconds,
        )
        return env

    # ── internals ────────────────────────────────────────────
    async def _read_rows(
        self, symbol: str, source: str, start_d: date, end_d: date
    ) -> list[DailyBarRow]:
        stmt = (
            select(DailyBarRow)
            .where(
                DailyBarRow.symbol == symbol,
                DailyBarRow.source == source,
                DailyBarRow.bar_date >= start_d,
                DailyBarRow.bar_date <= end_d,
            )
            .order_by(DailyBarRow.bar_date)
        )
        return list((await self._db.execute(stmt)).scalars().all())

    def _needs_backfill(
        self, rows: list[DailyBarRow], start_d: date, end_d: date
    ) -> bool:
        if not rows:
            return True
        # Range start uncovered -> the caller asked further back than we hold.
        if rows[0].bar_date > start_d:
            return True
        # Tail stale beyond the freshness window (covers weekend/holiday gaps so
        # we don't call upstream for a "missing today" that will never exist).
        window = timedelta(days=self._settings.history_staleness_days)
        return rows[-1].bar_date < end_d - window

    async def _backfill(
        self,
        adapter: PriceSourceAdapter,
        request_symbol: str,
        symbol: str,
        source: str,
        start: datetime,
        end: datetime,
    ) -> None:
        candles = await adapter.get_history(request_symbol, start, end, "1d")
        bars = _aggregate_daily(candles)
        if not bars:
            return
        values = [
            {
                "symbol": symbol,
                "source": source,
                "asset_type": adapter.asset_type,
                "bar_date": d,
                "open": b["open"],
                "high": b["high"],
                "low": b["low"],
                "close": b["close"],
                "volume": b["volume"],
            }
            for d, b in bars.items()
        ]
        stmt = insert(DailyBarRow).values(values)
        # Re-fetched bars may finalise a provisional close -> update OHLCV.
        stmt = stmt.on_conflict_do_update(
            constraint="uq_daily_bars_symbol_source_date",
            set_={
                "open": stmt.excluded.open,
                "high": stmt.excluded.high,
                "low": stmt.excluded.low,
                "close": stmt.excluded.close,
                "volume": stmt.excluded.volume,
                "fetched_at": datetime.now(timezone.utc),
            },
        )
        await self._db.execute(stmt)
        await self._db.commit()
