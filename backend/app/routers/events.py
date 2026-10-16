from __future__ import annotations

import base64
import json
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, desc, exists, func, select

from app.config import Settings, get_settings
from app.db import session_scope
from app.errors import InvalidRequest, NotFound
from app.models import EventEntity, MarketEvent

router = APIRouter(prefix="/events", tags=["events"])


def _cursor(value: str | None) -> tuple[datetime, int] | None:
    if value is None:
        return None
    try:
        decoded = json.loads(base64.urlsafe_b64decode(value.encode()).decode())
        if not isinstance(decoded, list) or len(decoded) != 2:
            raise ValueError("invalid event cursor")
        stamp, event_id = decoded[0], decoded[1]
        published = datetime.fromisoformat(stamp)
        if (
            isinstance(event_id, bool)
            or not isinstance(event_id, int)
            or event_id < 1
            or event_id > 2**63 - 1
            or published.tzinfo is None
            or published.utcoffset() is None
        ):
            raise ValueError("invalid event cursor")
        published.timestamp()
        return published, event_id
    except (
        ValueError,
        TypeError,
        UnicodeDecodeError,
        json.JSONDecodeError,
        OverflowError,
        IndexError,
        KeyError,
        AttributeError,
    ) as exc:
        raise InvalidRequest("invalid event cursor") from exc


def _encode(row: MarketEvent) -> str:
    return base64.urlsafe_b64encode(
        json.dumps([row.published_at.isoformat(), row.id]).encode()
    ).decode()


async def _items(db: Any, rows: list[MarketEvent]) -> list[dict[str, Any]]:
    ids = [row.id for row in rows]
    entities: dict[int, list[dict[str, Any]]] = {item_id: [] for item_id in ids}
    if ids:
        for entity in (
            await db.execute(select(EventEntity).where(EventEntity.event_id.in_(ids)))
        ).scalars():
            entities[entity.event_id].append(
                {
                    "kind": entity.kind,
                    "value": entity.value,
                    "symbol": entity.symbol,
                    "method": entity.method,
                    "confidence": entity.confidence,
                    "relevance": entity.relevance,
                }
            )
    return [
        {
            "id": row.id,
            "source": row.source,
            "feed": row.feed,
            "event_type": row.event_type,
            "form_type": row.form_type,
            "title": row.title,
            "summary": row.summary,
            "url": row.url,
            "published_at": row.published_at,
            "published_at_estimated": row.published_at_estimated,
            "first_seen_at": row.first_seen_at,
            "available_at": row.available_at,
            "revision": row.revision,
            "supersedes_id": row.supersedes_id,
            "story_key": row.story_key,
            "entities": entities[row.id],
        }
        for row in rows
    ]


@router.get("")
async def list_events(
    since: datetime | None = None,
    until: datetime | None = None,
    source: str | None = None,
    event_type: str | None = None,
    symbol: str | None = None,
    relevant_only: bool = False,
    all_revisions: bool = False,
    limit: int = Query(50, ge=1, le=200),
    cursor: str | None = None,
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    if settings.demo_mode:
        return {"items": [], "next_cursor": None}
    if since and until and since > until:
        raise InvalidRequest("since must not be after until")
    position = _cursor(cursor)
    filters = []
    if since:
        filters.append(MarketEvent.published_at >= since)
    if until:
        filters.append(MarketEvent.published_at <= until)
    if source:
        filters.append(MarketEvent.source == source)
    if event_type:
        filters.append(MarketEvent.event_type == event_type)
    if symbol:
        filters.append(
            exists(
                select(EventEntity.id).where(
                    EventEntity.event_id == MarketEvent.id,
                    EventEntity.symbol == symbol.upper(),
                )
            )
        )
    if relevant_only:
        filters.append(
            exists(
                select(EventEntity.id).where(
                    EventEntity.event_id == MarketEvent.id,
                    func.jsonb_array_length(EventEntity.relevance) > 0,
                )
            )
        )
    if position:
        filters.append(
            (MarketEvent.published_at < position[0])
            | and_(
                MarketEvent.published_at == position[0], MarketEvent.id < position[1]
            )
        )
    async with session_scope() as db:
        query = select(MarketEvent).where(*filters)
        if not all_revisions:
            newer = MarketEvent.__table__.alias("newer")
            query = query.where(
                ~exists(
                    select(newer.c.id).where(
                        newer.c.source == MarketEvent.source,
                        newer.c.feed == MarketEvent.feed,
                        newer.c.source_key == MarketEvent.source_key,
                        newer.c.revision > MarketEvent.revision,
                    )
                )
            )
        rows = (
            (
                await db.execute(
                    query.order_by(
                        desc(MarketEvent.published_at), desc(MarketEvent.id)
                    ).limit(limit + 1)
                )
            )
            .scalars()
            .all()
        )
        more, rows = len(rows) > limit, rows[:limit]
        return {
            "items": await _items(db, rows),
            "next_cursor": _encode(rows[-1]) if more and rows else None,
        }


@router.get("/{event_id}")
async def get_event(
    event_id: int, settings: Settings = Depends(get_settings)
) -> dict[str, Any]:
    if settings.demo_mode:
        raise NotFound("events are not available in demo mode")
    async with session_scope() as db:
        row = await db.get(MarketEvent, event_id)
        if row is None:
            raise NotFound("event not found")
        body = (await _items(db, [row]))[0]
        revisions = (
            (
                await db.execute(
                    select(MarketEvent)
                    .where(
                        MarketEvent.source == row.source,
                        MarketEvent.feed == row.feed,
                        MarketEvent.source_key == row.source_key,
                    )
                    .order_by(MarketEvent.revision)
                )
            )
            .scalars()
            .all()
        )
    body["revisions"] = [
        {
            "id": item.id,
            "revision": item.revision,
            "content_hash": item.content_hash,
            "available_at": item.available_at,
        }
        for item in revisions
    ]
    return body
