"""News aggregator — fetches configured RSS feeds concurrently, normalises
them into `NewsItem`s, and caches the merged result in Redis.

Per-feed failures are swallowed (a dead feed never sinks the whole response);
the endpoint returns whatever feeds responded.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from urllib.parse import urlparse

import feedparser
import httpx
from pydantic import BaseModel
from redis.asyncio import Redis

from app.config import Settings
from app.upstream import STALE_SUFFIX, STALE_TTL, is_cache_only

_CACHE_KEY = "news:aggregate"
_UA = "Mozilla/5.0 (catallax news aggregator)"


class NewsItem(BaseModel):
    title: str
    link: str
    source: str
    published: datetime | None = None
    summary: str | None = None


def _parse_published(entry: object) -> datetime | None:
    parsed = getattr(entry, "published_parsed", None) or getattr(
        entry, "updated_parsed", None
    )
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6], tzinfo=timezone.utc)
    except (ValueError, TypeError):
        return None


async def _fetch_feed(client: httpx.AsyncClient, url: str) -> list[NewsItem]:
    resp = await client.get(url)
    resp.raise_for_status()
    # feedparser is a synchronous CPU-bound parser — keep it off the event loop.
    parsed = await asyncio.to_thread(feedparser.parse, resp.content)
    source = parsed.feed.get("title") or urlparse(url).netloc
    items: list[NewsItem] = []
    for entry in parsed.entries:
        link = entry.get("link")
        title = entry.get("title")
        if not link or not title:
            continue
        items.append(
            NewsItem(
                title=title,
                link=link,
                source=source,
                published=_parse_published(entry),
                summary=entry.get("summary"),
            )
        )
    return items


async def _fetch_all(urls: list[str]) -> list[NewsItem]:
    async with httpx.AsyncClient(
        timeout=10, follow_redirects=True, headers={"User-Agent": _UA}
    ) as client:
        results = await asyncio.gather(
            *(_fetch_feed(client, u) for u in urls), return_exceptions=True
        )
    items: list[NewsItem] = []
    for result in results:
        if isinstance(result, list):
            items.extend(result)
    return items


async def get_news(redis: Redis, settings: Settings) -> list[NewsItem]:
    cached = await redis.get(_CACHE_KEY)
    if cached is not None:
        try:
            return [NewsItem.model_validate(i) for i in json.loads(cached)]
        except Exception:
            pass

    if is_cache_only(settings):
        stale = await redis.get(_CACHE_KEY + STALE_SUFFIX)
        if stale is not None:
            try:
                return [NewsItem.model_validate(i) for i in json.loads(stale)]
            except Exception:
                pass
        return []

    items = await _fetch_all(settings.news_feed_urls)
    # Newest first; undated items sink to the bottom.
    items.sort(
        key=lambda i: i.published or datetime.min.replace(tzinfo=timezone.utc),
        reverse=True,
    )
    items = items[: settings.news_max_items]

    payload = json.dumps([i.model_dump(mode="json") for i in items])
    await redis.set(_CACHE_KEY, payload, ex=settings.news_cache_ttl_seconds)
    if items:
        await redis.set(_CACHE_KEY + STALE_SUFFIX, payload, ex=STALE_TTL)
    return items
