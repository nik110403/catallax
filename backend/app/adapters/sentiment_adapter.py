"""SentimentAdapter (Phase 10a v1.1) — market sentiment + positioning.

Two free, keyless public sources behind one adapter (the only thing in the app
that talks to them; route handlers reach the data through
`/api/v1/market/sentiment` only — same boundary rule as MacroAdapter):

  * CNN **Fear & Greed** index — composite 0-100 gauge + its seven sub-indicators
    (momentum, strength, breadth, put/call, junk-bond demand, volatility, safe-haven
    demand) + history. The put/call sub-indicator means a separate (now gated)
    CBOE/AAII scrape is unnecessary.
  * CFTC **Commitments of Traders** (Socrata public-reporting API) — non-commercial
    ("large speculator") net positioning for a config basket of index/rates/metal/FX/
    vol futures, with 1y z-score + percentile + 1-week change.

Both are Redis-cached with a long-lived stale fallback (MacroAdapter pattern): a
transient upstream blank serves the last good payload rather than poisoning the TTL.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

import httpx
from fastapi import Depends
from redis.asyncio import Redis

from app.config import Settings, get_settings
from app.redis import get_redis
from app.upstream import guard

logger = logging.getLogger("catallax.sentiment")

# CNN / multpl reject the default httpx UA; present a browser one.
_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
_STALE_TTL = 7 * 86400
_STALE_WARN = "live source returned no data; showing the last good result"

# CNN sub-indicator key → display label (canonical seven).
_FG_COMPONENTS: list[tuple[str, str]] = [
    ("market_momentum_sp125", "Momentum"),
    ("stock_price_strength", "Strength"),
    ("stock_price_breadth", "Breadth"),
    ("put_call_options", "Put/Call"),
    ("market_volatility_vix", "Volatility"),
    ("junk_bond_demand", "Junk Bond Demand"),
    ("safe_haven_demand", "Safe Haven Demand"),
]


def _f(x: Any) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v and v not in (float("inf"), float("-inf")) else None  # NaN/inf → None


class SentimentAdapter:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self.redis = redis
        self.settings = settings
        self.ttl = settings.sentiment_cache_ttl_seconds

    def _client(self) -> httpx.AsyncClient:
        guard("sentiment", self.settings)
        return httpx.AsyncClient(
            timeout=self.settings.openbb_http_timeout,
            headers={"User-Agent": _UA},
            follow_redirects=True,
        )

    async def _cached(self, key: str, builder: Any) -> tuple[dict[str, Any], bool]:
        """Return (payload, stale). Empty/failed builds fall back to the stale key."""
        raw = await self.redis.get(key)
        if raw is not None:
            try:
                return json.loads(raw), False
            except Exception:
                pass
        try:
            data = await builder()
        except Exception as exc:
            logger.warning("sentiment build failed (%s): %s", key, exc)
            data = None
        if data:
            try:
                payload = json.dumps(data)
                await self.redis.set(key, payload, ex=self.ttl)
                await self.redis.set(f"{key}:stale", payload, ex=_STALE_TTL)
            except Exception as exc:
                logger.warning("sentiment cache set failed (%s): %s", key, exc)
            return data, False
        stale = await self.redis.get(f"{key}:stale")
        if stale is not None:
            try:
                return json.loads(stale), True
            except Exception:
                pass
        return {}, True

    # ── CNN Fear & Greed ───────────────────────────────────────
    async def fear_greed(self) -> tuple[dict[str, Any], bool]:
        async def build() -> dict[str, Any]:
            async with self._client() as client:
                resp = await client.get(self.settings.cnn_feargreed_url)
                resp.raise_for_status()
                d = resp.json()
            fg = d.get("fear_and_greed", {})
            components: list[dict[str, Any]] = []
            for key, label in _FG_COMPONENTS:
                sub = d.get(key) or {}
                components.append({
                    "key": key, "label": label,
                    "score": _f(sub.get("score")),
                    "rating": sub.get("rating"),
                })
            hist_data = (d.get("fear_and_greed_historical") or {}).get("data") or []
            history: list[dict[str, Any]] = []
            for pt in hist_data[-180:]:
                ms, score = pt.get("x"), _f(pt.get("y"))
                if ms is None or score is None:
                    continue
                day = datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).date().isoformat()
                history.append({"date": day, "score": round(score, 2)})
            return {
                "score": _f(fg.get("score")),
                "rating": fg.get("rating"),
                "previous_close": _f(fg.get("previous_close")),
                "previous_1_week": _f(fg.get("previous_1_week")),
                "previous_1_month": _f(fg.get("previous_1_month")),
                "previous_1_year": _f(fg.get("previous_1_year")),
                "components": components,
                "history": history,
            }

        return await self._cached("sentiment:feargreed", build)

    # ── CFTC Commitments of Traders ────────────────────────────
    async def cot(self) -> tuple[dict[str, Any], bool]:
        markets = self.settings.market_research_cot_market_map  # [(code, label)]
        inverse_set = self.settings.market_research_cot_inverse_set
        codes = [c for c, _ in markets]

        async def build() -> dict[str, Any]:
            if not codes:
                return {}
            in_list = ",".join(f"'{c}'" for c in codes)
            params = {
                "$select": (
                    "cftc_contract_market_code,report_date_as_yyyy_mm_dd,"
                    "noncomm_positions_long_all,noncomm_positions_short_all,open_interest_all"
                ),
                "$where": f"cftc_contract_market_code in({in_list})",
                "$order": "report_date_as_yyyy_mm_dd DESC",
                "$limit": "8000",
            }
            async with self._client() as client:
                resp = await client.get(self.settings.cftc_cot_url, params=params)
                resp.raise_for_status()
                rows = resp.json()
            # group rows per code, newest first (already ordered DESC)
            per_code: dict[str, list[dict[str, Any]]] = {}
            for r in rows:
                per_code.setdefault(r["cftc_contract_market_code"], []).append(r)

            import numpy as np

            out_rows: list[dict[str, Any]] = []
            for code, label in markets:
                series = per_code.get(code) or []
                if not series:
                    continue
                nets: list[float] = []
                for r in series:  # DESC order
                    try:
                        nets.append(
                            float(r["noncomm_positions_long_all"])
                            - float(r["noncomm_positions_short_all"])
                        )
                    except (TypeError, ValueError, KeyError):
                        nets.append(float("nan"))
                latest = series[0]
                net = nets[0]
                try:
                    oi = float(latest["open_interest_all"])
                except (TypeError, ValueError, KeyError):
                    oi = float("nan")
                arr = np.array([n for n in nets if n == n], dtype=float)  # drop NaN
                arr_1y = arr[:52]  # ~1y of weekly reports
                z = None
                if arr_1y.size >= 8:
                    mu, sd = float(arr_1y.mean()), float(arr_1y.std(ddof=1))
                    if sd > 0:
                        z = (net - mu) / sd
                pct = None
                if arr.size >= 8 and net == net:
                    pct = float((arr < net).mean() * 100.0)
                change_1w = (nets[0] - nets[1]) if len(nets) > 1 and nets[1] == nets[1] else None
                out_rows.append({
                    "code": code, "label": label,
                    "net": _f(net), "oi": _f(oi),
                    "pct_oi": _f(net / oi * 100.0) if oi and oi == oi else None,
                    "z_1y": _f(z), "percentile": _f(pct),
                    "change_1w": _f(change_1w),
                    "inverse": code in inverse_set,
                    "report_date": (latest.get("report_date_as_yyyy_mm_dd") or "")[:10],
                })
            if not out_rows:
                return {}
            as_of = max((r["report_date"] for r in out_rows if r["report_date"]), default=None)
            return {"as_of": as_of, "rows": out_rows}

        return await self._cached("sentiment:cot", build)


def get_sentiment_adapter(
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> SentimentAdapter:
    return SentimentAdapter(redis, settings)
