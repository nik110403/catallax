"""Macro & Economic router (Phase 8, OpenBB Path B).

All macro data reaches the UI only through here → `MacroAdapter` → the isolated
`openbb-data` sidecar. No provider keys ever reach the frontend. Responses are
`Envelope[T]` so widgets render provenance/staleness dots alongside the data.

Free sources: US/EU yield curves + treasury rates (federal_reserve/ecb, keyless),
multi-country indicators (econdb, keyless); FRED series + calendar light up with
the free `OPENBB_FRED_API_KEY`.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Path, Query

from app.adapters.envelope import Envelope
from app.adapters.macro_adapter import (
    CalendarData,
    IndicatorBoard,
    MacroAdapter,
    MacroSeries,
    RatesData,
    YieldCurve,
    get_macro_adapter,
)

router = APIRouter(tags=["macro"])

# Series ids are user-influenced (FRED id or `ROOT.country`) — allowlist the
# charset so nothing odd reaches the upstream query string.
_SERIES_ID = Path(pattern=r"^[A-Za-z0-9._]{1,40}$")


@router.get("/macro/series/{series_id}")
async def macro_series(
    series_id: str = _SERIES_ID,
    transform: str = Query("toya", pattern=r"^(toya|level|tpop|index)$"),
    adapter: MacroAdapter = Depends(get_macro_adapter),
) -> Envelope[MacroSeries]:
    return await adapter.series(series_id, transform)


@router.get("/macro/indicators")
async def macro_indicators(
    adapter: MacroAdapter = Depends(get_macro_adapter),
) -> Envelope[IndicatorBoard]:
    return await adapter.indicators()


@router.get("/macro/calendar")
async def macro_calendar(
    adapter: MacroAdapter = Depends(get_macro_adapter),
) -> Envelope[CalendarData]:
    return await adapter.calendar()


@router.get("/macro/yield-curve")
async def macro_yield_curve(
    region: str = Query("US", pattern=r"^(US|EU)$"),
    adapter: MacroAdapter = Depends(get_macro_adapter),
) -> Envelope[YieldCurve]:
    return await adapter.yield_curve(region)


@router.get("/macro/rates")
async def macro_rates(
    adapter: MacroAdapter = Depends(get_macro_adapter),
) -> Envelope[RatesData]:
    return await adapter.rates()
