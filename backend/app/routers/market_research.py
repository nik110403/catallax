"""Market Research router (Phase 10a) — top-down / cross-asset analytics.

All seven slice endpoints share the same cached bundle (built once by
`MarketResearchEngine.bundle()`). No DB session is needed here — history reads
use their own `session_scope()` internally (same pattern as risk.py).
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from redis.asyncio import Redis

from app.adapters.envelope import Envelope
from app.config import Settings, get_settings
from app.redis import get_redis
from app.services.market_research import (
    BreadthReport,
    ConditionsReport,
    CrossAssetReport,
    MarketResearchEngine,
    RegimeReport,
    RotationReport,
    RRGReport,
    SentimentReport,
    ValuationReport,
    VolatilityReport,
)

router = APIRouter(tags=["market-research"])


def get_market_research_engine(
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> MarketResearchEngine:
    # No DB session injected — history reads use session_scope() per series.
    return MarketResearchEngine(redis, settings)


@router.get("/market/regime")
async def market_regime(
    engine: MarketResearchEngine = Depends(get_market_research_engine),
) -> Envelope[RegimeReport]:
    data, meta = await engine.regime()
    return Envelope.fresh(
        data, source=meta["source"], stale=meta["stale"], warnings=meta["warnings"]
    )


@router.get("/market/cross-asset")
async def market_cross_asset(
    engine: MarketResearchEngine = Depends(get_market_research_engine),
) -> Envelope[CrossAssetReport]:
    data, meta = await engine.cross_asset()
    return Envelope.fresh(
        data, source=meta["source"], stale=meta["stale"], warnings=meta["warnings"]
    )


@router.get("/market/rotation")
async def market_rotation(
    engine: MarketResearchEngine = Depends(get_market_research_engine),
) -> Envelope[RotationReport]:
    data, meta = await engine.rotation()
    return Envelope.fresh(
        data, source=meta["source"], stale=meta["stale"], warnings=meta["warnings"]
    )


@router.get("/market/rrg")
async def market_rrg(
    engine: MarketResearchEngine = Depends(get_market_research_engine),
) -> Envelope[RRGReport]:
    data, meta = await engine.rrg()
    return Envelope.fresh(
        data, source=meta["source"], stale=meta["stale"], warnings=meta["warnings"]
    )


@router.get("/market/volatility")
async def market_volatility(
    engine: MarketResearchEngine = Depends(get_market_research_engine),
) -> Envelope[VolatilityReport]:
    data, meta = await engine.volatility()
    return Envelope.fresh(
        data, source=meta["source"], stale=meta["stale"], warnings=meta["warnings"]
    )


@router.get("/market/conditions")
async def market_conditions(
    engine: MarketResearchEngine = Depends(get_market_research_engine),
) -> Envelope[ConditionsReport]:
    data, meta = await engine.conditions()
    return Envelope.fresh(
        data, source=meta["source"], stale=meta["stale"], warnings=meta["warnings"]
    )


@router.get("/market/valuation")
async def market_valuation(
    engine: MarketResearchEngine = Depends(get_market_research_engine),
) -> Envelope[ValuationReport]:
    data, meta = await engine.valuation()
    return Envelope.fresh(
        data, source=meta["source"], stale=meta["stale"], warnings=meta["warnings"]
    )


@router.get("/market/sentiment")
async def market_sentiment(
    engine: MarketResearchEngine = Depends(get_market_research_engine),
) -> Envelope[SentimentReport]:
    data, meta = await engine.sentiment()
    return Envelope.fresh(
        data, source=meta["source"], stale=meta["stale"], warnings=meta["warnings"]
    )


@router.get("/market/breadth")
async def market_breadth(
    engine: MarketResearchEngine = Depends(get_market_research_engine),
) -> Envelope[BreadthReport]:
    data, meta = await engine.breadth()
    return Envelope.fresh(
        data, source=meta["source"], stale=meta["stale"], warnings=meta["warnings"]
    )
