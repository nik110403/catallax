"""Markets router — cross-asset overview + geo-map data proxies."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from redis.asyncio import Redis

from app.config import Settings, get_settings
from app.redis import get_redis
from app.services.markets import MarketsOverview, get_overview, get_quakes

router = APIRouter(tags=["markets"])


@router.get("/markets/overview")
async def markets_overview(
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> MarketsOverview:
    return await get_overview(redis, settings)


@router.get("/markets/quakes")
async def markets_quakes(
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> dict:
    # GeoJSON FeatureCollection (USGS, proxied + trimmed + cached).
    return await get_quakes(redis, settings)
