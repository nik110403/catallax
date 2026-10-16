"""Portfolio router — holdings, allocation, and summary from the ledger engine."""
from __future__ import annotations

from fastapi import APIRouter, Depends
from redis.asyncio import Redis

from app.config import Settings, get_settings
from app.redis import get_redis
from app.services.portfolio import (
    Allocation,
    Holding,
    PortfolioService,
    PortfolioSummary,
)

router = APIRouter(prefix="/portfolio", tags=["portfolio"])


def get_portfolio(
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> PortfolioService:
    return PortfolioService(redis, settings)


@router.get("/holdings")
async def holdings(
    enrich: bool = True,
    svc: PortfolioService = Depends(get_portfolio),
) -> list[Holding]:
    return await svc.holdings(enrich=enrich)


@router.get("/allocation")
async def allocation(svc: PortfolioService = Depends(get_portfolio)) -> Allocation:
    return await svc.allocation()


@router.get("/summary")
async def summary(svc: PortfolioService = Depends(get_portfolio)) -> PortfolioSummary:
    return await svc.summary()
