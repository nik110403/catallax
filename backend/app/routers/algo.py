"""Paper-trading algo hub routes. All under /api/v1.

Orders/positions/account/kill-switch delegate straight to `AlgoService` — every
rejection (400/404/409) is decided there, the router never re-derives a status
code. Strategy CRUD (create/list/start/pause/delete) is this module's own job:
`POST /algo/strategies` runs `StrategyConfig` field validation (pydantic) then
`StrategyRunner.validate` (DB-backed pre-flight) BEFORE the row is inserted, so
a bad strategy is never persisted even transiently.
"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Path, Request
from fastapi.responses import StreamingResponse
from redis.asyncio import Redis
from sqlalchemy import delete, func, select, update
from sqlalchemy.exc import IntegrityError

from app.config import Settings, get_settings
from app.db import session_scope
from app.models import AlgoStrategy
from app.redis import get_redis
from app.services.algo import AlgoService, OrderSpec, _EVENTS, _demo_readonly
from app.services.strategy_runner import StrategyConfig, StrategyRunner

router = APIRouter(tags=["algo"])

_ORDER_ID = Path(ge=1)
_STRATEGY_ID = Path(ge=1)


def get_algo_service(
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> AlgoService:
    return AlgoService(redis, settings)


def get_strategy_runner(
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> StrategyRunner:
    return StrategyRunner(redis, settings)


# ── orders ───────────────────────────────────────────────────────
@router.post("/algo/orders")
async def create_order(spec: OrderSpec, svc: AlgoService = Depends(get_algo_service)) -> dict:
    return await svc.submit_order(spec)


@router.delete("/algo/orders/{order_id}")
async def cancel_order(order_id: int = _ORDER_ID, svc: AlgoService = Depends(get_algo_service)) -> dict:
    return await svc.cancel_order(order_id)


@router.get("/algo/orders")
async def list_orders(
    status: str | None = None, limit: int = 100, svc: AlgoService = Depends(get_algo_service)
) -> dict:
    return {"orders": await svc.list_orders(status, limit)}


@router.get("/algo/fills")
async def list_fills(limit: int = 100, svc: AlgoService = Depends(get_algo_service)) -> dict:
    return {"fills": await svc.list_fills(limit)}


@router.get("/algo/positions")
async def get_positions(svc: AlgoService = Depends(get_algo_service)) -> dict:
    return {"positions": await svc.positions()}


@router.get("/algo/account")
async def get_account(svc: AlgoService = Depends(get_algo_service)) -> dict:
    return await svc.account()


# ── strategies ───────────────────────────────────────────────────
class StrategyCreate(StrategyConfig):
    """POST /algo/strategies body — StrategyConfig's fields plus a unique name."""

    name: str


def _strategy_dict(row: AlgoStrategy) -> dict:
    return {
        "id": row.id, "name": row.name, "config": row.config, "status": row.status,
        "last_rebalance_at": row.last_rebalance_at.isoformat() if row.last_rebalance_at else None,
        "created_at": row.created_at.isoformat(),
    }


@router.post("/algo/strategies")
async def create_strategy(
    body: StrategyCreate,
    runner: StrategyRunner = Depends(get_strategy_runner),
    settings: Settings = Depends(get_settings),
) -> dict:
    if settings.demo_mode:
        raise _demo_readonly()
    cfg = StrategyConfig(**body.model_dump(exclude={"name"}))
    errors = await runner.validate(cfg)
    if errors:
        raise HTTPException(status_code=400, detail={"code": "PREFLIGHT_FAILED", "errors": errors})

    async with session_scope() as db:
        exists = (await db.execute(
            select(func.count()).where(AlgoStrategy.name == body.name)
        )).scalar_one()
        if exists:
            raise HTTPException(status_code=409, detail={
                "code": "NAME_CONFLICT", "message": f"strategy name '{body.name}' already exists",
            })
        row = AlgoStrategy(name=body.name, config=cfg.model_dump(), status="paused")
        db.add(row)
        try:
            await db.commit()
        except IntegrityError:
            # concurrent insert raced the pre-check past the unique constraint backstop
            await db.rollback()
            raise HTTPException(status_code=409, detail={
                "code": "NAME_CONFLICT", "message": f"strategy name '{body.name}' already exists",
            }) from None
        await db.refresh(row)
        return _strategy_dict(row)


