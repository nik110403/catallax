"""Portfolio engine — holdings, allocation, and P&L from the Beancount ledger.

Value / cost / P&L are computed by Beancount itself (BQL `value()` / `cost()`
at the ledger's price directives, `convert()`-ed to the reporting currency), so
they are always internally consistent. Live quotes from the price adapters are
layered on as a best-effort `live_*` enrichment per holding — they never change
the authoritative ledger valuation.

Ghostfolio is intentionally not used: the bundled container holds no portfolio
data, whereas the ledger already records every lot with cost basis.
"""
from __future__ import annotations

import asyncio
import logging
import re
from decimal import Decimal

from pydantic import BaseModel
from redis.asyncio import Redis

from app.adapters.factory import build_adapter
from app.config import Settings
from app.errors import InvalidRequest
from app.services.ledger import LedgerBridge, make_ledger

logger = logging.getLogger("catallax.portfolio")

_CCY_RE = re.compile(r"^[A-Z][A-Z0-9._-]{1,23}$")


def _ccy(c: str) -> str:
    # Config-sourced currencies are interpolated into BQL — validate defensively.
    if not _CCY_RE.fullmatch(c):
        raise InvalidRequest(f"Invalid currency in config: {c!r}")
    return c


def _dec(v: object) -> Decimal:
    return v if isinstance(v, Decimal) else Decimal(str(v))


def _inv_get(inv: object, ccy: str) -> Decimal:
    return _dec(inv.get(ccy, 0)) if isinstance(inv, dict) else Decimal(0)


class Holding(BaseModel):
    symbol: str
    asset_class: str
    quantity: Decimal
    cost: Decimal  # reporting currency
    value: Decimal  # reporting currency, at ledger prices
    pnl: Decimal
    pnl_pct: float
    weight: float  # % of holdings value
    currency: str
    live_price: Decimal | None = None
    live_currency: str | None = None
    live_source: str | None = None


class AllocationSlice(BaseModel):
    key: str
    value: Decimal
    weight: float


class Allocation(BaseModel):
    currency: str
    total: Decimal
    by_class: list[AllocationSlice]
    by_symbol: list[AllocationSlice]


class PortfolioSummary(BaseModel):
    currency: str
    holdings_value: Decimal
    cash_value: Decimal
    total_value: Decimal
    total_cost: Decimal
    total_pnl: Decimal
    total_pnl_pct: float


class PortfolioService:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings
        self._bridge: LedgerBridge = make_ledger(redis, settings)

    @property
    def _rc(self) -> str:
        return _ccy(self._settings.reporting_currency.upper())

    def _cash_currencies(self) -> list[str]:
        return [_ccy(c) for c in self._settings.cash_currency_list]

    async def holdings(self, enrich: bool = True) -> list[Holding]:
        rc = self._rc
        exclusion = " AND ".join(f"currency != '{c}'" for c in self._cash_currencies()) or "1 = 1"
        bql = (
            f"SELECT currency, sum(number) AS qty, "
            f"convert(cost(sum(position)), '{rc}') AS cost, "
            f"convert(value(sum(position)), '{rc}') AS val "
            f"WHERE account ~ '^Assets:' AND {exclusion} "
            f"GROUP BY currency ORDER BY currency"
        )
        rows = await self._bridge.query_rows(bql)
        cmap = self._settings.portfolio_commodity_map

        parsed: list[tuple[str, Decimal, Decimal, Decimal, dict[str, str | None]]] = []
        for row in rows:
            sym = str(row[0]).upper()
            qty = _dec(row[1]) if len(row) > 1 and row[1] is not None else Decimal(0)
            cost = _inv_get(row[2], rc) if len(row) > 2 else Decimal(0)
            val = _inv_get(row[3], rc) if len(row) > 3 else Decimal(0)
            parsed.append((sym, qty, cost, val, cmap.get(sym, {})))

        total_val = sum((v for _, _, _, v, _ in parsed), Decimal(0))
        holdings = [
            Holding(
                symbol=sym,
                asset_class=str(meta.get("asset_class") or "other"),
                quantity=qty,
                cost=cost,
                value=val,
                pnl=val - cost,
                pnl_pct=round(float((val - cost) / cost * 100), 2) if cost else 0.0,
                weight=round(float(val / total_val * 100), 2) if total_val else 0.0,
                currency=rc,
            )
            for sym, qty, cost, val, meta in parsed
        ]
        if enrich:
            await self._enrich(holdings, cmap)
        return holdings

    async def _enrich(
        self, holdings: list[Holding], cmap: dict[str, dict[str, str | None]]
    ) -> None:
        async def one(h: Holding) -> None:
            meta = cmap.get(h.symbol)
            if not meta or not meta.get("source") or not meta.get("symbol"):
                return
            try:
                adapter = build_adapter(str(meta["source"]), self._redis, self._settings)
                quote = await adapter.get_quote(str(meta["symbol"]))
                h.live_price = quote.price
                h.live_currency = quote.currency
                h.live_source = quote.source
            except Exception as exc:
                # Live quote is best-effort; ledger valuation stands on its own.
                logger.debug("Live quote enrichment failed for %s: %s", h.symbol, exc)

        await asyncio.gather(*(one(h) for h in holdings))

    async def _cash(self) -> Decimal:
        rc = self._rc
        inclusion = " OR ".join(f"currency = '{c}'" for c in self._cash_currencies()) or "1 = 0"
        bql = (
            f"SELECT convert(sum(position), '{rc}') AS v "
            f"WHERE account ~ '^Assets:' AND ({inclusion})"
        )
        rows = await self._bridge.query_rows(bql)
        if rows and rows[0]:
            return _inv_get(rows[0][0], rc)
        return Decimal(0)

    async def allocation(self) -> Allocation:
        rc = self._rc
        holdings = await self.holdings(enrich=False)
        total = sum((h.value for h in holdings), Decimal(0))

        by_class_map: dict[str, Decimal] = {}
        by_symbol: list[AllocationSlice] = []
        for h in holdings:
            by_class_map[h.asset_class] = by_class_map.get(h.asset_class, Decimal(0)) + h.value
            by_symbol.append(
                AllocationSlice(
                    key=h.symbol,
                    value=h.value,
                    weight=round(float(h.value / total * 100), 2) if total else 0.0,
                )
            )
        by_class = [
            AllocationSlice(
                key=k,
                value=v,
                weight=round(float(v / total * 100), 2) if total else 0.0,
            )
            for k, v in sorted(by_class_map.items(), key=lambda kv: kv[1], reverse=True)
        ]
        return Allocation(currency=rc, total=total, by_class=by_class, by_symbol=by_symbol)

    async def summary(self) -> PortfolioSummary:
        rc = self._rc
        holdings = await self.holdings(enrich=False)
        hv = sum((h.value for h in holdings), Decimal(0))
        hc = sum((h.cost for h in holdings), Decimal(0))
        cash = await self._cash()
        pnl = hv - hc
        return PortfolioSummary(
            currency=rc,
            holdings_value=hv,
            cash_value=cash,
            total_value=hv + cash,
            total_cost=hc,
            total_pnl=pnl,
            total_pnl_pct=round(float(pnl / hc * 100), 2) if hc else 0.0,
        )
