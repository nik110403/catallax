"""Marquee ticker watchlist persistence router (Phase 14e).

Backs the editable ticker: the frontend used to hardcode 21 symbols, now it
fetches this list, lets the owner edit it, and saves it back here. Whole-list
replace (not per-row CRUD) matches how the UI edits it (an ordered array) and
avoids ordering races from concurrent partial writes.

The client fetches this over the gated REST API, then passes the resulting
symbols explicitly to `/ws/prices` — that socket is ungated and (as of this
phase) no longer falls back to the persisted watchlist itself (see
`routers/ws.py`'s docstring); this router is what makes the explicit-symbols
flow possible without leaking the watchlist to an unauthenticated caller.

No schema change: `WatchlistItem` (migration 0002) already has symbol/source/
sort_order. Display labels are a frontend concern and never touch this table.
"""
from __future__ import annotations

import re
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.factory import ADAPTERS
from app.config import Settings, get_settings
from app.db import get_db
from app.errors import DemoModeReadOnly, InvalidRequest
from app.models import WatchlistItem

router = APIRouter(prefix="/watchlist", tags=["watchlist"])

# Same bounds as the ungated WS ticker (`routers/ws.py`'s _MAX_SYMBOLS /
# _MAX_SYMBOL_LEN) so a saved watchlist can never itself exceed what
# `?symbols=` on that socket will accept.
_MAX_ITEMS = 50
_MAX_SYMBOL_LEN = 16
# Same char class as research.py's `_SYMBOL` Path pattern: yfinance-style
# tickers (BRK-B, ^GSPC, EURUSD=X, GC=F). Requires at least one alnum
# (rejects pure-punctuation symbols like "^^" or "==") and forbids ".."
# anywhere (security review, 2026-08-15: `..` reaches yfinance_adapter's
# upstream URL path unchanged).
_SYMBOL_RE = re.compile(r"^(?!.*\.\.)(?=.*[A-Za-z0-9])[A-Za-z0-9.\-^=]+$")


class WatchlistItemIn(BaseModel):
    symbol: str
    source: str | None = None


def _row_dict(row: WatchlistItem) -> dict[str, Any]:
    return {"symbol": row.symbol, "source": row.source, "sort_order": row.sort_order}


def _invalid_item(index: int, reason: str) -> InvalidRequest:
    # Security review (2026-08-15): never echo the caller's raw submitted
    # value back in the error body (errors.py:_validation_handler's "strip
    # the submitted input values" convention) — a fixed reason + the
    # rejected item's index is enough to locate it client-side.
    exc = InvalidRequest(f"{reason} at index {index}")
    exc.public_message = f"{reason} at index {index}"
    return exc


def normalize_watchlist(items: list[WatchlistItemIn]) -> list[dict[str, Any]]:
    """Validate + normalize a candidate watchlist. Pure (no DB), shared by the
    PUT handler and unit tests.

    Raises `InvalidRequest` (400) on any violation — a bad payload must never
    reach the DB, where a duplicate symbol would otherwise surface as an
    opaque `IntegrityError`/500 against the table's unique constraint.
    """
    if len(items) > _MAX_ITEMS:
        raise InvalidRequest(f"Watchlist cannot exceed {_MAX_ITEMS} symbols")

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for order, item in enumerate(items):
        symbol = item.symbol.strip().upper()
        if not symbol or len(symbol) > _MAX_SYMBOL_LEN or not _SYMBOL_RE.match(symbol):
            raise _invalid_item(order, "Invalid symbol")
        if symbol in seen:
            raise _invalid_item(order, "Duplicate symbol")
        seen.add(symbol)

        source = item.source.strip().lower() if item.source else None
        if source and source not in ADAPTERS:
            raise _invalid_item(order, "Unknown price source")

        out.append({"symbol": symbol, "source": source, "sort_order": order})
    return out


@router.get("")
async def get_watchlist(
    db: AsyncSession = Depends(get_db), settings: Settings = Depends(get_settings)
) -> list[dict[str, Any]]:
    # DEMO_MODE: the persisted watchlist is the live one — never serve
    # it to a demo visitor (same "empty read" pattern as signals/backtest/lab/algo).
    if settings.demo_empty_reads:
        return []
    rows = (
        (await db.execute(select(WatchlistItem).order_by(WatchlistItem.sort_order)))
        .scalars()
        .all()
    )
    return [_row_dict(r) for r in rows]


@router.put("")
async def put_watchlist(
    items: list[WatchlistItemIn],
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> list[dict[str, Any]]:
    # DEMO_MODE: whole-list replace with no backup — a demo visitor must never
    # be able to wipe the live watchlist (same 403 shape as every other
    # DEMO_MODE-gated mutation; see errors.DemoModeReadOnly).
    if settings.demo_mode:
        raise DemoModeReadOnly()
    normalized = normalize_watchlist(items)
    await db.execute(delete(WatchlistItem))
    db.add_all(WatchlistItem(**row) for row in normalized)
    await db.commit()
    return normalized