@router.get("/algo/strategies")
async def list_strategies(settings: Settings = Depends(get_settings)) -> dict:
    if settings.demo_mode:
        return {"strategies": []}
    async with session_scope() as db:
        rows = (await db.execute(select(AlgoStrategy).order_by(AlgoStrategy.created_at.desc()))).scalars().all()
    return {"strategies": [_strategy_dict(r) for r in rows]}


async def _set_strategy_status(strategy_id: int, status: str, settings: Settings) -> dict:
    if settings.demo_mode:
        raise _demo_readonly()
    async with session_scope() as db:
        res = await db.execute(
            update(AlgoStrategy).where(AlgoStrategy.id == strategy_id)
            .values(status=status).returning(AlgoStrategy.id)
        )
        updated_id = res.scalar_one_or_none()
        await db.commit()
        if updated_id is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "strategy not found"})
        row = (await db.execute(select(AlgoStrategy).where(AlgoStrategy.id == strategy_id))).scalar_one()
        return _strategy_dict(row)


@router.post("/algo/strategies/{strategy_id}/start")
async def start_strategy(
    strategy_id: int = _STRATEGY_ID, settings: Settings = Depends(get_settings)
) -> dict:
    return await _set_strategy_status(strategy_id, "active", settings)


@router.post("/algo/strategies/{strategy_id}/pause")
async def pause_strategy(
    strategy_id: int = _STRATEGY_ID, settings: Settings = Depends(get_settings)
) -> dict:
    return await _set_strategy_status(strategy_id, "paused", settings)


@router.delete("/algo/strategies/{strategy_id}")
async def delete_strategy(
    strategy_id: int = _STRATEGY_ID, settings: Settings = Depends(get_settings)
) -> dict:
    if settings.demo_mode:
        raise _demo_readonly()
    async with session_scope() as db:
        res = await db.execute(
            delete(AlgoStrategy).where(AlgoStrategy.id == strategy_id).returning(AlgoStrategy.id)
        )
        deleted_id = res.scalar_one_or_none()
        await db.commit()
        if deleted_id is None:
            raise HTTPException(status_code=404, detail={"code": "NOT_FOUND", "message": "strategy not found"})
    return {"deleted": strategy_id}


# ── kill switch / status / reconciliation ─────────────────────────
@router.post("/algo/kill")
async def kill(svc: AlgoService = Depends(get_algo_service)) -> dict:
    return await svc.kill()


@router.post("/algo/resume")
async def resume(svc: AlgoService = Depends(get_algo_service)) -> dict:
    return await svc.resume()


@router.get("/algo/status")
async def status(svc: AlgoService = Depends(get_algo_service)) -> dict:
    return await svc.status()


@router.get("/algo/reconciliation")
async def reconciliation(svc: AlgoService = Depends(get_algo_service)) -> dict:
    return await svc.reconciliation()


# ── SSE ──────────────────────────────────────────────────────────
def _sse(data: str) -> str:
    return f"data: {data}\n\n"


@router.get("/algo/stream")
async def stream(
    request: Request,
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> StreamingResponse:
    async def gen():
        # Demo mode never subscribes to the real event channel. Every other algo
        # surface reads empty here, but the worker may still be running against
        # the live paper book — forwarding its events would leak live
        # symbols, prices and quantities in real time, contradicting the empty
        # blotter beside it. Heartbeat-only rather than a 403: this is a read the
        # blotter opens on mount, and the honest demo story is "the book is
        # empty", not an error banner.
        if settings.demo_mode:
            while not await request.is_disconnected():
                yield ": ping\n\n"
                await asyncio.sleep(15.0)
            return

        pubsub = redis.pubsub()
        await pubsub.subscribe(_EVENTS)
        try:
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
        finally:
            await pubsub.unsubscribe(_EVENTS)
            await pubsub.aclose()

    return StreamingResponse(
        gen(), media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive", "X-Accel-Buffering": "no"},
    )
