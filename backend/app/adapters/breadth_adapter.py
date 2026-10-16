"""BreadthAdapter (Phase 10a v1.2) — whole-market breadth via the WSJ markets diary.

The WSJ markets-diary endpoint is a free, unauthenticated public market-data API
exposing the daily advance/decline diary for NYSE / Nasdaq / NYSE American / NYSE
Arca: issues traded, advances, declines, unchanged, new highs/lows, up/down/total
volume, and the closing Arms (TRIN). It carries only today/prev/week-ago per row —
not a series — so the engine persists one snapshot per session date and builds the
breadth oscillators (A/D line, McClellan) forward (see `market_breadth` table).

Behind the adapter boundary like every other source (same posture as the CNN /
multpl / CFTC feeds): the engine reaches it only here, the UI only through
`/api/v1/market/breadth`. Redis-cached with a stale fallback.
"""
from __future__ import annotations

import json
import logging
import math
import re
from typing import Any

import httpx
from fastapi import Depends
from redis.asyncio import Redis

from app.config import Settings, get_settings
from app.redis import get_redis
from app.upstream import guard

logger = logging.getLogger("catallax.breadth")

_UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"
_STALE_TTL = 14 * 86400
_MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July", "August",
     "September", "October", "November", "December"], 1)}

# WSJ diary row id → our field name. Volume ids appear twice per set (diary then
# consolidated); we take the first occurrence, which is the diary figure.
_ROW_FIELDS: dict[str, str] = {
    "issuestraded": "issues",
    "advances": "advances",
    "declines": "declines",
    "unchanged": "unchanged",
    "newhighs": "new_highs",
    "newlows": "new_lows",
    "advvolume": "adv_volume",
    "declvolume": "decl_volume",
    "totalvolume": "total_volume",
    "closingarmstrin": "trin",
}
_INT_FIELDS = {"issues", "advances", "declines", "unchanged", "new_highs", "new_lows"}
_BIG_FIELDS = {"adv_volume", "decl_volume", "total_volume"}


def _num(text: Any) -> float | None:
    if text is None:
        return None
    s = re.sub(r"[^0-9.\-]", "", str(text))
    if s in ("", "-", ".", "-."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_session_date(text: str | None) -> str | None:
    """'Thursday, June 25, 2026' → '2026-06-25'."""
    if not text:
        return None
    m = re.search(r"([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})", text)
    if not m:
        return None
    mon = _MONTHS.get(m.group(1).title())
    if mon is None:
        return None
    return f"{int(m.group(3)):04d}-{mon:02d}-{int(m.group(2)):02d}"


class BreadthAdapter:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self.redis = redis
        self.settings = settings
        self.ttl = settings.breadth_cache_ttl_seconds

    async def diary(self) -> tuple[dict[str, Any], bool]:
        """Return ({session_date, markets:{NAME:{fields}}, prev_markets:{...}}, stale)."""
        key = "breadth:diary"
        raw = await self.redis.get(key)
        if raw is not None:
            try:
                return json.loads(raw), False
            except Exception as exc:
                logger.debug("breadth diary cache deserialise failed: %s", exc)
        try:
            data = await self._build()
        except Exception as exc:
            logger.warning("breadth diary build failed: %s", exc)
            data = None
        if data and data.get("markets"):
            try:
                payload = json.dumps(data)
                await self.redis.set(key, payload, ex=self.ttl)
                await self.redis.set(f"{key}:stale", payload, ex=_STALE_TTL)
            except Exception as exc:
                logger.warning("breadth cache set failed: %s", exc)
            return data, False
        stale = await self.redis.get(f"{key}:stale")
        if stale is not None:
            try:
                return json.loads(stale), True
            except Exception as exc:
                logger.debug("breadth stale cache deserialise failed: %s", exc)
        return {}, True

    async def _build(self) -> dict[str, Any]:
        guard("breadth", self.settings)
        async with httpx.AsyncClient(
            timeout=self.settings.openbb_http_timeout,
            headers={"User-Agent": _UA},
            follow_redirects=True,
        ) as client:
            resp = await client.get(self.settings.wsj_marketsdiary_url)
            resp.raise_for_status()
            if len(resp.content) > 512_000:  # the diary is ~15KB; guard a misconfigured URL
                raise ValueError(f"WSJ diary response too large: {len(resp.content)} bytes")
            body = resp.json()
        data = body.get("data") or {}
        session_date = _parse_session_date(data.get("timestamp"))
        markets: dict[str, dict[str, Any]] = {}
        prev_markets: dict[str, dict[str, Any]] = {}
        for s in data.get("instrumentSets") or []:
            hdr = s.get("headerFields") or []
            name = hdr[0].get("label") if hdr else None
            if not name:
                continue
            name = str(name)[:24]  # market column is VARCHAR(24); never let upstream overflow it
            fields: dict[str, Any] = {}
            prev_fields: dict[str, Any] = {}
            for row in s.get("instruments") or []:
                field = _ROW_FIELDS.get(row.get("id"))
                if field is None or field in fields:  # first occurrence wins
                    continue
                for src, dst in (("latestClose", fields), ("previousClose", prev_fields)):
                    val = _num(row.get(src))
                    if val is None or not math.isfinite(val):  # guard int(inf)/int(nan)
                        continue
                    if field in _INT_FIELDS or field in _BIG_FIELDS:
                        # Bound untrusted upstream values to the DB column type
                        # (Integer/BigInteger) instead of failing at INSERT.
                        if abs(val) >= (2**31 if field in _INT_FIELDS else 2**63):
                            continue
                        dst[field] = int(val)
                    else:
                        dst[field] = round(val, 4)
            if fields.get("advances") is not None or fields.get("declines") is not None:
                markets[name] = fields
            if prev_fields.get("advances") is not None or prev_fields.get("declines") is not None:
                prev_markets[name] = prev_fields
        return {"session_date": session_date, "markets": markets, "prev_markets": prev_markets}


def get_breadth_adapter(
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> BreadthAdapter:
    return BreadthAdapter(redis, settings)
