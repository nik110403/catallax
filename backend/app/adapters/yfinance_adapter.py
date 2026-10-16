"""yfinance adapter — equities, ETFs, FX.

yfinance is a blocking library, so every call to it runs in a worker thread via
`asyncio.to_thread`. Symbol search uses Yahoo's async autocomplete endpoint
(same data source) over httpx, so it needs no thread.
"""

from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone
from decimal import Decimal

import httpx
from redis.asyncio import Redis
import yfinance as yf

from app.adapters.base import Candle, PriceSourceAdapter, Quote, SymbolMatch
from app.adapters.yahoo_symbols import to_yahoo_symbol
from app.config import Settings
from app.errors import SymbolNotFound, UpstreamError
from app.upstream import guard

logger = logging.getLogger("catallax.yfinance")

_YAHOO_SEARCH = "https://query1.finance.yahoo.com/v1/finance/search"
_UA = "Mozilla/5.0 (catallax)"


def _finite(value: object) -> bool:
    try:
        return math.isfinite(float(value))  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _to_decimal(value: object) -> Decimal:
    return Decimal(str(value))


class YFinanceAdapter(PriceSourceAdapter):
    name = "yfinance"
    asset_type = "equity"

    def __init__(
        self,
        redis: Redis,
        settings: Settings,
        *,
        crypto: bool | None = None,
        native_ticker: bool = False,
    ) -> None:
        """`crypto` is the caller's KNOWN asset-class hint (review 225d5f4
        re-review F2): bound once at construction so it is the ONLY symbol
        resolution applied — a per-call default-`None` heuristic re-applied on
        top of an already-resolved symbol is how a `SOL` EQUITY got remapped
        onto the `SOL-USD` coin (a bare "known non-crypto" ticker carries no
        marker a second heuristic pass could recognise). None (default) keeps
        the existing spelling heuristic for callers with no class info."""
        super().__init__(redis, settings)
        self._crypto_hint = crypto
        self._native_ticker = native_ticker

    @property
    def rate_limit_per_min(self) -> int:
        return self._settings.yfinance_rate_limit_per_min

    def _yf_symbol(self, symbol: str) -> str:
        """Leave an authoritative native ticker untouched; otherwise delegate to
        the shared mapper (alias, class-aware crypto rule, suffix pass-through)."""
        if self._native_ticker:
            return symbol
        return to_yahoo_symbol(symbol, self._settings, crypto=self._crypto_hint)

    def history_symbol(self, symbol: str) -> str:
        """Return the Yahoo ticker HistoryStore uses as its canonical key."""
        return self._yf_symbol(symbol)

    async def _fetch_quote(self, symbol: str) -> Quote:
        return await asyncio.to_thread(self._blocking_quote, symbol)

    def _blocking_quote(self, symbol: str) -> Quote:
        guard("yfinance", self._settings)
        ticker = yf.Ticker(self._yf_symbol(symbol))
        price: float | None = None
        currency: str | None = None
        try:
            fast = ticker.fast_info
            price = fast.get("last_price") or fast.get("lastPrice")
            currency = fast.get("currency")
        except Exception:
            pass

        if price is None:
            try:
                hist = ticker.history(period="1d")
            except Exception as exc:  # network/library failure
                raise UpstreamError(f"yfinance failed for {symbol}: {exc}") from exc
            if hist.empty:
                raise SymbolNotFound(f"No data for symbol {symbol}")
            price = float(hist["Close"].iloc[-1])

        return Quote(
            symbol=symbol.upper(),
            price=_to_decimal(price),
            currency=currency or "USD",
            as_of=datetime.now(timezone.utc),
            source=self.name,
        )

    async def _fetch_history(
        self, symbol: str, start: datetime, end: datetime, interval: str
    ) -> list[Candle]:
        return await asyncio.to_thread(
            self._blocking_history, symbol, start, end, interval
        )

    def _blocking_history(
        self, symbol: str, start: datetime, end: datetime, interval: str
    ) -> list[Candle]:
        try:
            frame = yf.Ticker(self._yf_symbol(symbol)).history(
                start=start, end=end, interval=interval
            )
        except Exception as exc:
            raise UpstreamError(f"yfinance history failed for {symbol}: {exc}") from exc
        if frame.empty:
            raise SymbolNotFound(f"No history for symbol {symbol}")

        candles: list[Candle] = []
        dropped = 0
        try:
            for ts, row in frame.iterrows():
                # Yahoo returns NaN OHLC on partial/not-yet-priced rows (e.g. today's EU session
                # row): drop those rows rather than failing the whole history.
                if not all(_finite(row[c]) for c in ("Open", "High", "Low", "Close")):
                    dropped += 1
                    continue
                candles.append(
                    Candle(
                        ts=ts.to_pydatetime(),
                        open=_to_decimal(row["Open"]),
                        high=_to_decimal(row["High"]),
                        low=_to_decimal(row["Low"]),
                        close=_to_decimal(row["Close"]),
                        volume=_to_decimal(row["Volume"]) if "Volume" in row else None,
                    )
                )
        except (KeyError, ValueError, TypeError) as exc:
            raise UpstreamError(
                f"yfinance returned an unexpected history shape for {symbol}: {exc}"
            ) from exc
        if dropped:
            logger.debug("yfinance %s: dropped %d row(s) with missing/non-finite OHLC", symbol, dropped)
        if not candles:
            raise UpstreamError(f"yfinance returned no valid OHLC rows for {symbol}")
        return candles

    async def _search(self, query: str) -> list[SymbolMatch]:
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(
                    _YAHOO_SEARCH,
                    params={"q": query, "quotesCount": 10, "newsCount": 0},
                    headers={"User-Agent": _UA},
                )
                resp.raise_for_status()
                payload = resp.json()
        except Exception as exc:
            raise UpstreamError(f"yahoo search failed: {exc}") from exc

        matches: list[SymbolMatch] = []
        for q in payload.get("quotes", []):
            sym = q.get("symbol")
            if not sym:
                continue
            matches.append(
                SymbolMatch(
                    symbol=sym,
                    name=q.get("shortname") or q.get("longname") or sym,
                    asset_type=(q.get("quoteType") or "equity").lower(),
                )
            )
        return matches
