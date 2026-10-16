from redis.asyncio import Redis

from app.config import get_settings

_redis: Redis | None = None


def init_redis() -> Redis:
    global _redis
    _redis = Redis.from_url(get_settings().redis_url, decode_responses=True)
    return _redis


async def dispose_redis() -> None:
    global _redis
    if _redis is not None:
        await _redis.aclose()
    _redis = None


def get_redis() -> Redis:
    assert _redis is not None, "Redis not initialised"
    return _redis
