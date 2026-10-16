"""Stooq adapter — free EOD equities/indices, the equity fallback.

Stooq has no API key and serves plain CSV: a light last-quote endpoint and a
daily-history endpoint. It is wired as the *fallback* behind yfinance, so it
implements quotes + daily history; `search` stays empty (yfinance owns search).

Symbols: Stooq namespaces tickers (`aapl.us`, `^spx`). A bare `AAPL` is mapped
to `aapl.us` (US default); anything already carrying a `.`/`^` is passed through.
"""
from __future__ import annotations

import csv
import io
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

import httpx

from app.adapters.base import Candle, PriceSourceAdapter, Quote, SymbolMatch
from app.errors import SymbolNotFound, UpstreamError
from app.upstream import guard

_UA = "Mozilla/5.0 (catallax)"


def _stooq_symbol(symbol: str) -> str:
    s = symbol.strip().lower()
    if "." in s or s.startswith("^"):
        return s
    return f"{s}.us"


def _dec(value: str) -> Decimal | None:
    try:
        return Decimal(value)
    except (InvalidOperation, ValueError):
        return None


class StooqAdapter(PriceSourceAdapter):
    name = "stooq"
    asset_type = "equity"

    @property
    def rate_limit_per_min(self) -> int:
        return self._settings.stooq_rate_limit_per_min

    @property
    def _base(self) -> str:
        return self._settings.stooq_base_url.rstrip("/")

    async def _get_csv(self, path: str, params: dict) -> str:
        guard("stooq", self._settings)
        try:
            # follow_redirects=False: Stooq's CSV endpoints serve directly, so a
            # redirect would only be an upstream change or MITM — don't chase it
            # to a possibly-internal host.
            async with httpx.AsyncClient(
                timeout=15, headers={"User-Agent": _UA}, follow_redirects=False
            ) as client:
                resp = await client.get(f"{self._base}{path}", params=params)
                resp.raise_for_status()
                text = resp.text
                # Stooq now serves a JS bot-challenge (HTML) to datacenter IPs
                # instead of CSV. Detect it so we fail cleanly (the fallback
                # chain then skips to the next source) rather than mis-parsing
                # HTML as rows.
                if text.lstrip()[:1] == "<":
                    raise UpstreamError("stooq returned non-CSV (bot challenge?)")
                return text
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                from app.errors import RateLimitExceeded

                raise RateLimitExceeded("stooq: upstream 429") from exc
            raise UpstreamError(f"stooq {path} -> {exc}") from exc
        except Exception as exc:
            raise UpstreamError(f"stooq {path} failed: {exc}") from exc

    async def _fetch_quote(self, symbol: str) -> Quote:
        text = await self._get_csv(
            "/q/l/", {"s": _stooq_symbol(symbol), "f": "sd2t2ohlcv", "h": "", "e": "csv"}
        )
        rows = list(csv.DictReader(io.StringIO(text)))
        if not rows:
            raise SymbolNotFound(f"No Stooq data for symbol {symbol}")
        row = rows[0]
        close = _dec(row.get("Close", ""))
        if close is None:  # Stooq returns "N/D" for unknown symbols
            raise SymbolNotFound(f"No Stooq price for symbol {symbol}")
        return Quote(
            symbol=symbol.upper(),
            price=close,
            currency="USD",
            as_of=datetime.now(timezone.utc),
            source=self.name,
        )

    async def _fetch_history(
        self, symbol: str, start: datetime, end: datetime, interval: str
    ) -> list[Candle]:
        text = await self._get_csv(
            "/q/d/l/",
            {
                "s": _stooq_symbol(symbol),
                "i": "d",
                "d1": start.strftime("%Y%m%d"),
                "d2": end.strftime("%Y%m%d"),
            },
        )
        rows = list(csv.DictReader(io.StringIO(text)))
        candles: list[Candle] = []
        for row in rows:
            d = row.get("Date")
            o, h, low, c = (
                _dec(row.get("Open", "")),
                _dec(row.get("High", "")),
                _dec(row.get("Low", "")),
                _dec(row.get("Close", "")),
            )
            if not d or None in (o, h, low, c):
                continue
            try:
                ts = datetime.strptime(d, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            candles.append(
                Candle(
                    ts=ts,
                    open=o,  # type: ignore[arg-type]
                    high=h,  # type: ignore[arg-type]
                    low=low,  # type: ignore[arg-type]
                    close=c,  # type: ignore[arg-type]
                    volume=_dec(row.get("Volume", "")),
                )
            )
        if not candles:
            raise SymbolNotFound(f"No Stooq history for symbol {symbol}")
        return candles

    async def _search(self, query: str) -> list[SymbolMatch]:
        # Stooq has no clean search API; it is a price fallback only.
        return []
