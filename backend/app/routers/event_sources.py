from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import func, select

from app.config import Settings, get_settings
from app.db import session_scope
from app.models import Alert, AlertDelivery, MarketEvent, SourceCursor

router = APIRouter(prefix="/event-sources", tags=["events"])


def _zero() -> dict[str, Any]:
    return {
        "duplicate_rate": None,
        "alerts_fired_7d": 0,
        "alerts_acked_7d": 0,
        "ack_rate_7d": None,
        "alerts_suppressed_7d": 0,
        "suppress_reasons_7d": {},
        "alerts_dispositioned_7d": 0,
        "usefulness_rate_7d": None,
    }


@router.get("")
async def list_event_sources(
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    if settings.demo_mode:
        return {"items": [], "summary": _zero()}
    now, since = (
        datetime.now(timezone.utc),
        datetime.now(timezone.utc) - timedelta(days=7),
    )
    async with session_scope() as db:
        newest = {
            (source, feed): published
            for source, feed, published in (
                await db.execute(
                    select(
                        MarketEvent.source,
                        MarketEvent.feed,
                        func.max(MarketEvent.published_at),
                    ).group_by(MarketEvent.source, MarketEvent.feed)
                )
            ).all()
        }
        rows = (await db.execute(select(SourceCursor))).scalars().all()
        items = [
            {
                "source": row.source,
                "feed": row.feed,
                "last_attempt_at": row.last_attempt_at,
                "last_success_at": row.last_success_at,
                "last_failure_at": row.last_failure_at,
                "last_error": row.last_error,
                "lag_seconds": (now - newest[(row.source, row.feed)]).total_seconds()
                if (row.source, row.feed) in newest
                else None,
                "consecutive_failures": row.consecutive_failures,
                "dead_letter_count": row.dead_letter_count,
                "next_attempt_at": row.next_attempt_at,
                "events_seen": row.events_seen,
                "events_new": row.events_new,
                "events_revised": row.events_revised,
                "events_duplicate": row.events_duplicate,
                "events_rejected": row.events_rejected,
                "duplicate_rate": row.events_duplicate / row.events_seen
                if row.events_seen
                else None,
            }
            for row in rows
        ]
        fired, acked = (
            await db.execute(
                select(func.count(Alert.id), func.count(Alert.acked_at)).where(
                    Alert.fired_at >= since
                )
            )
        ).one()
        dispositions = (
            await db.execute(
                select(Alert.disposition, func.count(Alert.id))
                .where(Alert.fired_at >= since, Alert.disposition.is_not(None))
                .group_by(Alert.disposition)
            )
        ).all()
        reason = func.coalesce(AlertDelivery.detail["reason"].as_string(), "unknown")
        suppress = (
            await db.execute(
                select(reason, func.count(AlertDelivery.id))
                .where(
                    AlertDelivery.action == "suppress",
                    AlertDelivery.attempted_at >= since,
                )
                .group_by(reason)
            )
        ).all()
    summary = _zero()
    summary.update(
        {
            "alerts_fired_7d": fired,
            "alerts_acked_7d": acked,
            "ack_rate_7d": acked / fired if fired else None,
            "alerts_dispositioned_7d": sum(count for _, count in dispositions),
        }
    )
    useful = dict(dispositions).get("useful", 0)
    false = dict(dispositions).get("false_positive", 0)
    summary["usefulness_rate_7d"] = (
        useful / (useful + false) if useful + false else None
    )
    summary["alerts_suppressed_7d"] = sum(count for _, count in suppress)
    summary["suppress_reasons_7d"] = {
        str(label or "unknown"): count for label, count in suppress
    }
    seen, duplicates = (
        sum(item["events_seen"] for item in items),
        sum(item["events_duplicate"] for item in items),
    )
    summary["duplicate_rate"] = duplicates / seen if seen else None
    return {"items": items, "summary": summary}
