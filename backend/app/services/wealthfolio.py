"""Wealthfolio net-worth bridge.

Net worth is sourced from the Wealthfolio portfolio, NOT the
Beancount ledger: the ledger bundled with Catallax is sample data, whereas
Wealthfolio holds the actual, market-priced, multi-account portfolio. The
Wealthfolio REST API does not expose an absolute net-worth series (it returns
returns-indexed performance, USD for the whole-portfolio TOTAL), so a read-only
exporter sidecar in the wealthfolio stack serves the absolute EUR series
(``SUM(daily_account_valuation.total_value_base)`` per day). This bridge fetches
that series and caches it briefly in Redis.

Everything else (accounts, transactions, currencies, holdings) still comes from
the ledger via `services/ledger.py`; only the headline net worth is Wealthfolio.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal

import httpx
from pydantic import BaseModel
from redis.asyncio import Redis

from app.config import Settings
from app.errors import UpstreamError
from app.services.demo import (
    WF_ASSETS, WF_CASH, WF_INVESTMENTS, WF_TOTAL, wf_accounts, wf_networth_series, wf_position_history,
)

logger = logging.getLogger("catallax.wealthfolio")

_CACHE_KEY = "wealthfolio:networth"
_HOLDINGS_KEY = "wealthfolio:holdings"


def _optional_decimal(v: object) -> Decimal | None:
    if v is None:
        return None
    try:
        return Decimal(str(v))
    except (ValueError, TypeError, InvalidOperation):
        return None


class NetWorthPoint(BaseModel):
    date: date
    value: Decimal
    currency: str
    # Per-day split of `value` — sourced from the exporter (real mode) or a
    # synthetic fixture (demo mode). Optional: older cache entries and
    # malformed upstream points may lack them.
    cash: Decimal | None = None
    investments: Decimal | None = None


class HoldingAsset(BaseModel):
    symbol: str
    name: str
    asset_class: str
    quantity: Decimal
    value: Decimal
    # Per-asset cost basis (SE2/SE4), already scaled by the exporter's own
    # per-account rescale factor so `value - cost_basis` is internally
    # consistent — absent-tolerant: `None` for an old/cached exporter payload
    # that predates SE2, never a hard requirement.
    cost_basis: Decimal | None = None


class PositionHistoryPoint(BaseModel):
    """One aggregated day of a symbol's dated position history (SE3's
    `/positions-history`, `WealthfolioBridge.positions_history()`)."""
    date: date
    symbol: str
    quantity: Decimal
    value: Decimal | None = None
    cost_basis: Decimal | None = None
    # True only for the single trailing forward-filled point this bridge
    # appends when the exporter's series ends before the requested date —
    # never true for a real exporter row.
    stale: bool = False


class HoldingAccount(BaseModel):
    account: str
    value: Decimal
    investments: Decimal
    cash: Decimal


class Holdings(BaseModel):
    base: str
    as_of: str | None = None
    investments: Decimal
    cash: Decimal
    total: Decimal
    by_asset: list[HoldingAsset] = []
    by_account: list[HoldingAccount] = []


class NetWorthComponents(BaseModel):
    investments: Decimal  # Wealthfolio market value of holdings
    wf_cash: Decimal  # cash sitting inside the Wealthfolio accounts
    ledger_cash: Decimal  # liquid bank/wallet cash from the Beancount ledger


class CombinedNetWorth(BaseModel):
    currency: str
    total: Decimal
    # "all" = the full combined net worth (investments + WF cash + ledger bank cash);
    # "investments" = just the market-priced investment holdings.
    scope: Literal["all", "investments"] = "all"
    components: NetWorthComponents
    points: list[NetWorthPoint] = []


class WealthfolioBridge:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings

    @property
    def _base(self) -> str:
        return self._settings.wealthfolio_networth_url.rstrip("/")

    async def networth(self, months: int) -> list[NetWorthPoint]:
        key = f"{_CACHE_KEY}:{months}"
        raw = await self._redis.get(key)
        if raw is not None:
            try:
                return [NetWorthPoint.model_validate(d) for d in json.loads(raw)]
            except Exception as exc:
                logger.debug("Wealthfolio cache deserialise failed for %s: %s", key, exc)
        points = await self._fetch(months)
        try:
            await self._redis.set(
                key,
                json.dumps([p.model_dump(mode="json") for p in points]),
                ex=self._settings.wealthfolio_cache_ttl_seconds,
            )
        except Exception as exc:
            logger.warning("Wealthfolio cache write failed for %s: %s", key, exc)
        return points

    async def _fetch(self, months: int) -> list[NetWorthPoint]:
        url = f"{self._base}/networth"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url, params={"months": months})
                resp.raise_for_status()
                # Parse JSON numbers straight to Decimal to preserve precision.
                body = json.loads(resp.text, parse_float=Decimal)
        except httpx.HTTPError as exc:
            logger.warning("Wealthfolio exporter unreachable: %s (url=%s)", exc, url)
            raise UpstreamError("Net-worth source unavailable") from exc

        if not isinstance(body, dict) or not isinstance(body.get("points"), list):
            logger.warning("Unexpected exporter payload: %s", str(body)[:200])
            raise UpstreamError("Unexpected response from net-worth source")

        base = str(body.get("base") or self._settings.reporting_currency)
        out: list[NetWorthPoint] = []
        for p in body["points"]:
            try:
                out.append(
                    NetWorthPoint(
                        date=date.fromisoformat(str(p["date"])[:10]),
                        value=Decimal(str(p["total_value"])),
                        currency=base,
                        cash=_optional_decimal(p.get("cash")),
                        investments=_optional_decimal(p.get("investments")),
                    )
                )
            except (KeyError, ValueError, TypeError, InvalidOperation) as exc:
                logger.debug("Skipping malformed net-worth point %r: %s", p, exc)
        return out

    # ── holdings (per-asset + per-account) ───────────────────
    async def holdings(self) -> Holdings:
        raw = await self._redis.get(_HOLDINGS_KEY)
        if raw is not None:
            try:
                return Holdings.model_validate_json(raw)
            except Exception as exc:
                logger.debug("Wealthfolio holdings cache deserialise failed: %s", exc)
        h = await self._fetch_holdings()
        try:
            await self._redis.set(
                _HOLDINGS_KEY, h.model_dump_json(),
                ex=self._settings.wealthfolio_cache_ttl_seconds,
            )
        except Exception as exc:
            logger.warning("Wealthfolio holdings cache write failed: %s", exc)
        return h

    async def _fetch_holdings(self) -> Holdings:
        url = f"{self._base}/holdings"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                body = json.loads(resp.text, parse_float=Decimal)
        except httpx.HTTPError as exc:
            logger.warning("Wealthfolio holdings exporter unreachable: %s (url=%s)", exc, url)
            raise UpstreamError("Holdings source unavailable") from exc
        if not isinstance(body, dict) or not isinstance(body.get("by_account"), list):
            logger.warning("Unexpected holdings payload: %s", str(body)[:200])
            raise UpstreamError("Unexpected response from holdings source")

        base = str(body.get("base") or self._settings.reporting_currency)
        totals = body.get("totals") or {}

        def _d(v: object) -> Decimal:
            try:
                return Decimal(str(v))
            except (ValueError, TypeError, InvalidOperation):
                return Decimal(0)

        assets: list[HoldingAsset] = []
        for a in body.get("by_asset") or []:
            try:
                assets.append(HoldingAsset(
                    symbol=str(a["symbol"]),
                    name=str(a.get("name") or a["symbol"]),
                    asset_class=str(a.get("asset_class") or "OTHER"),
                    quantity=_d(a.get("quantity")), value=_d(a.get("value")),
                    # `fallback: true` (I1) means this asset has NO quote at all —
                    # value == cost_basis at the source, so any gain arithmetic
                    # against it is meaningless, not just approximate. Map to
                    # `None` so the advisor's existing absent-tolerant path takes
                    # over (conservative full-value-as-gain), never a real
                    # cost_basis that would silently price a tax-free trim.
                    cost_basis=None if a.get("fallback") else _optional_decimal(a.get("cost_basis")),
                ))
            except (KeyError, TypeError) as exc:
                logger.debug("Skipping malformed holding asset %r: %s", a, exc)
        accounts: list[HoldingAccount] = []
        for a in body.get("by_account") or []:
            try:
                accounts.append(HoldingAccount(
                    account=str(a["account"]), value=_d(a.get("value")),
                    investments=_d(a.get("investments")), cash=_d(a.get("cash")),
                ))
            except (KeyError, TypeError) as exc:
                logger.debug("Skipping malformed holding account %r: %s", a, exc)
        return Holdings(
            base=base,
            as_of=str(body["as_of"]) if body.get("as_of") else None,
            investments=_d(totals.get("investments")), cash=_d(totals.get("cash")),
            total=_d(totals.get("total")), by_asset=assets, by_account=accounts,
        )

    # ── dated positions-history (SE3) ─────────────────────────
    async def positions_history(self, symbol: str, as_of: date | None = None) -> list[PositionHistoryPoint]:
        """One symbol's dated history from the exporter's `/positions-history`
        (SE3), aggregated and cleaned for consumption:

        - **Per-account aggregation**: the exporter returns one row per
          per-account transaction chain, so a symbol held across N accounts
          returns multiple rows on the same date (e.g. a symbol held in two
          accounts returns two rows per date) — rows sharing a date are summed here
          (quantity/value/cost_basis) into one point per date.
        - **Fallback exclusion**: any row with ``fallback: true`` is a
          no-quote cost-priced fallback, never a market valuation — excluded
          from the aggregate entirely (the "never cost-valued" promise), not
          just flagged.
        - **Staleness**: the exporter's series ends at the last snapshot/
          transaction date, not necessarily `as_of` (default: today) — rather
          than fabricate a value for every missing day, exactly ONE trailing
          point is appended at `as_of` carrying the last known aggregate,
          marked `stale=True`, so a caller can see both the real last-data
          date (the point before it) and that the series doesn't reach `as_of`
          on its own."""
        as_of = as_of or date.today()
        url = f"{self._base}/positions-history"
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(url, params={"symbol": symbol})
                resp.raise_for_status()
                body = json.loads(resp.text, parse_float=Decimal)
        except httpx.HTTPError as exc:
            logger.warning("Wealthfolio positions-history exporter unreachable: %s (url=%s)", exc, url)
            raise UpstreamError("Positions history source unavailable") from exc
        if not isinstance(body, list):
            logger.warning("Unexpected positions-history payload: %s", str(body)[:200])
            raise UpstreamError("Unexpected response from positions-history source")

        by_date: dict[date, dict[str, Decimal | None]] = {}
        for row in body:
            if not isinstance(row, dict) or row.get("fallback"):
                continue
            try:
                d = date.fromisoformat(str(row["date"])[:10])
            except (KeyError, ValueError, TypeError):
                continue
            agg = by_date.setdefault(d, {"quantity": Decimal(0), "value": None, "cost_basis": None})
            q = _optional_decimal(row.get("quantity"))
            if q is not None:
                agg["quantity"] = (agg["quantity"] or Decimal(0)) + q
            v = _optional_decimal(row.get("value"))
            if v is not None:
                agg["value"] = (agg["value"] or Decimal(0)) + v
            cb = _optional_decimal(row.get("cost_basis"))
            if cb is not None:
                agg["cost_basis"] = (agg["cost_basis"] or Decimal(0)) + cb

        points = [
            PositionHistoryPoint(
                date=d, symbol=symbol.upper(), quantity=agg["quantity"],
                value=agg["value"], cost_basis=agg["cost_basis"],
            )
            for d, agg in sorted(by_date.items())
        ]
        if points and points[-1].date < as_of:
            last = points[-1]
            points.append(PositionHistoryPoint(
                date=as_of, symbol=last.symbol, quantity=last.quantity,
                value=last.value, cost_basis=last.cost_basis, stale=True,
            ))
        return points

    # ── combined net worth (Wealthfolio + ledger cash) ───────
    async def combined(
        self,
        months: int,
        ledger_cash: Decimal,
        scope: Literal["all", "investments"] = "all",
    ) -> CombinedNetWorth:
        """Net worth, two scopes:

        - ``all`` (default): the full picture = Wealthfolio portfolio + the ledger's
          bank/wallet cash. The WF total already includes WF-side cash;
          ``ledger_cash`` is the liquid cash the ledger tracks (banks) that Wealthfolio
          does not, added to each WF point as a floor.
        - ``investments``: just the market-priced investment holdings — strips both the
          ledger bank cash and the WF-side cash. ``value`` approximates by subtracting the
          *current* WF cash from each WF-total point: the right-hand edge equals
          ``investments`` exactly; earlier points treat WF cash as roughly constant (a
          stated approximation, kept for continuity). Each point's own ``investments``
          field is unaffected by that approximation — it's the exporter's exact per-day
          figure."""
        series = await self.networth(months)
        h = await self.holdings()
        cur = h.base
        if scope == "investments":
            # Strip the (current) WF cash from each WF-total point. Floor at 0: for a
            # very early point where that day's WF total was below today's cash, the
            # constant-cash approximation could otherwise go negative.
            points = [
                NetWorthPoint(
                    date=p.date, value=max(p.value - h.cash, Decimal(0)), currency=cur,
                    investments=p.investments,
                )
                for p in series
            ]
            total = h.investments
        else:
            points = [
                NetWorthPoint(
                    date=p.date, value=p.value + ledger_cash, currency=cur,
                    cash=(p.cash + ledger_cash) if p.cash is not None else None,
                    investments=p.investments,
                )
                for p in series
            ]
            total = h.total + ledger_cash
        return CombinedNetWorth(
            currency=cur,
            total=total,
            scope=scope,
            components=NetWorthComponents(
                investments=h.investments, wf_cash=h.cash, ledger_cash=ledger_cash,
            ),
            points=points,
        )


class DemoWealthfolioBridge(WealthfolioBridge):
    """DEMO_MODE bridge: synthetic fixtures from `services/demo.py`, no network
    or Redis touched.

    `networth()`'s last point equals `holdings().total` exactly (see
    `demo.wf_networth_series`), so the Home hero figure and the Wealthfolio
    Holdings total agree. `combined()` is inherited unchanged — it's built
    entirely from `self.networth()`/`self.holdings()`, both overridden below,
    so normal polymorphism already composes demo data correctly; no separate
    override needed.
    """

    async def networth(self, months: int) -> list[NetWorthPoint]:
        return [
            NetWorthPoint(date=d, value=v, currency="EUR", cash=c, investments=i)
            for d, v, c, i in wf_networth_series(months)
        ]

    async def holdings(self) -> Holdings:
        assets = [
            HoldingAsset(
                symbol=a.symbol, name=a.name, asset_class=a.asset_class,
                quantity=a.quantity, value=a.value, cost_basis=a.cost_basis,
            )
            for a in WF_ASSETS
        ]
        accounts = [HoldingAccount(**row) for row in wf_accounts()]
        return Holdings(
            base="EUR",
            as_of=date.today().isoformat(),
            investments=WF_INVESTMENTS,
            cash=WF_CASH,
            total=WF_TOTAL,
            by_asset=assets,
            by_account=accounts,
        )

    async def positions_history(self, symbol: str, as_of: date | None = None) -> list[PositionHistoryPoint]:
        """Synthetic per-symbol history (SE4 demo bridge) — always fresh
        through today (no staleness in demo), consistent with `WF_ASSETS`'
        current value/cost_basis (the last point pins to them exactly, like
        `wf_networth_series` pins to `WF_TOTAL`). Empty for a symbol the demo
        persona doesn't hold."""
        rows = wf_position_history(symbol, months=36)
        if rows is None:
            return []
        return [
            PositionHistoryPoint(date=d, symbol=symbol.upper(), quantity=q, value=v, cost_basis=cb)
            for d, q, v, cb in rows
        ]


def make_wealthfolio(redis: Redis, settings: Settings) -> WealthfolioBridge:
    """The ONE construction seam for `WealthfolioBridge` — every call site MUST
    go through this (never `WealthfolioBridge(...)` directly), or DEMO_MODE
    leaks live portfolio data. Returns the demo bridge when `settings.demo_mode`."""
    if settings.demo_mode:
        return DemoWealthfolioBridge(redis, settings)
    return WealthfolioBridge(redis, settings)
