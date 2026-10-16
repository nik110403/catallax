"""Backtester routes (Phase 9, engine). All under /api/v1.

POST /backtests          validate (pre-flight) → enqueue → 202-style job
GET  /backtests          recent jobs (list)
GET  /backtests/{id}     one job's status (poll target)
GET  /backtests/{id}/report   Envelope[BacktestReport] (done jobs)
GET  /backtests/{id}/stream   SSE live progress (Redis pub/sub)

The API process only enqueues + reads; the off-API worker runs the engine, so
these handlers never block on a backtest. `{id}` is a server-minted UUID.
"""
from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Path, Request
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis

from app.adapters.backtest.base import BacktestConfig, BacktestReport
from app.adapters.envelope import Envelope
from app.config import Settings, get_settings
from app.redis import get_redis
from app.services.backtest import (
    BacktestJob,
    BacktestList,
    BacktestService,
    _channel,
)

router = APIRouter(tags=["backtests"])

# Server-minted UUID handle — bound the route alphabet (defence-in-depth on the
# Redis channel name + DB lookup).
_JOB_ID = Path(pattern=r"^[A-Za-z0-9\-]{8,36}$")


def get_backtest_service(
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> BacktestService:
    return BacktestService(redis, settings)


@router.post("/backtests", response_model=BacktestJob)
async def create_backtest(
    config: BacktestConfig,
    svc: BacktestService = Depends(get_backtest_service),
) -> BacktestJob:
    # Pre-flight runs inside enqueue: PreflightFailed (400 + structured details) is
    # raised BEFORE the job is queued, so the engine is never invoked on a bad spec.
    return await svc.enqueue(config)


@router.get("/backtests", response_model=BacktestList)
async def list_backtests(
    limit: int = 50,
    svc: BacktestService = Depends(get_backtest_service),
) -> BacktestList:
    return await svc.list(limit)


@router.get("/backtests/{job_id}", response_model=BacktestJob)
async def get_backtest(
    job_id: str = _JOB_ID,
    svc: BacktestService = Depends(get_backtest_service),
) -> BacktestJob:
    return await svc.get(job_id)


@router.get("/backtests/{job_id}/report", response_model=Envelope[BacktestReport])
async def get_backtest_report(
    job_id: str = _JOB_ID,
    svc: BacktestService = Depends(get_backtest_service),
) -> Envelope[BacktestReport]:
    return await svc.report(job_id)


def _sse(data: str) -> str:
    return f"data: {data}\n\n"


@router.get("/backtests/{job_id}/stream")
async def stream_backtest(
    request: Request,
    job_id: str = _JOB_ID,
    redis: Redis = Depends(get_redis),
    svc: BacktestService = Depends(get_backtest_service),
) -> StreamingResponse:
    channel = _channel(job_id)
    # Validate existence BEFORE returning the stream so an unknown job 404s (raising
    # inside the generator can't change the already-committed 200). gen() re-reads the
    # state after subscribing, which also closes the job-finishes-mid-setup race.
    await svc.get(job_id)

    async def gen():
        # Subscribe FIRST, THEN read current state — closes the race where the job
        # finishes between the state read and the subscribe (terminal msg buffered).
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
                    yield ": ping\n\n"  # heartbeat keeps the proxy connection alive
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
            # Tell nginx NOT to buffer this response (no nginx.conf change needed).
            "X-Accel-Buffering": "no",
        },
    )
