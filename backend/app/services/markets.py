"""Markets overview service.

Aggregates a curated cross-asset board — global indices, FX, commodities, bond
yields (yfinance, one batched download) and crypto (CoinGecko markets endpoint,
one call) — into a single Redis-cached blob plus a derived risk "composite"
signal. One upstream burst per `markets_cache_ttl_seconds`; the frontend polls
the cached result.

A separate `get_quakes` proxies the USGS earthquake feed (free, no key) so the
geo-map widget never calls an external API directly (widget-system rule).
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone

import httpx
import yfinance as yf
from pydantic import BaseModel
from redis.asyncio import Redis

from app.config import Settings
from app.upstream import STALE_SUFFIX, STALE_TTL, guard, is_cache_only

logger = logging.getLogger("catallax.markets")

_OVERVIEW_KEY = "markets:overview"
_CRYPTO_KEY = "markets:crypto"
_QUAKES_KEY = "markets:quakes"
# Crypto is kept warm far longer than the overview so a CoinGecko 429 (the free
# tier is easily exhausted by the WS ticker) falls back to the last good board.
_CRYPTO_STALE_TTL = 1800
_UA = "Mozilla/5.0 (catallax markets)"

# Curated instrument board. (yahoo symbol, label) per group. Crypto is sourced
# from CoinGecko instead (24h change comes free), keyed by coingecko id.
_YF_GROUPS: dict[str, tuple[str, list[tuple[str, str]]]] = {
    "indices": (
        "Indices",
        [
            ("^GSPC", "S&P 500"),
            ("^IXIC", "Nasdaq"),
            ("^DJI", "Dow Jones"),
            ("^FTSE", "FTSE 100"),
            ("^GDAXI", "DAX"),
            ("^STOXX50E", "Euro Stoxx 50"),
            ("^N225", "Nikkei 225"),
            ("^HSI", "Hang Seng"),
        ],
    ),
    "fx": (
        "FX",
        [
            ("EURUSD=X", "EUR/USD"),
            ("GBPUSD=X", "GBP/USD"),
            ("USDJPY=X", "USD/JPY"),
            ("USDCHF=X", "USD/CHF"),
            ("AUDUSD=X", "AUD/USD"),
            ("EURGBP=X", "EUR/GBP"),
        ],
    ),
    "commodities": (
        "Commodities",
        [
            ("GC=F", "Gold"),
            ("SI=F", "Silver"),
            ("CL=F", "WTI Crude"),
            ("BZ=F", "Brent Crude"),
            ("NG=F", "Nat Gas"),
            ("HG=F", "Copper"),
        ],
    ),
    "yields": (
        "Bond Yields",
        [
            ("^IRX", "US 13wk"),
            ("^FVX", "US 5yr"),
            ("^TNX", "US 10yr"),
            ("^TYX", "US 30yr"),
        ],
    ),
}

# CoinGecko id -> display ticker.
_CRYPTO: list[tuple[str, str]] = [
    ("bitcoin", "BTC"),
    ("ethereum", "ETH"),
    ("solana", "SOL"),
    ("binancecoin", "BNB"),
    ("ripple", "XRP"),
    ("cardano", "ADA"),
]


class MarketQuote(BaseModel):
    symbol: str
    label: str
    price: str
    change: str  # absolute change vs previous close (display units)
    change_pct: float
    currency: str


class MarketGroup(BaseModel):
    key: str
    label: str
    items: list[MarketQuote]


class MarketComposite(BaseModel):
    score: float  # avg % change of the indices group
    label: str  # Risk-On / Neutral / Risk-Off
    advancers: int
    decliners: int
    total: int


class MarketsOverview(BaseModel):
    as_of: datetime
    groups: list[MarketGroup]
    composite: MarketComposite


def _fmt_price(v: float) -> str:
    # Compact: 2 dp for most, 4 dp for sub-10 (FX), to keep the board readable.
    return f"{v:,.4f}" if abs(v) < 10 else f"{v:,.2f}"


def _blocking_yf() -> list[MarketGroup]:
    """One batched yfinance download → last + previous close per symbol."""
    flat = [(g, sym, label) for g, (_, items) in _YF_GROUPS.items() for sym, label in items]
    symbols = [s for _, s, _ in flat]
    try:
        guard("markets-yfinance")
        data = yf.download(
            tickers=" ".join(symbols),
            # 1mo, not 5d: Yahoo's daily history for the CBOE yield indices
            # (^IRX/^FVX/^TNX/^TYX) often carries a single row inside a 5d
            # window, which collapsed change to 0.00 (prev == last).
            period="1mo",
            interval="1d",
            group_by="ticker",
            auto_adjust=False,
            progress=False,
            threads=True,
        )
    except Exception as exc:  # whole batch failed — return empty groups
        logger.warning("yfinance markets download failed: %s", exc)
        data = None

    def closes_for(sym: str) -> list[float]:
        if data is None:
            return []
        try:
            col = data[sym]["Close"] if sym in data.columns.get_level_values(0) else None
        except Exception:
            col = None
        if col is None:
            return []
        vals = [float(x) for x in col.dropna().tolist()]
        return vals

    out: dict[str, MarketGroup] = {
        g: MarketGroup(key=g, label=label, items=[]) for g, (label, _) in _YF_GROUPS.items()
    }
    for g, sym, label in flat:
        vals = closes_for(sym)
        if not vals:
            continue
        last = vals[-1]
        prev = vals[-2] if len(vals) >= 2 else last
        change = last - prev
        pct = (change / prev * 100) if prev else 0.0
        # FX symbols (EURUSD=X) report in the quote currency; indices/yields/commodities in USD.
        cur = "USD"
        out[g].items.append(
            MarketQuote(
                symbol=sym,
                label=label,
                price=_fmt_price(last),
                change=_fmt_price(change),
                change_pct=round(pct, 2),
                currency=cur,
            )
        )
    return [grp for grp in out.values() if grp.items]


async def _fetch_crypto(redis: Redis, settings: Settings) -> MarketGroup:
    ids = ",".join(cid for cid, _ in _CRYPTO)
    label_by_id = {cid: tic for cid, tic in _CRYPTO}
    items: list[MarketQuote] = []
    try:
        guard("markets-coingecko", settings)
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": _UA}) as client:
            resp = await client.get(
                f"{settings.coingecko_base_url}/coins/markets",
                params={
                    "vs_currency": "usd",
                    "ids": ids,
                    "price_change_percentage": "24h",
                },
            )
            resp.raise_for_status()
            rows = resp.json()
    except Exception as exc:
        logger.warning("coingecko markets fetch failed: %s", exc)
        rows = []

    # Preserve our declared order.
    by_id = {r.get("id"): r for r in rows if isinstance(r, dict)}
    for cid, tic in _CRYPTO:
        r = by_id.get(cid)
        if not r:
            continue
        price = float(r.get("current_price") or 0)
        pct = float(r.get("price_change_percentage_24h") or 0)
        change = price * pct / 100
        items.append(
            MarketQuote(
                symbol=tic,
                label=label_by_id[cid],
                price=_fmt_price(price),
                change=_fmt_price(change),
                change_pct=round(pct, 2),
                currency="USD",
            )
        )

    group = MarketGroup(key="crypto", label="Crypto", items=items)
    if items:
        await redis.set(_CRYPTO_KEY, group.model_dump_json(), ex=_CRYPTO_STALE_TTL)
        return group
    # Rate-limited / empty → serve the last good crypto board if we have one.
    stale = await redis.get(_CRYPTO_KEY)
    if stale is not None:
        try:
            return MarketGroup.model_validate_json(stale)
        except Exception:
            pass
    return group


def _composite(groups: list[MarketGroup]) -> MarketComposite:
    idx = next((g for g in groups if g.key == "indices"), None)
    items = idx.items if idx else []
    if not items:
        return MarketComposite(score=0.0, label="No data", advancers=0, decliners=0, total=0)
    score = sum(i.change_pct for i in items) / len(items)
    adv = sum(1 for i in items if i.change_pct > 0)
    dec = sum(1 for i in items if i.change_pct < 0)
    label = "Risk-On" if score > 0.4 else "Risk-Off" if score < -0.4 else "Neutral"
    return MarketComposite(
        score=round(score, 2), label=label, advancers=adv, decliners=dec, total=len(items)
    )


async def get_overview(redis: Redis, settings: Settings) -> MarketsOverview:
    cached = await redis.get(_OVERVIEW_KEY)
    if cached is not None:
        try:
            return MarketsOverview.model_validate_json(cached)
        except Exception:
            pass

    if is_cache_only(settings):
        # Demo: serve the last good overview, else an empty one (never cached).
        stale = await redis.get(_OVERVIEW_KEY + STALE_SUFFIX)
        if stale is not None:
            try:
                return MarketsOverview.model_validate_json(stale)
            except Exception:
                pass
        return MarketsOverview(
            as_of=datetime.now(timezone.utc), groups=[], composite=_composite([])
        )

    yf_groups, crypto = await asyncio.gather(
        asyncio.to_thread(_blocking_yf),
        _fetch_crypto(redis, settings),
    )
    groups = list(yf_groups)
    if crypto.items:
        groups.append(crypto)
    overview = MarketsOverview(
        as_of=datetime.now(timezone.utc),
        groups=groups,
        composite=_composite(groups),
    )
    await redis.set(
        _OVERVIEW_KEY, overview.model_dump_json(), ex=settings.markets_cache_ttl_seconds
    )
    if groups:  # long-lived copy for cache-only mode; never overwrite it with nothing
        await redis.set(_OVERVIEW_KEY + STALE_SUFFIX, overview.model_dump_json(), ex=STALE_TTL)
    return overview


async def get_quakes(redis: Redis, settings: Settings) -> dict:
    """Proxy the USGS 2.5+/day earthquake GeoJSON (free, no key), Redis-cached."""
    cached = await redis.get(_QUAKES_KEY)
    if cached is not None:
        try:
            return json.loads(cached)
        except Exception:
            pass
    if is_cache_only(settings):
        stale = await redis.get(_QUAKES_KEY + STALE_SUFFIX)
        if stale is not None:
            try:
                return json.loads(stale)
            except Exception:
                pass
        return {"type": "FeatureCollection", "features": []}
    try:
        async with httpx.AsyncClient(timeout=10, headers={"User-Agent": _UA}) as client:
            resp = await client.get(settings.usgs_quakes_url)
            resp.raise_for_status()
            data = resp.json()
    except Exception as exc:
        logger.warning("USGS quakes fetch failed: %s", exc)
        data = {"type": "FeatureCollection", "features": []}
    # Trim to the fields the map needs; keep payload small.
    feats = []
    for f in data.get("features", []):
        geom = f.get("geometry") or {}
        props = f.get("properties") or {}
        if geom.get("type") != "Point":
            continue
        feats.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": geom.get("coordinates", [])[:2]},
                "properties": {"mag": props.get("mag"), "place": props.get("place")},
            }
        )
    trimmed = {"type": "FeatureCollection", "features": feats}
    await redis.set(_QUAKES_KEY, json.dumps(trimmed), ex=settings.markets_cache_ttl_seconds)
    if feats:
        await redis.set(_QUAKES_KEY + STALE_SUFFIX, json.dumps(trimmed), ex=STALE_TTL)
    return trimmed
