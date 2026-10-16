"""ShillerAdapter (Phase 10a v1.1) — Shiller CAPE (cyclically-adjusted P/E).

Source: multpl.com's "Shiller PE by month" table — the maintained free mirror of
Robert Shiller's series (the Yale `ie_data.xls` is no longer updated). Parsed with
the stdlib HTML parser via BeautifulSoup (no lxml/xlrd dependency). CAPE moves
monthly, so the full history is cached hard in Redis with a stale fallback.

Behind the adapter boundary like every other source — the engine reaches it only
through this class, and the UI only through `/api/v1/market/valuation`.
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx
from bs4 import BeautifulSoup
from fastapi import Depends
from redis.asyncio import Redis

from app.config import Settings, get_settings
from app.redis import get_redis

logger = logging.getLogger("catallax.shiller")

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
_STALE_TTL = 30 * 86400
_MONTHS = {m: i for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"], 1)}


def _parse_date(text: str) -> str | None:
    """'Jun 25, 2026' → '2026-06-25' (month-start when no day given)."""
    m = re.match(r"([A-Za-z]{3})\w*\s+(\d{1,2}),\s+(\d{4})", text.strip())
    if not m:
        return None
    mon = _MONTHS.get(m.group(1)[:3].title())
    if mon is None:
        return None
    return f"{int(m.group(3)):04d}-{mon:02d}-{int(m.group(2)):02d}"


class ShillerAdapter:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self.redis = redis
        self.settings = settings
        self.ttl = settings.shiller_cache_ttl_seconds

    async def cape(self) -> tuple[dict[str, Any], bool]:
        """Return ({points:[{date,cape}], latest_date, cape, earnings_yield}, stale)."""
        key = "shiller:cape"
        raw = await self.redis.get(key)
        if raw is not None:
            try:
                return json.loads(raw), False
            except Exception:
                pass
        try:
            data = await self._build()
        except Exception as exc:
            logger.warning("shiller CAPE build failed: %s", exc)
            data = None
        if data and data.get("points"):
            try:
                payload = json.dumps(data)
                await self.redis.set(key, payload, ex=self.ttl)
                await self.redis.set(f"{key}:stale", payload, ex=_STALE_TTL)
            except Exception as exc:
                logger.warning("shiller cache set failed: %s", exc)
            return data, False
        stale = await self.redis.get(f"{key}:stale")
        if stale is not None:
            try:
                return json.loads(stale), True
            except Exception:
                pass
        return {}, True

    async def _build(self) -> dict[str, Any]:
        async with httpx.AsyncClient(
            timeout=self.settings.openbb_http_timeout,
            headers={"User-Agent": _UA},
            follow_redirects=True,
        ) as client:
            resp = await client.get(self.settings.shiller_cape_url)
            resp.raise_for_status()
            html = resp.text
        soup = BeautifulSoup(html, "html.parser")
        table = soup.find("table")
        if table is None:
            return {}
        points: list[dict[str, Any]] = []
        for tr in table.find_all("tr"):
            tds = tr.find_all("td")
            if len(tds) < 2:
                continue
            date = _parse_date(tds[0].get_text(strip=True))
            try:
                cape = float(re.sub(r"[^0-9.]", "", tds[1].get_text(strip=True)))
            except ValueError:
                continue
            if date is None or cape <= 0:
                continue
            points.append({"date": date, "cape": round(cape, 2)})
        if not points:
            return {}
        points.sort(key=lambda p: p["date"])  # ascending
        latest = points[-1]
        return {
            "points": points,
            "latest_date": latest["date"],
            "cape": latest["cape"],
            "earnings_yield": round(100.0 / latest["cape"], 3),
        }


def get_shiller_adapter(
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> ShillerAdapter:
    return ShillerAdapter(redis, settings)
