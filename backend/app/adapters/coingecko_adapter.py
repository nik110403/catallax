"""CoinGecko adapter — crypto only, free REST API over httpx.

Maintains a symbol -> coingecko-id map (e.g. BTC -> bitcoin) resolved via the
ranked `/search` endpoint and cached in a Redis hash so we resolve the
market-cap-leading coin for an ambiguous ticker, not a random one.
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

import httpx

from app.adapters.base import Candle, PriceSourceAdapter, Quote, SymbolMatch
from app.errors import SymbolNotFound, UpstreamError
from app.upstream import guard

_IDMAP_PREFIX = "coingecko:id:"  # per-symbol key, own TTL
_IDMAP_TTL = 86_400  # 24h


class CoinGeckoAdapter(PriceSourceAdapter):
    name = "coingecko"
    asset_type = "crypto"

    @property
    def rate_limit_per_min(self) -> int:
        return self._settings.coingecko_rate_limit_per_min

    @property
    def _base(self) -> str:
        return self._settings.coingecko_base_url.rstrip("/")

    async def _get(self, path: str, params: dict | None = None) -> object:
        guard("coingecko", self._settings)
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(f"{self._base}{path}", params=params)
                resp.raise_for_status()
                return resp.json()
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 429:
                from app.errors import RateLimitExceeded

                raise RateLimitExceeded("coingecko: upstream 429") from exc
            raise UpstreamError(f"coingecko {path} -> {exc}") from exc
        except Exception as exc:
            raise UpstreamError(f"coingecko {path} failed: {exc}") from exc

    async def _resolve_id(self, symbol: str) -> str:
        sym = symbol.lower()
        key = f"{_IDMAP_PREFIX}{sym}"
        cached = await self._redis.get(key)
        if cached:
            return cached

        data = await self._get("/search", {"query": symbol})
        coins = data.get("coins", []) if isinstance(data, dict) else []
        # /search returns coins ranked by market cap. Prefer an exact symbol
        # match and cache only that (confident). Otherwise fall back to the
        # top-ranked result but do NOT cache it, so a typo/ambiguous query can't
        # pin a wrong coin for the full TTL.
        for coin in coins:
            if (coin.get("symbol") or "").lower() == sym:
                chosen = coin.get("id")
                if chosen:
                    await self._redis.set(key, chosen, ex=_IDMAP_TTL)
                    return chosen
        if coins and coins[0].get("id"):
            return coins[0]["id"]
        raise SymbolNotFound(f"No CoinGecko coin for symbol {symbol}")

    async def _fetch_quote(self, symbol: str) -> Quote:
        coin_id = await self._resolve_id(symbol)
        data = await self._get(
            "/simple/price",
            {
                "ids": coin_id,
                "vs_currencies": "usd",
                "include_last_updated_at": "true",
            },
        )
        entry = data.get(coin_id) if isinstance(data, dict) else None
        if not entry or "usd" not in entry:
            raise SymbolNotFound(f"No price for {symbol} ({coin_id})")

        as_of = (
            datetime.fromtimestamp(entry["last_updated_at"], tz=timezone.utc)
            if entry.get("last_updated_at")
            else datetime.now(timezone.utc)
        )
        return Quote(
            symbol=symbol.upper(),
            price=Decimal(str(entry["usd"])),
            currency="USD",
            as_of=as_of,
            source=self.name,
        )

    async def _fetch_history(
        self, symbol: str, start: datetime, end: datetime, interval: str
    ) -> list[Candle]:
        coin_id = await self._resolve_id(symbol)
        # The free API serves at most the last 365 days of OHLC; a longer ask is refused.
        span_days = min(365, max(1, (end - start).days or 1))
        data = await self._get(
            f"/coins/{coin_id}/ohlc",
            {"vs_currency": "usd", "days": str(span_days)},
        )
        if not isinstance(data, list):
            raise UpstreamError(f"coingecko ohlc returned non-list for {symbol}")

        candles: list[Candle] = []
        for row in data:
            ts_ms, o, h, low, c = row[0], row[1], row[2], row[3], row[4]
            ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
            if ts < start or ts > end:
                continue
            candles.append(
                Candle(
                    ts=ts,
                    open=Decimal(str(o)),
                    high=Decimal(str(h)),
                    low=Decimal(str(low)),
                    close=Decimal(str(c)),
                    volume=None,  # /ohlc carries no volume
                )
            )
        return candles

    async def _search(self, query: str) -> list[SymbolMatch]:
        data = await self._get("/search", {"query": query})
        coins = data.get("coins", []) if isinstance(data, dict) else []
        return [
            SymbolMatch(
                symbol=(coin.get("symbol") or "").upper(),
                name=coin.get("name") or coin.get("id") or "",
                asset_type="crypto",
            )
            for coin in coins
            if coin.get("symbol")
        ]
