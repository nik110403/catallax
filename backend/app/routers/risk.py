"""Portfolio Risk router (Phase 8.5).

Derived risk analytics over a Wealthfolio portfolio, COMPUTED from the F0
daily-history backbone — no new data source. Everything reaches the UI only here
→ `RiskEngine`. Responses are `Envelope[T]` so the Risk tab renders
provenance/coverage dots and the (stale) flag when any upstream series degrades.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends
from redis.asyncio import Redis

from app.adapters.envelope import Envelope
from app.config import Settings, get_settings
from app.redis import get_redis
from app.services.risk import (
    FxDragReport,
    RiskEngine,
    RiskXray,
    StressReport,
    VarReport,
)

router = APIRouter(tags=["risk"])


def get_risk_engine(
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> RiskEngine:
    # No DB session injected — risk reads history via session_scope() per series.
    return RiskEngine(redis, settings)


@router.get("/risk/xray")
async def risk_xray(engine: RiskEngine = Depends(get_risk_engine)) -> Envelope[RiskXray]:
    data, meta = await engine.xray()
    return Envelope.fresh(data, source=meta["source"], stale=meta["stale"], warnings=meta["warnings"])


@router.get("/risk/var")
async def risk_var(engine: RiskEngine = Depends(get_risk_engine)) -> Envelope[VarReport]:
    data, meta = await engine.var()
    return Envelope.fresh(data, source=meta["source"], stale=meta["stale"], warnings=meta["warnings"])


@router.get("/risk/fx-drag")
async def risk_fx_drag(engine: RiskEngine = Depends(get_risk_engine)) -> Envelope[FxDragReport]:
    data, meta = await engine.fx_drag()
    return Envelope.fresh(data, source=meta["source"], stale=meta["stale"], warnings=meta["warnings"])


@router.get("/risk/stress")
async def risk_stress(engine: RiskEngine = Depends(get_risk_engine)) -> Envelope[StressReport]:
    # engine.stress() honours the RISK_STRESS_ENABLED flag (returns empty if off).
    return Envelope.fresh(await engine.stress(), source="risk-engine")
