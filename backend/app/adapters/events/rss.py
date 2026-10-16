from __future__ import annotations

import asyncio
import re
import time
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import feedparser
import httpx

from app.adapters.events.base import PollResult, RawEvent, SourceAdapter

_RATE_DECISION = re.compile(
    r"interest rate|monetary policy decision|FOMC statement", re.IGNORECASE
)


def _named_pairs(raw: str) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, url = part.split("=", 1)
        name, url = name.strip(), url.strip()
        if name and url:
            out.append((name, url))
    return out


def _feed_slug(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.netloc}{parsed.path}".rstrip("/") or parsed.netloc


def _entry_key(entry: Any) -> str:
    for field in ("id", "guid"):
        value = (
            entry.get(field) if hasattr(entry, "get") else getattr(entry, field, None)
        )
        if isinstance(value, str) and value.strip():
            return value.strip()
    link = entry.get("link") if hasattr(entry, "get") else getattr(entry, "link", None)
    return (link or "").strip()


def _published(entry: Any) -> datetime | None:
    parsed = getattr(entry, "published_parsed", None) or getattr(
        entry, "updated_parsed", None
    )
    if not parsed:
        return None
    try:
        return datetime(*parsed[:6], tzinfo=timezone.utc)
    except (ValueError, TypeError, OverflowError):
        return None


def _cb_type(title: str) -> str:
    if _RATE_DECISION.search(title or ""):
        return "rate_decision"
    if re.search(r"speech", title or "", re.IGNORECASE):
        return "speech"
    return "press_release"


class RssSourceAdapter(SourceAdapter):
    rate_limit_per_min = 60

    def __init__(
        self,
        *,
        source: str,
        feed: str,
        url: str,
        event_type: str,
        hints: list[tuple[str, str, str]],
        settings: Any,
        type_from_title: bool = False,
    ) -> None:
        self.source = source
        self.feed = feed
        self._url = url
        self._event_type = event_type
        self._hints = hints
        self._type_from_title = type_from_title
        self._timeout = settings.events_http_timeout_seconds
        self._ua = settings.events_user_agent
        self._max_items = settings.events_max_items_per_feed
        self._stamp = 0.0

    async def _limit(self) -> None:
        interval = 60.0 / max(self.rate_limit_per_min, 1)
        now = time.monotonic()
        wait = self._stamp + interval - now
        if wait > 0:
            await asyncio.sleep(wait)
        self._stamp = time.monotonic()

    async def _get(
        self, client: httpx.AsyncClient, headers: dict[str, str]
    ) -> httpx.Response:
        delay = 0.5
        response: httpx.Response | None = None
        for attempt in range(4):
            response = await client.get(self._url, headers=headers)
            if response.status_code == 304:
                return response
            if response.status_code in {429, 500, 502, 503, 504} and attempt < 3:
                retry_after = response.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else delay
                except ValueError:
                    wait = delay
                await asyncio.sleep(max(wait, 0))
                delay *= 2
                continue
            response.raise_for_status()
            return response
        assert response is not None
        response.raise_for_status()
        return response

    async def poll(
        self, cursor: dict[str, Any], etag: str | None, last_modified: str | None
    ) -> PollResult:
        await self._limit()
        extra: dict[str, str] = {}
        if etag:
            extra["If-None-Match"] = etag
        if last_modified:
            extra["If-Modified-Since"] = last_modified
        async with httpx.AsyncClient(
            timeout=self._timeout,
            follow_redirects=True,
            headers={"User-Agent": self._ua},
        ) as client:
            response = await self._get(client, extra)
        if response.status_code == 304:
            return PollResult(
                events=[],
                cursor=cursor or {},
                etag=etag,
                last_modified=last_modified,
                not_modified=True,
            )
        seen = datetime.now(timezone.utc)
        parsed = await asyncio.to_thread(feedparser.parse, response.content)
        events: list[RawEvent] = []
        newest: datetime | None = None
        for entry in parsed.entries[: self._max_items]:
            title = (entry.get("title") or "").strip()
            link = (entry.get("link") or "").strip()
            key = _entry_key(entry)
            if not title or not link or not key:
                continue
            published = _published(entry)
            if published is not None and (newest is None or published > newest):
                newest = published
            event_type = _cb_type(title) if self._type_from_title else self._event_type
            events.append(
                RawEvent(
                    source=self.source,
                    feed=self.feed,
                    source_key=key,
                    event_type=event_type,
                    form_type=None,
                    title=title,
                    summary=entry.get("summary"),
                    url=link,
                    published_at=published,
                    first_seen_at=seen,
                    raw={"id": key},
                    entity_hints=list(self._hints),
                )
            )
        out_cursor = (
            {"newest_published_at": newest.isoformat()}
            if newest
            else dict(cursor or {})
        )
        return PollResult(
            events=events,
            cursor=out_cursor,
            etag=response.headers.get("ETag"),
            last_modified=response.headers.get("Last-Modified"),
        )


def build(settings: Any, redis: Any, universe: Any) -> list[SourceAdapter]:
    adapters: list[SourceAdapter] = []
    for url in settings.news_feed_urls:
        adapters.append(
            RssSourceAdapter(
                source="rss",
                feed=_feed_slug(url),
                url=url,
                event_type="news",
                hints=[],
                settings=settings,
            )
        )
    for name, url in _named_pairs(settings.events_cb_feeds):
        adapters.append(
            RssSourceAdapter(
                source="central_bank",
                feed=name,
                url=url,
                event_type="press_release",
                hints=[("central_bank", name, "configured")],
                settings=settings,
                type_from_title=True,
            )
        )
    for symbol, url in _named_pairs(settings.events_ir_feeds):
        token = symbol.upper()
        adapters.append(
            RssSourceAdapter(
                source="company_ir",
                feed=f"ir:{token}",
                url=url,
                event_type="ir_release",
                hints=[("symbol", token, "configured")],
                settings=settings,
            )
        )
    return adapters
