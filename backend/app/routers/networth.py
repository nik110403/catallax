"""Net-worth router — combined view: Wealthfolio portfolio + the ledger's cash.

The headline net worth = the Wealthfolio portfolio (via the exporter) PLUS the
liquid cash the Beancount ledger tracks that Wealthfolio does not. The
ledger's investment accounts are excluded (they mirror Wealthfolio, so adding
them would double-count). Accounts, transactions and currencies still come from the
ledger; holdings come from Wealthfolio (see `routers/wealthfolio.py`).
"""
from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Query
from redis.asyncio import Redis

from app.config import Settings, get_settings
from app.redis import get_redis
from app.services.ledger import LedgerBridge, make_ledger
from app.services.wealthfolio import CombinedNetWorth, WealthfolioBridge, make_wealthfolio

router = APIRouter(prefix="/networth", tags=["networth"])


def get_wealthfolio(
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> WealthfolioBridge:
    return make_wealthfolio(redis, settings)


def get_ledger(
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> LedgerBridge:
    return make_ledger(redis, settings)


@router.get("")
async def networth(
    months: int = Query(12, ge=1, le=120),
    scope: Literal["all", "investments"] = Query(
        "all",
        description="'all' = full net worth (investments + WF cash + ledger bank cash); "
        "'investments' = market-priced holdings only.",
    ),
    wealthfolio: WealthfolioBridge = Depends(get_wealthfolio),
    ledger: LedgerBridge = Depends(get_ledger),
) -> CombinedNetWorth:
    # 'investments' scope ignores ledger cash, but fetching it is cheap (cached) and
    # keeps the component breakdown populated for the UI chips.
    ledger_cash = await ledger.cash()
    return await wealthfolio.combined(months, ledger_cash, scope)
