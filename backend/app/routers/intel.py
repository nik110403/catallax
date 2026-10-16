"""AI intel routes. All under /api/v1. The API only enqueues + reads;
the off-API ai-runner worker runs enrichment, so these handlers never block."""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis

from app.adapters.llm.factory import build_llm_adapter
from app.config import Settings, get_settings
from app.redis import get_redis
from app.services.intel_service import AiJob, AiList, IntelService, _channel

router = APIRouter(tags=["intel"])

_JOB_ID = Path(pattern=r"^[A-Za-z0-9\-]{8,36}$")


def get_intel_service(
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> IntelService:
    return IntelService(redis, settings)


@router.post("/intel/enrich", response_model=AiJob)
async def enrich(svc: IntelService = Depends(get_intel_service)) -> AiJob:
    return await svc.enqueue("enrich")


@router.get("/intel/runs", response_model=AiList)
async def list_runs(limit: int = 50, svc: IntelService = Depends(get_intel_service)) -> AiList:
    return await svc.list(limit)


@router.get("/intel/runs/{job_id}", response_model=AiJob)
async def get_run(job_id: str = _JOB_ID, svc: IntelService = Depends(get_intel_service)) -> AiJob:
    return await svc.get(job_id)


@router.get("/intel/news")
async def get_news(limit: int = 60, svc: IntelService = Depends(get_intel_service)) -> dict:
    return {"items": await svc.news(limit)}


@router.get("/intel/events")
async def get_events(limit: int = 200, svc: IntelService = Depends(get_intel_service)) -> dict:
    return {"events": await svc.events(limit)}


@router.get("/intel/status")
async def status(
    redis: Redis = Depends(get_redis), settings: Settings = Depends(get_settings)
) -> dict:
    adapter = build_llm_adapter(settings.llm_provider, settings)
    return {"provider": settings.llm_provider, "healthy": await adapter.health()}


def _sse(data: str) -> str:
    return f"data: {data}\n\n"


@router.get("/intel/runs/{job_id}/stream")
async def stream_run(
    request: Request,
    job_id: str = _JOB_ID,
    redis: Redis = Depends(get_redis),
    svc: IntelService = Depends(get_intel_service),
) -> StreamingResponse:
    channel = _channel(job_id)
    await svc.get(job_id)  # 404s an unknown job before the 200 stream commits

    async def gen():
        pubsub = redis.pubsub()
        await pubsub.subscribe(channel)
        try:
            job = await svc.get(job_id)
            yield _sse(json.dumps({"status": job.status, "progress": job.progress, "phase": job.phase}))
            if job.status in ("done", "failed"):
                yield "event: end\ndata: {}\n\n"
                return
            while True:
                if await request.is_disconnected():
                    break
                msg = await pubsub.get_message(ignore_subscribe_messages=True, timeout=15.0)
                if msg is None:
                    yield ": ping\n\n"
                    continue
                data = msg["data"]
                if isinstance(data, bytes):
                    data = data.decode()
                yield _sse(data)
                try:
                    if json.loads(data).get("status") in ("done", "failed"):
                        yield "event: end\ndata: {}\n\n"
                        break
                except (ValueError, TypeError):
                    pass
        finally:
            await pubsub.unsubscribe(channel)
            await pubsub.aclose()

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
