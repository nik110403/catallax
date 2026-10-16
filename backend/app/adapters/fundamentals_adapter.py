"""FundamentalsAdapter (Phase 10b) — single-name financial statements via yfinance.

Equity fundamentals (income statement / balance sheet / cash-flow + a light
profile) for the Research tab's deep-dive + screening. yfinance is already a
backend dependency (the F0 price backbone uses it); this adds *no new dep* and
no image rebuild — the lean alternative to mounting OpenBB's `equity` router,
which calls the same yfinance underneath (decided 2026-06-26, see STRATEGY 10b).

Behind the adapter boundary like every other source: the engine
(`services/research.py`) reaches it only here, the UI only through
`/api/v1/research/*`. yfinance's `Ticker` calls are blocking, so they run in a
thread; the normalised result is Redis-cached with a long-lived `:stale`
fallback (statements update quarterly — a stale sheet is fine through an outage).

Normalisation: yfinance labels its statement rows with human strings that drift
between tickers, so each canonical field is resolved from an ordered alias list
(first present wins). Values are kept in the listing currency; the engine pairs
them with an F0 price in the same currency (so Altman's market-value term is
self-consistent).
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from typing import Any

import pandas as pd
from fastapi import Depends
from redis.asyncio import Redis

from app.adapters.yahoo_symbols import crypto_hint, is_crypto_symbol, to_yahoo_symbol
from app.config import Settings, get_settings
from app.redis import get_redis

logger = logging.getLogger("catallax.fundamentals")

_STALE_TTL = 30 * 86400  # statements move quarterly; a month-old sheet still beats nothing
_MAX_YEARS = 5  # keep the most recent N fiscal years (Beneish/Piotroski need t and t-1)

# canonical field -> ordered yfinance row aliases (first present wins)
_INCOME_FIELDS: dict[str, tuple[str, ...]] = {
    "revenue": ("Total Revenue", "Operating Revenue"),
    "cogs": ("Cost Of Revenue", "Reconciled Cost Of Revenue"),
    "gross_profit": ("Gross Profit",),
    "operating_income": ("Operating Income", "Total Operating Income As Reported"),
    "ebit": ("EBIT", "Operating Income"),
    "pretax_income": ("Pretax Income",),
    "net_income": ("Net Income", "Net Income Common Stockholders"),
    "sga": ("Selling General And Administration", "Operating Expense"),
    "diluted_shares": ("Diluted Average Shares", "Basic Average Shares"),
    "depreciation": ("Reconciled Depreciation",),
}
_BALANCE_FIELDS: dict[str, tuple[str, ...]] = {
    "total_assets": ("Total Assets",),
    "current_assets": ("Current Assets",),
    "current_liabilities": ("Current Liabilities",),
    "total_liabilities": ("Total Liabilities Net Minority Interest",),
    "retained_earnings": ("Retained Earnings",),
    "working_capital": ("Working Capital",),
    "long_term_debt": ("Long Term Debt", "Long Term Debt And Capital Lease Obligation"),
    "total_debt": ("Total Debt",),
    "equity": ("Stockholders Equity", "Common Stock Equity", "Total Equity Gross Minority Interest"),
    "receivables": ("Receivables", "Accounts Receivable"),
    "net_ppe": ("Net PPE",),
    "cash": ("Cash And Cash Equivalents", "Cash Cash Equivalents And Short Term Investments"),
    "shares": ("Ordinary Shares Number", "Share Issued"),
}
_CASHFLOW_FIELDS: dict[str, tuple[str, ...]] = {
    "operating_cash_flow": ("Operating Cash Flow", "Cash Flow From Continuing Operating Activities"),
    "free_cash_flow": ("Free Cash Flow",),
    "capex": ("Capital Expenditure",),
    "dep_amort": ("Depreciation And Amortization", "Depreciation Amortization Depletion"),
}


def _num(v: Any) -> float | None:
    """JSON-safe float: NaN/inf/None -> None."""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _extract(df: pd.DataFrame | None, fields: dict[str, tuple[str, ...]]) -> dict[str, dict[str, float | None]]:
    """yfinance statement frame (rows=line items, cols=fiscal-period Timestamps)
    → {fiscal_year_iso: {canonical_field: value}} for the most recent N years."""
    out: dict[str, dict[str, float | None]] = {}
    if df is None or df.empty:
        return out
    index = {str(i): i for i in df.index}
    # columns are period-end Timestamps; newest first, capped
    cols = sorted(df.columns, reverse=True)[:_MAX_YEARS]
    for col in cols:
        try:
            year_key = pd.Timestamp(col).date().isoformat()
        except Exception:
            year_key = str(col)[:10]
        row: dict[str, float | None] = {}
        for canon, aliases in fields.items():
            val: float | None = None
            for alias in aliases:
                idx = index.get(alias)
                if idx is not None:
                    val = _num(df.at[idx, col])
                    if val is not None:
                        break
            row[canon] = val
        out[year_key] = row
    return out


def _not_applicable(symbol: str) -> dict[str, Any]:
    """Empty-but-honest statements payload for a crypto holding."""
    return {
        "symbol": symbol, "name": symbol, "sector": None, "industry": None, "currency": None,
        "market_cap": None, "shares_outstanding": None, "trailing_eps": None, "dividend_rate": None,
        "fiscal_years": [], "income": {}, "balance": {}, "cashflow": {},
        "not_applicable": "crypto asset — no equity financial statements exist",
    }


class FundamentalsAdapter:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self.redis = redis
        self.settings = settings
        self.ttl = settings.fundamentals_cache_ttl_seconds

    async def statements(self, symbol: str, *, asset_class: str | None = None) -> tuple[dict[str, Any], bool]:
        """Return ({profile + income/balance/cashflow by fiscal year}, stale).
        `asset_class` is the holding's Wealthfolio class when the caller knows
        it (the report pack does): it decides the crypto skip and the Yahoo
        ticker; unknown (None/blank) falls back to the ticker heuristic."""
        symbol = symbol.upper().strip()
        if is_crypto_symbol(symbol, self.settings, asset_class=asset_class):
            # Coins have no income statement / balance sheet: skip cleanly with
            # NO Yahoo call (a raw bare-ticker `yf.Ticker(...).info` can 404).
            return _not_applicable(symbol), False
        key = f"fundamentals:{symbol}"
        raw = await self.redis.get(key)
        if raw is not None:
            try:
                return json.loads(raw), False
            except (json.JSONDecodeError, ValueError) as exc:
                # a corrupt/edited cache blob must not be silent (OWASP A09) — rebuild
                logger.warning("fundamentals cache deserialise failed (%s): %s", symbol, exc)
        try:
            # yfinance attribute reads are blocking HTTP with no internal timeout; bound
            # the wait so a stalled Yahoo connection can't pin the screen() semaphore slot.
            data = await asyncio.wait_for(
                asyncio.to_thread(self._build, symbol, crypto_hint(asset_class)),
                timeout=self.settings.fundamentals_fetch_timeout_seconds,
            )
        except Exception as exc:  # includes asyncio.TimeoutError from wait_for
            logger.warning("fundamentals build failed for %s: %s", symbol, exc)
            data = None
        if data and (data.get("income") or data.get("balance")):
            try:
                payload = json.dumps(data)
                await self.redis.set(key, payload, ex=self.ttl)
                await self.redis.set(f"{key}:stale", payload, ex=_STALE_TTL)
            except Exception as exc:
                logger.warning("fundamentals cache set failed (%s): %s", symbol, exc)
            return data, False
        stale = await self.redis.get(f"{key}:stale")
        if stale is not None:
            try:
                return json.loads(stale), True
            except (json.JSONDecodeError, ValueError) as exc:
                logger.warning("fundamentals stale deserialise failed (%s): %s", symbol, exc)
        return {}, True

    def _build(self, symbol: str, crypto: bool | None = None) -> dict[str, Any]:
        import yfinance as yf  # local import: keeps the module import light + mirrors yfinance adapter

        # shared mapping: SAP -> SAP.DE; a known equity never becomes `<SYM>-USD`
        t = yf.Ticker(to_yahoo_symbol(symbol, self.settings, crypto=crypto))
        income = _extract(getattr(t, "income_stmt", None), _INCOME_FIELDS)
        balance = _extract(getattr(t, "balance_sheet", None), _BALANCE_FIELDS)
        cashflow = _extract(getattr(t, "cashflow", None), _CASHFLOW_FIELDS)
        # union of fiscal years across statements, newest first
        years = sorted({*income, *balance, *cashflow}, reverse=True)[:_MAX_YEARS]

        info: dict[str, Any] = {}
        try:
            info = t.info or {}
        except Exception as exc:  # .info occasionally 404s/throttles; statements still useful
            logger.debug("fundamentals .info failed (%s): %s", symbol, exc)

        return {
            "symbol": symbol,
            "name": info.get("longName") or info.get("shortName") or symbol,
            "sector": info.get("sector"),
            "industry": info.get("industry"),
            "currency": info.get("currency") or info.get("financialCurrency"),
            "market_cap": _num(info.get("marketCap")),
            "shares_outstanding": _num(info.get("sharesOutstanding")),
            "trailing_eps": _num(info.get("trailingEps")),
            "dividend_rate": _num(info.get("dividendRate")),
            "fiscal_years": years,
            "income": income,
            "balance": balance,
            "cashflow": cashflow,
        }


def get_fundamentals_adapter(
    redis: Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> FundamentalsAdapter:
    return FundamentalsAdapter(redis, settings)
