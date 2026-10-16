"""Market news router — aggregated RSS feeds."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from redis.asyncio import Redis

from app.config import Settings, get_settings
from app.redis import get_redis
from app.services.news import NewsItem, get_news

router = APIRouter(tags=["news"])


@router.get("/news")
async def list_news(
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> list[NewsItem]:
    return await get_news(redis, settings)
