"""Analyst journal routes. All under `/api/v1/journal`.
The share-gate is global middleware (`ShareGateMiddleware`, wraps the whole
app in `main.py`) — there is no per-route dependency to add here, same as
`routers/reports.py`.

DEMO_MODE (mirrors `routers/reports.py`/`routers/forecasts.py`): list-shaped
GETs (`GET /entries`, `GET /due`) return an empty, zero-count envelope —
never a DB read, never a `JournalService` construction. The single-item GET
(`GET /entries/{id}`) has no demo fixture to show, so it 404s instead of
returning a shape that would misleadingly imply "no such entry" vs "no demo
data at all" — same choice `reports.py`'s own per-week detail routes make.
Every mutation (`POST /entries`, `PATCH /entries/{id}`, `POST
/entries/{id}/review`) raises `DemoModeReadOnly` (403) before touching
anything.
"""
from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, ConfigDict, Field
from redis.asyncio import Redis

from app.config import Settings, get_settings
from app.errors import DemoModeReadOnly, NotFound
from app.redis import get_redis
from app.services.journal import JournalService

router = APIRouter(prefix="/journal", tags=["journal"])

_DEFAULT_LIST_LIMIT = 100
_MAX_LIST_LIMIT = 1000


class JournalEntryCreateRequest(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    view: str = Field(min_length=1)
    probability: float = Field(gt=0.0, lt=1.0)
    review_date: date
    subject: str = Field(min_length=1, max_length=64)
    entry_date: date | None = None
    thesis: str | None = None
    evidence: list[dict[str, Any]] | None = None
    invalidation: str | None = None
    sizing_note: str | None = None
    confidence_note: str | None = None


class JournalEntryUpdateRequest(BaseModel):
    """Prose fields only — `extra="forbid"` so an attempt to PATCH an
    immutable field (`probability`, `subject`, `entry_date`, `horizon_days`,
    `title`, `view`) 422s at the API boundary; `JournalService.update` also
    rejects them for callers that go straight to the service."""

    model_config = ConfigDict(extra="forbid")

    thesis: str | None = None
    evidence: list[dict[str, Any]] | None = None
    invalidation: str | None = None
    sizing_note: str | None = None
    confidence_note: str | None = None


class JournalReviewRequest(BaseModel):
    note: str = Field(min_length=1, max_length=2000)
    status: str | None = Field(default=None, description="reviewed | superseded — defaults to reviewed")


@router.post("/entries", status_code=201)
async def create_entry(
    body: JournalEntryCreateRequest,
    settings: Settings = Depends(get_settings), redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    if settings.demo_mode:
        raise DemoModeReadOnly()
    return await JournalService(redis, settings).create(body.model_dump())


@router.get("/entries")
async def list_entries(
    status: str | None = None, subject: str | None = None,
    due_before: date | None = Query(None),
    limit: int = Query(_DEFAULT_LIST_LIMIT, ge=1, le=_MAX_LIST_LIMIT),
    settings: Settings = Depends(get_settings), redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    if settings.demo_empty_reads:
        return {"entries": [], "count": 0}
    rows = await JournalService(redis, settings).list(status=status, subject=subject, due_before=due_before, limit=limit)
    return {"entries": rows, "count": len(rows)}


@router.get("/due")
async def due_entries(
    before: date = Query(default_factory=date.today),
    settings: Settings = Depends(get_settings), redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    if settings.demo_empty_reads:
        return {"entries": [], "count": 0}
    rows = await JournalService(redis, settings).due(before)
    return {"entries": rows, "count": len(rows)}


@router.get("/entries/{entry_id}")
async def get_entry(
    entry_id: int = Path(..., ge=1),
    settings: Settings = Depends(get_settings), redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    if settings.demo_empty_reads:
        raise NotFound("journal entries are not available in demo mode")
    return await JournalService(redis, settings).get(entry_id)


@router.patch("/entries/{entry_id}")
async def update_entry(
    body: JournalEntryUpdateRequest, entry_id: int = Path(..., ge=1),
    settings: Settings = Depends(get_settings), redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    if settings.demo_mode:
        raise DemoModeReadOnly()
    payload = body.model_dump(exclude_unset=True)
    return await JournalService(redis, settings).update(entry_id, payload)


@router.post("/entries/{entry_id}/review")
async def review_entry(
    body: JournalReviewRequest, entry_id: int = Path(..., ge=1),
    settings: Settings = Depends(get_settings), redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    if settings.demo_mode:
        raise DemoModeReadOnly()
    return await JournalService(redis, settings).review(entry_id, body.note, body.status)
