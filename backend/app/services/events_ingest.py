from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.exc import DBAPIError, DataError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.events.base import RawEvent, SourceAdapter
from app.config import get_settings
from app.models import MarketEvent, SourceCursor

logger = logging.getLogger(__name__)
_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "with",
        "from",
        "that",
        "this",
        "are",
        "was",
        "were",
        "has",
        "have",
        "will",
        "its",
        "not",
        "into",
        "over",
        "under",
        "via",
        "per",
        "but",
        "our",
        "you",
    }
)


@dataclass
class IngestStats:
    seen: int = 0
    new: int = 0
    revised: int = 0
    duplicate: int = 0
    rejected: int = 0


def canonical_hash(event: RawEvent) -> str:
    payload = {
        "title": event.title.strip()[:500],
        "summary": (event.summary or "").strip(),
        "url": event.url[:1000],
        "form_type": event.form_type,
        "event_type": event.event_type,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def story_key(title: str) -> str | None:
    folded = title.encode("ascii", "ignore").decode().lower()
    words = [
        word
        for word in re.findall(r"[a-z0-9]+", folded)
        if len(word) >= 3 and word not in _STOPWORDS
    ][:8]
    return (
        hashlib.sha1(" ".join(sorted(words)).encode()).hexdigest()
        if len(words) >= 3
        else None
    )


def _source_key(value: str) -> str:
    encoded = value.encode("utf-8")
    return (
        value if len(value) <= 255 else "sha256:" + hashlib.sha256(encoded).hexdigest()
    )


def _safe_key(value: object) -> str:
    raw = value.encode("utf-8", "surrogatepass") if isinstance(value, str) else b""
    return hashlib.sha256(raw).hexdigest()


def _safe_error(exc: BaseException, reason: str) -> str:
    return f"{type(exc).__name__}:{reason}"


def _stamp(value: datetime | None) -> None:
    if value is None:
        return
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError("invalid timestamp")
    try:
        value.timestamp()
    except OverflowError as exc:
        raise ValueError("invalid timestamp") from exc


def _text(value: object, *, optional: bool = False) -> None:
    if value is None and optional:
        return
    if not isinstance(value, str):
        raise ValueError("invalid field")


def _validate(event: RawEvent) -> None:
    _text(event.source_key)
    try:
        event.source_key.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("invalid field") from exc
    _text(event.title)
    _text(event.summary, optional=True)
    _text(event.url)
    _text(event.form_type, optional=True)
    _text(event.event_type)
    if event.first_seen_at is None:
        raise ValueError("invalid timestamp")
    _stamp(event.first_seen_at)
    _stamp(event.published_at)
    if not isinstance(event.raw, dict):
        raise ValueError("invalid field")
    if not isinstance(event.entity_hints, list):
        raise ValueError("invalid field")
    try:
        for hint in event.entity_hints:
            list(hint)
    except TypeError as exc:
        raise ValueError("invalid field") from exc


def _raw(value: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(value, default=str, separators=(",", ":")).encode()
    return (
        value
        if len(encoded) <= 65536
        else {"truncated": True, "sha256": hashlib.sha256(encoded).hexdigest()}
    )


async def ingest(
    session: AsyncSession, adapter: SourceAdapter, now: datetime
) -> IngestStats:
    settings = get_settings()
    stats = IngestStats()
    cursor = await session.get(
        SourceCursor, {"source": adapter.source, "feed": adapter.feed}
    )
    if cursor is None:
        cursor = SourceCursor(source=adapter.source, feed=adapter.feed, cursor={})
        session.add(cursor)
        await session.flush()
    if cursor.next_attempt_at and cursor.next_attempt_at > now:
        return stats
    cursor.last_attempt_at = now
    try:
        result = await asyncio.wait_for(
            adapter.poll(cursor.cursor, cursor.etag, cursor.last_modified),
            settings.events_adapter_timeout_seconds,
        )
        latest: dict[str, RawEvent] = {}
        for event in result.events:
            try:
                _validate(event)
                key = _source_key(event.source_key)
            except ValueError as exc:
                stats.rejected += 1
                logger.warning(
                    "event rejected source=%s feed=%s key=%s error=%s",
                    adapter.source,
                    adapter.feed,
                    _safe_key(getattr(event, "source_key", "")),
                    _safe_error(exc, "rejected"),
                )
                continue
            previous = latest.get(key)
            if previous is None or (event.published_at or event.first_seen_at) > (
                previous.published_at or previous.first_seen_at
            ):
                latest[key] = event
        for key, event in latest.items():
            stats.seen += 1
            try:
                async with session.begin_nested():
                    latest_row = (
                        await session.execute(
                            select(MarketEvent)
                            .where(
                                MarketEvent.source == event.source,
                                MarketEvent.feed == event.feed,
                                MarketEvent.source_key == key,
                            )
                            .order_by(desc(MarketEvent.revision))
                            .limit(1)
                        )
                    ).scalar_one_or_none()
                    digest = canonical_hash(event)
                    if latest_row and latest_row.content_hash == digest:
                        stats.duplicate += 1
                        continue
                    published = event.published_at or event.first_seen_at
                    estimated = event.published_at is None
                    raw = _raw(dict(event.raw))
                    if event.published_at and event.published_at > event.first_seen_at:
                        raw["published_upstream"] = event.published_at.isoformat()
                        published, estimated = event.first_seen_at, True
                    row = MarketEvent(
                        source=event.source,
                        feed=event.feed,
                        source_key=key,
                        revision=(latest_row.revision + 1 if latest_row else 1),
                        supersedes_id=(latest_row.id if latest_row else None),
                        content_hash=digest,
                        event_type=event.event_type,
                        form_type=event.form_type,
                        title=event.title[:500],
                        summary=event.summary,
                        url=event.url[:1000],
                        published_at=published,
                        first_seen_at=event.first_seen_at,
                        available_at=func.greatest(
                            func.clock_timestamp(), event.first_seen_at
                        ),
                        published_at_estimated=estimated,
                        entity_hints=[list(hint) for hint in event.entity_hints],
                        story_key=story_key(event.title),
                        raw=raw,
                    )
                    session.add(row)
                    await session.flush()
                    if latest_row:
                        stats.revised += 1
                    else:
                        stats.new += 1
            except (IntegrityError, DataError, ValueError, DBAPIError) as exc:
                stats.rejected += 1
                logger.warning(
                    "event rejected source=%s feed=%s key=%s error=%s",
                    adapter.source,
                    adapter.feed,
                    _safe_key(event.source_key),
                    _safe_error(exc, "rejected"),
                )
        cursor.cursor, cursor.etag, cursor.last_modified = (
            result.cursor,
            result.etag,
            result.last_modified,
        )
        (
            cursor.last_success_at,
            cursor.last_error,
            cursor.consecutive_failures,
            cursor.next_attempt_at,
        ) = now, None, 0, None
        cursor.events_seen += stats.seen
        cursor.events_new += stats.new
        cursor.events_revised += stats.revised
        cursor.events_duplicate += stats.duplicate
        cursor.events_rejected += stats.rejected
        await session.commit()
    except Exception as exc:
        await session.rollback()
        cursor = await session.get(
            SourceCursor, {"source": adapter.source, "feed": adapter.feed}
        )
        if cursor is None:
            cursor = SourceCursor(source=adapter.source, feed=adapter.feed, cursor={})
            session.add(cursor)
        failures = (cursor.consecutive_failures or 0) + 1
        cursor.last_attempt_at = now
        cursor.consecutive_failures = failures
        cursor.last_failure_at = now
        cursor.last_error = _safe_error(exc, "poll")
        cursor.next_attempt_at = now + timedelta(
            seconds=min((2**failures) * settings.events_tick_interval_seconds, 86400)
        )
        if failures == settings.events_dead_letter_after:
            cursor.dead_letter_count = (cursor.dead_letter_count or 0) + 1
            logger.error(
                "event feed dead-letter source=%s feed=%s", adapter.source, adapter.feed
            )
        await session.commit()
    return stats
