"""Signal self-scoring router (Phase 9, deliverable #1).

Logs emitted cross-sectional signals and grades them against realised forward
returns COMPUTED from the F0 daily-history backbone — no new data source. The grade
(cross-sectional Rank-IC → ICIR → t, quintile L/S spread, Wilson-LB hit-rate) is
recomputed lazily so it can never go stale against a restated bar. Everything reaches
the UI only here → `SignalEngine`; computed responses are `Envelope[T]` so the
Signals tab renders provenance + the honesty warnings (survivorship, lumpy universe).
"""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, Field
from redis.asyncio import Redis

from app.adapters.envelope import Envelope
from app.config import Settings, get_settings
from app.redis import get_redis
from app.services.signals import (
    CrossSection,
    EmitResult,
    SignalCatalog,
    SignalEngine,
    SignalGrade,
    Scoreboard,
)

router = APIRouter(tags=["signals"])


def get_signal_engine(
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> SignalEngine:
    # No DB session injected — signals read F0 history via session_scope() per series.
    return SignalEngine(redis, settings)


# Allowed character sets — keep ids/symbols out of Redis key segments + DB junk.
_SIGNAL_ID_RE = r"^[A-Za-z0-9_.\-]+$"
_SYMBOL_RE = r"^[A-Za-z0-9_.^=\-]+$"
_SOURCE_RE = r"^[A-Za-z0-9_\-]+$"


class SignalIn(BaseModel):
    """One emitted signal. `symbol` must be F0-priceable via (symbol, source)."""

    signal_id: str = Field(min_length=1, max_length=64, pattern=_SIGNAL_ID_RE)
    as_of_date: date
    symbol: str = Field(min_length=1, max_length=32, pattern=_SYMBOL_RE)
    source: str | None = Field(default=None, max_length=32, pattern=_SOURCE_RE)
    value: float
    meta: dict | None = None


@router.post("/signals")
async def emit_signals(
    payload: SignalIn | list[SignalIn],
    engine: SignalEngine = Depends(get_signal_engine),
) -> Envelope[EmitResult]:
    items = payload if isinstance(payload, list) else [payload]
    result = await engine.emit([i.model_dump() for i in items])
    return Envelope.fresh(result, source="signal-engine")


@router.get("/signals")
async def list_signals(engine: SignalEngine = Depends(get_signal_engine)) -> Envelope[SignalCatalog]:
    return Envelope.fresh(await engine.catalog(), source="signal-engine")


@router.post("/signals/seed")
async def seed_signals(
    refresh: bool = Query(False),
    engine: SignalEngine = Depends(get_signal_engine),
) -> Envelope[EmitResult]:
    # Runs the reference generators (xs_mom_252_21 + xs_lowvol_63) over the WF book.
    # Guarded by SIGNAL_SEED_ENABLED; idempotent (upsert on the unique key).
    return Envelope.fresh(await engine.seed(refresh=refresh), source="signal-engine")


@router.get("/signals/scoreboard")
async def signals_scoreboard(
    horizon: int | None = Query(None, ge=1, le=504),
    quantiles: int | None = Query(None, ge=2, le=10),
    engine: SignalEngine = Depends(get_signal_engine),
) -> Envelope[Scoreboard]:
    board = await engine.scoreboard(horizon, quantiles)
    return Envelope.fresh(board, source="signal-engine", warnings=board.warnings)


@router.get("/signals/{signal_id}/grade")
async def grade_signal(
    signal_id: str = Path(..., max_length=64, pattern=_SIGNAL_ID_RE),
    horizon: int | None = Query(None, ge=1, le=504),
    quantiles: int | None = Query(None, ge=2, le=10),
    refresh: bool = Query(False),
    engine: SignalEngine = Depends(get_signal_engine),
) -> Envelope[SignalGrade]:
    grade, meta = await engine.grade(signal_id, horizon, quantiles, refresh=refresh)
    return Envelope.fresh(grade, source=meta["source"], stale=meta["stale"], warnings=meta["warnings"])


@router.get("/signals/{signal_id}/cross-section")
async def signal_cross_section(
    signal_id: str = Path(..., max_length=64, pattern=_SIGNAL_ID_RE),
    as_of: str = Query(..., description="decision date YYYY-MM-DD"),
    horizon: int | None = Query(None, ge=1, le=504),
    engine: SignalEngine = Depends(get_signal_engine),
) -> Envelope[CrossSection]:
    return Envelope.fresh(await engine.cross_section(signal_id, as_of, horizon), source="signal-engine")
