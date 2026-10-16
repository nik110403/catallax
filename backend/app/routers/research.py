"""Equity Research router (Phase 10b) — single-name deep dive + screening.

Each endpoint is independent (per-symbol), so there is no shared bundle like the
Market Research tab: fundamentals come pre-cached from the FundamentalsAdapter,
price/technicals from the F0 backbone, scores are pure math. The frontend reaches
all of this only through `/api/v1/research/*`.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query
from redis.asyncio import Redis

from app.adapters.envelope import Envelope
from app.config import Settings, get_settings
from app.redis import get_redis
from app.services.research import (
    FundamentalsReport,
    ResearchEngine,
    ScoresReport,
    ScreenReport,
    TechnicalReport,
    ValuationReport,
)

router = APIRouter(tags=["research"])

# yfinance tickers use letters/digits + . - ^ = (e.g. BRK-B, ^GSPC, VWCE.DE).
# Constrain here so untrusted input can't reach yfinance / Redis keys as junk.
_SYMBOL = Path(..., min_length=1, max_length=24, pattern=r"^[A-Za-z0-9.\-^=]+$")


def get_research_engine(
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> ResearchEngine:
    return ResearchEngine(redis, settings)


def _env(data, meta) -> Envelope:
    return Envelope.fresh(
        data, source=meta["source"], stale=meta["stale"], warnings=meta["warnings"]
    )


@router.get("/research/screen")
async def research_screen(
    engine: ResearchEngine = Depends(get_research_engine),
    min_f_score: float | None = Query(None, ge=0, le=9),
    min_z_score: float | None = Query(None),
    max_m_score: float | None = Query(None),
    max_pe: float | None = Query(None, gt=0),
    min_pe: float | None = Query(None, gt=0),
    max_pb: float | None = Query(None, gt=0),
    max_ps: float | None = Query(None, gt=0),
    min_div_yield: float | None = Query(None, ge=0),
    min_roe: float | None = Query(None),
    min_market_cap: float | None = Query(None, ge=0),
) -> Envelope[ScreenReport]:
    # Only known, typed filter keys reach the engine (no arbitrary dict from client).
    raw = {
        "min_f_score": min_f_score, "min_z_score": min_z_score, "max_m_score": max_m_score,
        "max_pe": max_pe, "min_pe": min_pe, "max_pb": max_pb, "max_ps": max_ps,
        "min_div_yield": min_div_yield, "min_roe": min_roe, "min_market_cap": min_market_cap,
    }
    filters = {k: v for k, v in raw.items() if v is not None}
    data, meta = await engine.screen(filters)
    return _env(data, meta)


@router.get("/research/{symbol}/fundamentals")
async def research_fundamentals(
    symbol: str = _SYMBOL,
    engine: ResearchEngine = Depends(get_research_engine),
) -> Envelope[FundamentalsReport]:
    data, meta = await engine.fundamentals(symbol)
    return _env(data, meta)


@router.get("/research/{symbol}/valuation")
async def research_valuation(
    symbol: str = _SYMBOL,
    engine: ResearchEngine = Depends(get_research_engine),
) -> Envelope[ValuationReport]:
    data, meta = await engine.valuation(symbol)
    return _env(data, meta)


@router.get("/research/{symbol}/scores")
async def research_scores(
    symbol: str = _SYMBOL,
    engine: ResearchEngine = Depends(get_research_engine),
) -> Envelope[ScoresReport]:
    data, meta = await engine.scores(symbol)
    return _env(data, meta)


@router.get("/research/{symbol}/technicals")
async def research_technicals(
    symbol: str = _SYMBOL,
    engine: ResearchEngine = Depends(get_research_engine),
) -> Envelope[TechnicalReport]:
    data, meta = await engine.technicals(symbol)
    return _env(data, meta)
