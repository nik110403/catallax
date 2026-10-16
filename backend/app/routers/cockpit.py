"""Cockpit routes. All under `/api/v1/cockpit`. The share-gate
is global middleware (`ShareGateMiddleware`, wraps the whole app in
`main.py`) — no per-route dependency needed, same as `routers/journal.py`.

`COCKPIT_ENABLED` gates the WHOLE router (`_require_enabled`, off by
default) — every route 503s with a not-enabled body naming the flag until a
host switches it on deliberately. `_require_enabled` also requires
`SHARE_PASSPHRASE` to be set (503 `COCKPIT_REQUIRES_SHARE_GATE`) — an LLM
loop with tool access to the whole portfolio must never be reachable on a
host that never turned the share gate on, mirroring unredacted note export's
own share-gate requirement (`routers/notes.py::_export`). Within an enabled
cockpit, DEMO_MODE then governs read/write shape exactly like
`routers/journal.py`: `ask` (and session creation) 403 before touching
anything; the sessions list returns an empty envelope; the single-session
GET has no demo fixture to show, so it 404s instead of implying "no such
session" vs "no demo data at all" — the same choice `routers/journal.py`'s
`get_entry` makes.
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from app.config import Settings, get_settings
from app.errors import DemoModeReadOnly, NotFound
from app.redis import get_redis
from app.services.cockpit import CockpitDisabled, CockpitRequiresShareGate, CockpitService
from app.services.intel_service import IntelService

router = APIRouter(prefix="/cockpit", tags=["cockpit"])

_KEY = Path(min_length=1, max_length=64)


def _require_enabled(settings: Settings = Depends(get_settings)) -> None:
    if not settings.cockpit_enabled:
        raise CockpitDisabled()
    if not settings.share_passphrase.get_secret_value():
        raise CockpitRequiresShareGate()


class SessionCreateRequest(BaseModel):
    title: str | None = Field(default=None, max_length=200)


class AskRequest(BaseModel):
    text: str = Field(min_length=1, max_length=4000)


@router.post("/sessions", status_code=201, dependencies=[Depends(_require_enabled)])
async def create_session(
    body: SessionCreateRequest,
    settings: Settings = Depends(get_settings), redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    if settings.demo_mode:
        raise DemoModeReadOnly()
    return await CockpitService(redis, settings).create_session(body.title)


@router.get("/sessions", dependencies=[Depends(_require_enabled)])
async def list_sessions(
    settings: Settings = Depends(get_settings), redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    if settings.demo_mode:
        return {"sessions": [], "count": 0}
    rows = await CockpitService(redis, settings).list_sessions()
    return {"sessions": rows, "count": len(rows)}


@router.get("/sessions/{key}", dependencies=[Depends(_require_enabled)])
async def get_session(
    key: str = _KEY,
    settings: Settings = Depends(get_settings), redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    if settings.demo_mode:
        raise NotFound("cockpit sessions are not available in demo mode")
    return await CockpitService(redis, settings).get_session(key)


@router.post("/sessions/{key}/ask", status_code=202, dependencies=[Depends(_require_enabled)])
async def ask(
    body: AskRequest, key: str = _KEY,
    settings: Settings = Depends(get_settings), redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    if settings.demo_mode:
        raise DemoModeReadOnly()
    return await CockpitService(redis, settings).ask(key, body.text)


@router.get("/jobs/{job_id}", dependencies=[Depends(_require_enabled)])
async def get_job(
    job_id: str,
    settings: Settings = Depends(get_settings), redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    """Shape matches the frontend `JobStatus` polling contract:
    `{status, phase, frac, message?}` — NOT `AiJob`'s own `{progress, ...}`."""
    if settings.demo_mode:
        raise NotFound("cockpit jobs are not available in demo mode")
    job = await IntelService(redis, settings).get(job_id)
    message = (job.error or {}).get("message") if job.status == "failed" else None
    return {"status": job.status, "phase": job.phase, "frac": job.progress, "message": message}
