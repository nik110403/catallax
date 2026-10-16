"""Broker adapter contract — the ABC + the paper (F2-backed) broker.

Kept minimal (YAGNI): the only cross-venue surface the paper engine needs is a
mark-to-market quote source and a liveness check. Order/position/fill logic is
NOT here — it lives in `services/algo.py` (Task 3), which is what actually
simulates the paper book against these marks.
"""
from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod

from app.adapters.factory import get_price_adapter
from app.config import Settings
from app.redis import get_redis

logger = logging.getLogger("catallax.broker")


class BrokerAdapter(ABC):
    """One broker (paper or, eventually, live). `mark` fetches last-price marks
    for a symbol set; `health` reports basic liveness for status widgets."""

    name: str = "base"
    available: bool = False

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @abstractmethod
    async def mark(self, symbols: list[str]) -> dict[str, float]:
        """Return {symbol: last_price} for as many symbols as could be quoted.
        A per-symbol failure is skipped (logged WARNING), never raised —
        one bad symbol must not take down the whole mark pass."""
        raise NotImplementedError

    @abstractmethod
    async def health(self) -> bool:
        """Basic liveness check for status widgets."""
        raise NotImplementedError


class PaperBroker(BrokerAdapter):
    """Paper-trading fill simulator's quote source: wraps the existing F2
    fallback price chain (`get_price_adapter`) rather than a real venue."""

    name = "paper"
    available = True

    async def mark(self, symbols: list[str]) -> dict[str, float]:
        adapter = get_price_adapter(source=None, redis=get_redis(), settings=self._settings)

        async def _one(symbol: str) -> tuple[str, float | None]:
            try:
                quote = await adapter.get_quote(symbol)
                return symbol, float(quote.price)
            except Exception as exc:
                logger.warning("PaperBroker.mark: failed to quote %s: %s", symbol, exc)
                return symbol, None

        results = await asyncio.gather(*(_one(s) for s in symbols))
        return {symbol: price for symbol, price in results if price is not None}

    async def health(self) -> bool:
        return True
