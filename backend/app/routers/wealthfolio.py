"""Wealthfolio holdings router — the portfolio, per-asset + per-account.

Holdings come from Wealthfolio (the actual market-priced positions), NOT the Beancount
ledger (which tracks cash/expenses, not investment lots). Served via the read-only
exporter sidecar; see `services/wealthfolio.py`.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from app.routers.networth import get_wealthfolio
from app.services.wealthfolio import Holdings, WealthfolioBridge

router = APIRouter(prefix="/wealthfolio", tags=["wealthfolio"])


@router.get("/holdings")
async def holdings(
    bridge: WealthfolioBridge = Depends(get_wealthfolio),
) -> Holdings:
    return await bridge.holdings()
