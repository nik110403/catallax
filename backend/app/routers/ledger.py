"""Ledger router — Beancount data via the Fava bridge (`services/ledger.py`)."""
from __future__ import annotations

from datetime import date

from fastapi import APIRouter, Depends, Query
from redis.asyncio import Redis

from app.config import Settings, get_settings
from app.redis import get_redis
from app.services.ledger import (
    AccountNode,
    CurrencyTotal,
    LedgerBridge,
    LedgerTransaction,
    NetWorthPoint,
    make_ledger,
)

router = APIRouter(prefix="/ledger", tags=["ledger"])


def get_bridge(
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> LedgerBridge:
    return make_ledger(redis, settings)


@router.get("/accounts")
async def accounts(bridge: LedgerBridge = Depends(get_bridge)) -> list[AccountNode]:
    return await bridge.accounts()


@router.get("/networth")
async def networth(
    currency: str | None = None,
    interval: str = "month",
    months: int = Query(18, ge=1, le=120),
    bridge: LedgerBridge = Depends(get_bridge),
) -> list[NetWorthPoint]:
    return await bridge.networth(currency or "", months)


@router.get("/transactions")
async def transactions(
    account: str | None = None,
    from_: date | None = Query(None, alias="from"),
    to: date | None = None,
    bridge: LedgerBridge = Depends(get_bridge),
) -> list[LedgerTransaction]:
    return await bridge.transactions(account, from_, to)


@router.get("/currencies")
async def currencies(
    currency: str | None = None,
    bridge: LedgerBridge = Depends(get_bridge),
) -> list[CurrencyTotal]:
    return await bridge.currencies(currency or "")
