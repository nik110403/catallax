"""Quant/ML Lab routes. All under /api/v1.

POST /lab/factors          validate → enqueue a factor-IC study (off-API job)
POST /lab/models           validate → enqueue a model train/eval (off-API job)
GET  /lab/runs             recent runs (list)
GET  /lab/runs/{id}        one run's status (poll target)
GET  /lab/runs/{id}/report Envelope[FactorReport|ModelReport] (done runs)
GET  /lab/runs/{id}/stream SSE live progress (Redis pub/sub)
GET  /lab/scan             synchronous technical-pattern scan over the F0 backbone
GET  /lab/engines          model-engine + scanner-pattern catalog (for the UI)

The API process only enqueues + reads; the off-API `qlib-lab` worker runs the heavy
compute (factor study / model train), so these handlers never block. `{id}` is a
server-minted UUID. The scan is the one synchronous endpoint — pure-numpy detectors
over cached F0 bars, the same cost class as the risk/research engines.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Path, Query, Request
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis

from app.adapters.envelope import Envelope
from app.adapters.lab.base import FACTORS, FactorConfig, ModelConfig
from app.adapters.lab.factory import known_engines
from app.config import Settings, get_settings
from app.redis import get_redis
from app.services.lab import LabJob, LabList, LabService, _channel
from app.services.signals import SCAN_PATTERNS, ScanReport, ScannerEngine

router = APIRouter(tags=["lab"])

# Server-minted UUID handle — bound the route alphabet (defence-in-depth on the
# Redis channel name + DB lookup).
_JOB_ID = Path(pattern=r"^[A-Za-z0-9\-]{8,36}$")
# Comma-separated symbol/pattern lists in query params — bound the alphabet.
_SYMBOLS = Query(default=None, max_length=2000, pattern=r"^[A-Za-z0-9_.,^=\-]*$")
_PATTERNS = Query(default=None, max_length=512, pattern=r"^[a-z_,]*$")


def get_lab_service(
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> LabService:
    return LabService(redis, settings)


def get_scanner(
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> ScannerEngine:
    return ScannerEngine(redis, settings)


@router.post("/lab/factors", response_model=LabJob)
async def create_factor_study(
    config: FactorConfig,
    svc: LabService = Depends(get_lab_service),
) -> LabJob:
    return await svc.enqueue_factor(config)


@router.post("/lab/models", response_model=LabJob)
async def create_model_run(
    config: ModelConfig,
    svc: LabService = Depends(get_lab_service),
) -> LabJob:
    return await svc.enqueue_model(config)


@router.get("/lab/runs", response_model=LabList)
async def list_runs(
    limit: int = Query(50, ge=1, le=200),
    svc: LabService = Depends(get_lab_service),
) -> LabList:
    return await svc.list(limit)


@router.get("/lab/engines")
async def lab_engines(settings: Settings = Depends(get_settings)) -> dict:
    """Static catalog for the UI: model engines, factor names, scanner patterns."""
    return {
        "engines": known_engines(),
        "default_engine": settings.lab_default_model_engine,
        "factors": list(FACTORS),
        "scan_patterns": [{"name": k, "label": v} for k, v in SCAN_PATTERNS.items()],
    }


@router.get("/lab/scan", response_model=Envelope[ScanReport])
async def scan(
    patterns: str | None = _PATTERNS,
    universe: str | None = _SYMBOLS,
    emit: bool = False,
    scanner: ScannerEngine = Depends(get_scanner),
) -> Envelope[ScanReport]:
    # NOTE: emit=true makes this GET write `signals` rows (an A6 "side-effecting GET").
    # Accepted for this single-user, no-auth LAN app with no CDN/prefetch in front: emit
    # is an explicit, default-off user action (the Scanner widget's checkbox), consistent
    # with the app's overall no-auth posture. Harden to a POST variant if auth/CDN ever land.
    pats = [p for p in (patterns or "").split(",") if p] or None
    uni = [s.strip().upper() for s in (universe or "").split(",") if s.strip()] or None
    report = await scanner.scan(pats, uni, emit=emit)
    return Envelope.fresh(report, source="lab:scan", cache_hit=False,
                          stale=bool(report.warnings), warnings=report.warnings)


@router.get("/lab/runs/{job_id}", response_model=LabJob)
async def get_run(
    job_id: str = _JOB_ID,
    svc: LabService = Depends(get_lab_service),
) -> LabJob:
    return await svc.get(job_id)


@router.get("/lab/runs/{job_id}/report", response_model=None)
async def get_run_report(
    job_id: str = _JOB_ID,
    svc: LabService = Depends(get_lab_service),
):
    # FactorReport | ModelReport depending on the run kind — the Envelope carries the
    # concrete payload; the frontend discriminates on the job's `kind`.
    return await svc.report(job_id)


def _sse(data: str) -> str:
    return f"data: {data}\n\n"


@router.get("/lab/runs/{job_id}/stream")
async def stream_run(
    request: Request,
    job_id: str = _JOB_ID,
    redis: Redis = Depends(get_redis),
    svc: LabService = Depends(get_lab_service),
) -> StreamingResponse:
    channel = _channel(job_id)
    # Validate existence BEFORE returning the stream so an unknown job 404s.
    await svc.get(job_id)

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
        gen(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
