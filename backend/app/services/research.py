"""Equity Research engine (Phase 10b) — single-name deep dive + screening.

Pure, reproducible fundamental analytics over the `FundamentalsAdapter`
(yfinance statements) paired with the F0 daily-price backbone for market value
and technicals. The three forensic scores — **Piotroski F-Score**, **Altman
Z-Score**, **Beneish M-Score** — are textbook formulae computed in Python so
every number is auditable (the LLM layer, Phase 12, only narrates these facts).

Design mirrors the Phase 10a research engine:
  * Engine.__init__(redis, settings)        — no DB session; session_scope() per price read
  * statements come pre-cached from the adapter; price from HistoryStore
  * each public method returns (PydanticModel, meta) — the router wraps Envelope
  * screen() fans out over the configured universe with bounded concurrency

Honesty: when a statement line item is missing, the dependent score component is
reported `passed=None` / the ratio `None` (never silently treated as zero), and a
warning is surfaced. Conventions (e.g. ROA against current-year assets, EBITDA ≈
EBIT + depreciation) are documented inline and in `skills/equity-research.md`.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel
from redis.asyncio import Redis

from app.adapters.fundamentals_adapter import FundamentalsAdapter
from app.adapters.yahoo_symbols import crypto_hint
from app.adapters.yfinance_adapter import YFinanceAdapter
from app.config import Settings
from app.db import session_scope
from app.errors import RateLimitExceeded, UpstreamError
from app.services.history import HistoryStore

logger = logging.getLogger("catallax.research")

# Process-wide guard so a cold-cache burst (e.g. a browser auto-refresh) launches the
# full-universe screen fan-out ONCE, not once per concurrent request. The engine is
# Depends-instantiated per request, so the lock must live at module scope.
_SCREEN_LOCK = asyncio.Lock()


# ── numeric helpers ────────────────────────────────────────────
def _f(x: Any, nd: int = 4) -> float | None:
    """JSON-safe rounded float: NaN/inf/None -> None."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v, nd) if math.isfinite(v) else None


def _fc(x: Any, nd: int = 2) -> str:
    """Compact display number for detail strings: 111482000000 -> '111.5B'."""
    v = _f(x)
    if v is None:
        return "n/a"
    a = abs(v)
    for cut, suf in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e5, "K")):
        if a >= cut:
            return f"{v / (cut if suf != 'K' else 1e3):.1f}{suf}"
    return f"{v:.{nd}f}"


def _div(a: Any, b: Any) -> float | None:
    """Safe division: None / zero / non-finite -> None."""
    try:
        an, bn = float(a), float(b)
    except (TypeError, ValueError):
        return None
    if bn == 0 or not (math.isfinite(an) and math.isfinite(bn)):
        return None
    r = an / bn
    return r if math.isfinite(r) else None


def _gt(a: float | None, b: float | None) -> bool | None:
    """Tri-state a > b: None if either operand is missing."""
    if a is None or b is None:
        return None
    return a > b


# ── response models ─────────────────────────────────────────────
class StatementLine(BaseModel):
    key: str
    label: str
    values: list[float | None]  # one per fiscal year (newest first)


class FundamentalsReport(BaseModel):
    symbol: str
    name: str
    sector: str | None
    industry: str | None
    currency: str | None
    fiscal_years: list[str]
    income: list[StatementLine]
    balance: list[StatementLine]
    cashflow: list[StatementLine]
    margins: list[StatementLine]  # derived: gross / operating / net margin, ROE, ROA per year


class ValuationRatio(BaseModel):
    key: str
    label: str
    value: float | None
    unit: str  # "x" | "%" | "ratio"


class ValuationReport(BaseModel):
    symbol: str
    name: str
    currency: str | None
    price: float | None
    market_cap: float | None
    ratios: list[ValuationRatio]


class ScoreComponent(BaseModel):
    key: str
    label: str
    passed: bool | None  # None = input unavailable (not counted)
    detail: str


class ScoreCard(BaseModel):
    key: str  # "piotroski" | "altman" | "beneish"
    label: str
    score: float | None
    scale: str  # human band, e.g. "0–9" or "Z > 2.99 safe"
    verdict: str | None  # "Strong" / "Safe" / "Distress" / "Manipulation risk" ...
    components: list[ScoreComponent]


class ScoresReport(BaseModel):
    symbol: str
    name: str
    fiscal_years: list[str]
    cards: list[ScoreCard]
    warnings: list[str]


class TechPoint(BaseModel):
    date: str
    close: float | None
    sma50: float | None
    sma200: float | None


class TechnicalReport(BaseModel):
    symbol: str
    last: float | None
    sma50: float | None
    sma200: float | None
    dist_sma200_pct: float | None
    high_52w: float | None
    low_52w: float | None
    pct_of_52w_range: float | None
    rsi_14: float | None
    trend: str  # "uptrend" | "downtrend" | "mixed"
    series: list[TechPoint]


class ScreenRow(BaseModel):
    symbol: str
    name: str
    sector: str | None
    price: float | None
    market_cap: float | None
    pe: float | None
    pb: float | None
    ps: float | None
    div_yield: float | None
    roe: float | None
    f_score: float | None
    z_score: float | None
    m_score: float | None


class ScreenReport(BaseModel):
    rows: list[ScreenRow]
    universe_size: int
    filtered: int
    filters: dict[str, float]


_STALE_WARN = "live source returned no data; showing the last good result"


# ── per-year statement access ───────────────────────────────────
def _val(stmt: dict[str, Any], year: str, field: str) -> float | None:
    return (stmt.get(year) or {}).get(field)


# ── forensic scores (pure) ──────────────────────────────────────
def _piotroski(fund: dict[str, Any]) -> ScoreCard:
    """Piotroski F-Score (0–9). Needs the two most recent fiscal years.

    ROA / asset-turnover use current-year total assets (a common, reproducible
    convention vs Piotroski's average-assets original); documented as such."""
    years = fund.get("fiscal_years") or []
    inc, bal, cf = fund.get("income", {}), fund.get("balance", {}), fund.get("cashflow", {})
    comps: list[ScoreComponent] = []
    if len(years) < 2:
        return ScoreCard(key="piotroski", label="Piotroski F-Score", score=None,
                         scale="0–9", verdict=None, components=comps)
    t, p = years[0], years[1]

    ni_t, ni_p = _val(inc, t, "net_income"), _val(inc, p, "net_income")
    ta_t, ta_p = _val(bal, t, "total_assets"), _val(bal, p, "total_assets")
    ocf_t = _val(cf, t, "operating_cash_flow")
    roa_t, roa_p = _div(ni_t, ta_t), _div(ni_p, ta_p)
    ltd_t, ltd_p = _val(bal, t, "long_term_debt"), _val(bal, p, "long_term_debt")
    lev_t, lev_p = _div(ltd_t, ta_t), _div(ltd_p, ta_p)
    cr_t = _div(_val(bal, t, "current_assets"), _val(bal, t, "current_liabilities"))
    cr_p = _div(_val(bal, p, "current_assets"), _val(bal, p, "current_liabilities"))
    sh_t = _val(bal, t, "shares") or _val(inc, t, "diluted_shares")
    sh_p = _val(bal, p, "shares") or _val(inc, p, "diluted_shares")
    gm_t = _div(_val(inc, t, "gross_profit"), _val(inc, t, "revenue"))
    gm_p = _div(_val(inc, p, "gross_profit"), _val(inc, p, "revenue"))
    ato_t = _div(_val(inc, t, "revenue"), ta_t)
    ato_p = _div(_val(inc, p, "revenue"), ta_p)

    def comp(key: str, label: str, passed: bool | None, detail: str) -> None:
        comps.append(ScoreComponent(key=key, label=label, passed=passed, detail=detail))

    comp("roa_pos", "Positive ROA", None if ni_t is None else ni_t > 0,
         f"net income {_fc(ni_t)}")
    comp("cfo_pos", "Positive operating cash flow", None if ocf_t is None else ocf_t > 0,
         f"OCF {_fc(ocf_t)}")
    comp("roa_up", "ROA improving", _gt(roa_t, roa_p),
         f"ROA {_fc(roa_t)} vs {_fc(roa_p)}")
    comp("accrual", "Cash flow > net income (quality)",
         None if (ocf_t is None or ni_t is None) else ocf_t > ni_t,
         f"OCF {_fc(ocf_t)} vs NI {_fc(ni_t)}")
    comp("lev_down", "Leverage decreasing",
         None if (lev_t is None or lev_p is None) else lev_t < lev_p,
         f"LTD/assets {_fc(lev_t)} vs {_fc(lev_p)}")
    comp("curr_up", "Current ratio improving", _gt(cr_t, cr_p),
         f"current ratio {_fc(cr_t)} vs {_fc(cr_p)}")
    comp("no_dilution", "No share dilution",
         None if (sh_t is None or sh_p is None) else sh_t <= sh_p,
         f"shares {_fc(sh_t)} vs {_fc(sh_p)}")
    comp("margin_up", "Gross margin improving", _gt(gm_t, gm_p),
         f"gross margin {_fc(gm_t)} vs {_fc(gm_p)}")
    comp("turn_up", "Asset turnover improving", _gt(ato_t, ato_p),
         f"turnover {_fc(ato_t)} vs {_fc(ato_p)}")

    awarded = sum(1 for c in comps if c.passed is True)
    if any(c.passed is None for c in comps):
        verdict = "Incomplete data"
    elif awarded >= 7:
        verdict = "Strong"
    elif awarded >= 4:
        verdict = "Moderate"
    else:
        verdict = "Weak"
    return ScoreCard(key="piotroski", label="Piotroski F-Score", score=float(awarded),
                     scale="0–9 (higher = stronger fundamentals)", verdict=verdict, components=comps)


def _altman(fund: dict[str, Any], market_cap: float | None) -> ScoreCard:
    """Altman Z-Score (original manufacturing model)."""
    years = fund.get("fiscal_years") or []
    inc, bal = fund.get("income", {}), fund.get("balance", {})
    comps: list[ScoreComponent] = []
    if not years:
        return ScoreCard(key="altman", label="Altman Z-Score", score=None,
                         scale="Z > 2.99 safe", verdict=None, components=comps)
    t = years[0]
    ta = _val(bal, t, "total_assets")
    wc = _val(bal, t, "working_capital")
    if wc is None:
        wc = (lambda a, b: a - b if a is not None and b is not None else None)(
            _val(bal, t, "current_assets"), _val(bal, t, "current_liabilities"))
    re = _val(bal, t, "retained_earnings")
    ebit = _val(inc, t, "ebit")
    tl = _val(bal, t, "total_liabilities")
    sales = _val(inc, t, "revenue")

    x1, x2 = _div(wc, ta), _div(re, ta)
    x3, x4, x5 = _div(ebit, ta), _div(market_cap, tl), _div(sales, ta)
    for key, label, x in (
        ("x1", "Working capital / assets (×1.2)", x1),
        ("x2", "Retained earnings / assets (×1.4)", x2),
        ("x3", "EBIT / assets (×3.3)", x3),
        ("x4", "Market value equity / liabilities (×0.6)", x4),
        ("x5", "Sales / assets (×1.0)", x5),
    ):
        comps.append(ScoreComponent(key=key, label=label, passed=x is not None,
                                    detail=f"{_fc(x)}"))
    if None in (x1, x2, x3, x4, x5):
        return ScoreCard(key="altman", label="Altman Z-Score", score=None,
                         scale="Z > 2.99 safe · 1.81–2.99 grey · < 1.81 distress",
                         verdict="Incomplete data", components=comps)
    z = 1.2 * x1 + 1.4 * x2 + 3.3 * x3 + 0.6 * x4 + 1.0 * x5
    verdict = "Safe" if z > 2.99 else ("Grey zone" if z >= 1.81 else "Distress")
    return ScoreCard(key="altman", label="Altman Z-Score", score=_f(z, 2),
                     scale="Z > 2.99 safe · 1.81–2.99 grey · < 1.81 distress",
                     verdict=verdict, components=comps)


def _beneish(fund: dict[str, Any]) -> ScoreCard:
    """Beneish M-Score (8-variable earnings-manipulation model). Needs t and t-1."""
    years = fund.get("fiscal_years") or []
    inc, bal, cf = fund.get("income", {}), fund.get("balance", {}), fund.get("cashflow", {})
    comps: list[ScoreComponent] = []
    if len(years) < 2:
        return ScoreCard(key="beneish", label="Beneish M-Score", score=None,
                         scale="M > −2.22 = elevated risk", verdict=None, components=comps)
    t, p = years[0], years[1]

    def gm(y: str) -> float | None:  # gross margin
        return _div((lambda s, c: s - c if s is not None and c is not None else None)(
            _val(inc, y, "revenue"), _val(inc, y, "cogs")), _val(inc, y, "revenue"))

    def depr(y: str) -> float | None:
        return _val(inc, y, "depreciation") or _val(cf, y, "dep_amort")

    sales_t, sales_p = _val(inc, t, "revenue"), _val(inc, p, "revenue")
    ta_t, ta_p = _val(bal, t, "total_assets"), _val(bal, p, "total_assets")
    dsri = _div(_div(_val(bal, t, "receivables"), sales_t), _div(_val(bal, p, "receivables"), sales_p))
    gmi = _div(gm(p), gm(t))
    aqi_t = (lambda: (lambda v: 1 - v if v is not None else None)(
        _div((lambda a, b: a + b if a is not None and b is not None else None)(
            _val(bal, t, "current_assets"), _val(bal, t, "net_ppe")), ta_t)))()
    aqi_p = (lambda: (lambda v: 1 - v if v is not None else None)(
        _div((lambda a, b: a + b if a is not None and b is not None else None)(
            _val(bal, p, "current_assets"), _val(bal, p, "net_ppe")), ta_p)))()
    aqi = _div(aqi_t, aqi_p)
    sgi = _div(sales_t, sales_p)
    dep_t, dep_p = depr(t), depr(p)
    ppe_t, ppe_p = _val(bal, t, "net_ppe"), _val(bal, p, "net_ppe")
    depi = _div(
        _div(dep_p, (lambda d, n: d + n if d is not None and n is not None else None)(dep_p, ppe_p)),
        _div(dep_t, (lambda d, n: d + n if d is not None and n is not None else None)(dep_t, ppe_t)),
    )
    sgai = _div(_div(_val(inc, t, "sga"), sales_t), _div(_val(inc, p, "sga"), sales_p))
    lvg_t = _div(_val(bal, t, "total_liabilities"), ta_t)
    lvg_p = _div(_val(bal, p, "total_liabilities"), ta_p)
    lvgi = _div(lvg_t, lvg_p)
    tata = _div((lambda ni, ocf: ni - ocf if ni is not None and ocf is not None else None)(
        _val(inc, t, "net_income"), _val(cf, t, "operating_cash_flow")), ta_t)

    parts = {"DSRI": dsri, "GMI": gmi, "AQI": aqi, "SGI": sgi, "DEPI": depi,
             "SGAI": sgai, "LVGI": lvgi, "TATA": tata}
    for k, v in parts.items():
        comps.append(ScoreComponent(key=k.lower(), label=k, passed=v is not None, detail=f"{_fc(v)}"))
    if any(v is None for v in parts.values()):
        return ScoreCard(key="beneish", label="Beneish M-Score", score=None,
                         scale="M > −2.22 = elevated risk", verdict="Incomplete data", components=comps)
    m = (-4.84 + 0.92 * dsri + 0.528 * gmi + 0.404 * aqi + 0.892 * sgi
         + 0.115 * depi - 0.172 * sgai + 4.679 * tata - 0.327 * lvgi)
    verdict = "Elevated manipulation risk" if m > -2.22 else "Low manipulation risk"
    return ScoreCard(key="beneish", label="Beneish M-Score", score=_f(m, 2),
                     scale="M > −2.22 = elevated earnings-manipulation risk",
                     verdict=verdict, components=comps)


class ResearchEngine:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings
        self._fund = FundamentalsAdapter(redis, settings)

    # ── price from the F0 backbone (own session) ──────────────
    async def _price_series(self, symbol: str, *, asset_class: str | None = None) -> pd.Series | None:
        """`asset_class` is the holding's KNOWN Wealthfolio class (review
        225d5f4 re-review F2): a colliding ticker (e.g. equity `SOL`) must not
        resolve onto the `SOL-USD` coin. The adapter binds the crypto hint
        once at construction — see `YFinanceAdapter.__init__` — so it is the
        only symbol resolution applied; unknown (None) keeps the heuristic."""
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=self._settings.research_history_lookback_days)
        try:
            async with session_scope() as session:
                adapter = YFinanceAdapter(self._redis, self._settings, crypto=crypto_hint(asset_class))
                env = await HistoryStore(session, self._redis, self._settings).get_daily(
                    adapter, symbol, start, end
                )
        except (RateLimitExceeded, UpstreamError) as exc:
            logger.warning("research price fetch TRANSIENT %s: %s", symbol, exc)
            return None
        except Exception as exc:
            logger.info("research price fetch failed %s: %s", symbol, exc)
            return None
        if not env.data:
            return None
        s = pd.Series(
            [float(c.close) for c in env.data],
            index=pd.to_datetime([c.ts.date() for c in env.data]),
        )
        s = s[~s.index.duplicated(keep="last")].sort_index()
        s = s[s > 0]
        return s if not s.empty else None

    async def _market_cap(self, fund: dict[str, Any], price: float | None) -> float | None:
        mc = fund.get("market_cap")
        if mc is not None:
            return mc
        shares = fund.get("shares_outstanding")
        if shares is None:
            years = fund.get("fiscal_years") or []
            if years:
                shares = _val(fund.get("balance", {}), years[0], "shares")
        if shares is None or price is None:
            return None
        return _f(float(shares) * price, 0)

    # ── fundamentals summary ──────────────────────────────────
    async def fundamentals(self, symbol: str, *, asset_class: str | None = None) -> tuple[FundamentalsReport, dict[str, Any]]:
        fund, stale = await self._fund.statements(symbol, asset_class=asset_class)
        years = fund.get("fiscal_years") or []
        inc, bal, cf = fund.get("income", {}), fund.get("balance", {}), fund.get("cashflow", {})

        def lines(stmt: dict[str, Any], spec: list[tuple[str, str]]) -> list[StatementLine]:
            return [
                StatementLine(key=k, label=lbl, values=[_f(_val(stmt, y, k), 0) for y in years])
                for k, lbl in spec
            ]

        income_lines = lines(inc, [
            ("revenue", "Revenue"), ("gross_profit", "Gross Profit"),
            ("operating_income", "Operating Income"), ("ebit", "EBIT"),
            ("net_income", "Net Income"), ("diluted_shares", "Diluted Shares"),
        ])
        balance_lines = lines(bal, [
            ("total_assets", "Total Assets"), ("current_assets", "Current Assets"),
            ("current_liabilities", "Current Liabilities"), ("total_liabilities", "Total Liabilities"),
            ("long_term_debt", "Long-Term Debt"), ("equity", "Shareholders' Equity"),
            ("cash", "Cash & Equivalents"),
        ])
        cashflow_lines = lines(cf, [
            ("operating_cash_flow", "Operating Cash Flow"), ("capex", "Capital Expenditure"),
            ("free_cash_flow", "Free Cash Flow"),
        ])
        # derived per-year margins / returns (as %)
        def pct_line(key: str, label: str, num: str, den_stmt: dict[str, Any], den: str) -> StatementLine:
            # numerator is always an income line (net income / margins); denominator
            # may be a balance line (ROE/ROA) or income (margins).
            vals: list[float | None] = []
            for y in years:
                r = _div(_val(inc, y, num), _val(den_stmt, y, den))
                vals.append(_f(r * 100, 2) if r is not None else None)
            return StatementLine(key=key, label=label, values=vals)

        margins = [
            pct_line("gross_margin", "Gross Margin %", "gross_profit", inc, "revenue"),
            pct_line("operating_margin", "Operating Margin %", "operating_income", inc, "revenue"),
            pct_line("net_margin", "Net Margin %", "net_income", inc, "revenue"),
            pct_line("roe", "Return on Equity %", "net_income", bal, "equity"),
            pct_line("roa", "Return on Assets %", "net_income", bal, "total_assets"),
        ]
        report = FundamentalsReport(
            symbol=fund.get("symbol", symbol.upper()),
            name=fund.get("name", symbol.upper()),
            sector=fund.get("sector"), industry=fund.get("industry"),
            currency=fund.get("currency"), fiscal_years=years,
            income=income_lines, balance=balance_lines, cashflow=cashflow_lines, margins=margins,
        )
        meta = {"source": "yfinance", "stale": stale,
                "warnings": [_STALE_WARN] if stale else ([] if years else ["no fundamental data for this symbol"])}
        return report, meta

    # ── valuation ratios ──────────────────────────────────────
    async def valuation(self, symbol: str, *, asset_class: str | None = None) -> tuple[ValuationReport, dict[str, Any]]:
        fund, stale = await self._fund.statements(symbol, asset_class=asset_class)
        series = await self._price_series(symbol, asset_class=asset_class)
        price = _f(series.iloc[-1], 4) if series is not None else None
        market_cap = await self._market_cap(fund, price)
        years = fund.get("fiscal_years") or []
        inc, bal, cf = fund.get("income", {}), fund.get("balance", {}), fund.get("cashflow", {})
        t = years[0] if years else None

        ratios: list[ValuationRatio] = []
        if t is not None:
            ni, rev = _val(inc, t, "net_income"), _val(inc, t, "revenue")
            equity, ebit = _val(bal, t, "equity"), _val(inc, t, "ebit")
            dep = _val(inc, t, "depreciation") or _val(cf, t, "dep_amort")
            ebitda = (ebit + dep) if (ebit is not None and dep is not None) else None
            ev = None
            if market_cap is not None:
                ev = market_cap + (_val(bal, t, "total_debt") or 0) - (_val(bal, t, "cash") or 0)
            fcf, ta = _val(cf, t, "free_cash_flow"), _val(bal, t, "total_assets")
            div_rate = fund.get("dividend_rate")
            ratios = [
                ValuationRatio(key="pe", label="P/E", value=_f(_div(market_cap, ni), 2), unit="x"),
                ValuationRatio(key="pb", label="P/B", value=_f(_div(market_cap, equity), 2), unit="x"),
                ValuationRatio(key="ps", label="P/S", value=_f(_div(market_cap, rev), 2), unit="x"),
                ValuationRatio(key="ev_ebitda", label="EV/EBITDA", value=_f(_div(ev, ebitda), 2), unit="x"),
                ValuationRatio(key="fcf_yield", label="FCF Yield",
                               value=_f((_div(fcf, market_cap) or 0) * 100, 2) if _div(fcf, market_cap) is not None else None, unit="%"),
                ValuationRatio(key="div_yield", label="Dividend Yield",
                               value=_f((_div(div_rate, price) or 0) * 100, 2) if _div(div_rate, price) is not None else None, unit="%"),
                ValuationRatio(key="roe", label="Return on Equity",
                               value=_f((_div(ni, equity) or 0) * 100, 2) if _div(ni, equity) is not None else None, unit="%"),
                ValuationRatio(key="roa", label="Return on Assets",
                               value=_f((_div(ni, ta) or 0) * 100, 2) if _div(ni, ta) is not None else None, unit="%"),
            ]
        report = ValuationReport(
            symbol=fund.get("symbol", symbol.upper()), name=fund.get("name", symbol.upper()),
            currency=fund.get("currency"), price=price, market_cap=_f(market_cap, 0), ratios=ratios,
        )
        warns = [_STALE_WARN] if stale else ([] if years else ["no fundamental data for this symbol"])
        return report, {"source": "yfinance", "stale": stale, "warnings": warns}

    # ── forensic scores ───────────────────────────────────────
    async def scores(self, symbol: str, *, asset_class: str | None = None) -> tuple[ScoresReport, dict[str, Any]]:
        fund, stale = await self._fund.statements(symbol, asset_class=asset_class)
        series = await self._price_series(symbol, asset_class=asset_class)
        price = float(series.iloc[-1]) if series is not None else None
        market_cap = await self._market_cap(fund, price)
        cards = [_piotroski(fund), _altman(fund, market_cap), _beneish(fund)]
        warnings: list[str] = []
        if stale:
            warnings.append(_STALE_WARN)
        if not (fund.get("fiscal_years") or []):
            warnings.append("no fundamental data for this symbol")
        elif len(fund.get("fiscal_years") or []) < 2:
            warnings.append("only one fiscal year available — F-Score and M-Score need two")
        if any(c.passed is None for card in cards for c in card.components):
            warnings.append("some statement line items were unavailable — affected components shown as n/a")
        report = ScoresReport(
            symbol=fund.get("symbol", symbol.upper()), name=fund.get("name", symbol.upper()),
            fiscal_years=fund.get("fiscal_years") or [], cards=cards, warnings=warnings,
        )
        return report, {"source": "yfinance", "stale": stale, "warnings": warnings}

    # ── technicals (F0 price) ─────────────────────────────────
    async def technicals(self, symbol: str, *, asset_class: str | None = None) -> tuple[TechnicalReport, dict[str, Any]]:
        series = await self._price_series(symbol, asset_class=asset_class)
        if series is None or len(series) < 2:
            empty = TechnicalReport(symbol=symbol.upper(), last=None, sma50=None, sma200=None,
                                    dist_sma200_pct=None, high_52w=None, low_52w=None,
                                    pct_of_52w_range=None, rsi_14=None, trend="mixed", series=[])
            return empty, {"source": "yfinance", "stale": True,
                           "warnings": ["no price history for this symbol"]}
        sma50 = series.rolling(50).mean()
        sma200 = series.rolling(200).mean()
        last = float(series.iloc[-1])
        s50 = _f(sma50.iloc[-1], 4)
        s200 = _f(sma200.iloc[-1], 4)
        win_52w = series.tail(252)
        hi, lo = float(win_52w.max()), float(win_52w.min())
        rng = _f((last - lo) / (hi - lo) * 100, 1) if hi > lo else None
        # Wilder RSI(14)
        delta = series.diff()
        gain = delta.clip(lower=0).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
        loss = (-delta.clip(upper=0)).ewm(alpha=1 / 14, adjust=False, min_periods=14).mean()
        rs = gain / loss.replace(0, np.nan)
        rsi = 100 - 100 / (1 + rs)
        rsi_14 = _f(rsi.iloc[-1], 1)
        if s50 is not None and s200 is not None:
            trend = "uptrend" if (last > s50 > s200) else ("downtrend" if (last < s50 < s200) else "mixed")
        else:
            trend = "mixed"
        # downsample to a sane series length for the chart (every Nth bar)
        n = len(series)
        step = max(1, n // 400)
        pts: list[TechPoint] = []
        for i in range(0, n, step):
            idx = series.index[i]
            pts.append(TechPoint(
                date=pd.Timestamp(idx).date().isoformat(),
                close=_f(series.iloc[i], 4),
                sma50=_f(sma50.iloc[i], 4), sma200=_f(sma200.iloc[i], 4),
            ))
        report = TechnicalReport(
            symbol=symbol.upper(), last=_f(last, 4), sma50=s50, sma200=s200,
            dist_sma200_pct=_f((last / s200 - 1) * 100, 2) if s200 else None,
            high_52w=_f(hi, 4), low_52w=_f(lo, 4), pct_of_52w_range=rng,
            rsi_14=rsi_14, trend=trend, series=pts,
        )
        return report, {"source": "yfinance", "stale": False, "warnings": []}

    async def _read_screen_cache(self, cache_key: str) -> list[ScreenRow] | None:
        cached = await self._redis.get(cache_key)
        if cached is None:
            return None
        try:
            return [ScreenRow.model_validate(r) for r in json.loads(cached)]
        except (json.JSONDecodeError, ValueError, TypeError) as exc:
            # corrupt/edited cache blob — visible (OWASP A09), then recompute
            logger.warning("research screen cache deserialise failed: %s", exc)
            return None

    # ── screen the configured universe ────────────────────────
    async def screen(self, filters: dict[str, float]) -> tuple[ScreenReport, dict[str, Any]]:
        universe = self._settings.research_universe_map[: self._settings.research_screen_max_symbols]
        cache_key = (
            "research:screen:"
            + hashlib.sha256(self._settings.research_universe.encode()).hexdigest()[:12]
            + f":{datetime.now(timezone.utc).date().isoformat()}"
        )
        base_rows = await self._read_screen_cache(cache_key)
        if base_rows is None:
            # Double-checked under the lock: a request that waited may find the cache
            # now warm (the holder just wrote it) and skip the fan-out entirely.
            async with _SCREEN_LOCK:
                base_rows = await self._read_screen_cache(cache_key)
                if base_rows is None:
                    sem = asyncio.Semaphore(self._settings.research_fetch_concurrency)

                    async def one(sym: str, label: str) -> ScreenRow | None:
                        async with sem:
                            try:
                                return await self._screen_row(sym, label)
                            except (UpstreamError, RateLimitExceeded, asyncio.TimeoutError) as exc:
                                logger.info("research screen row transient %s: %s", sym, exc)
                                return None
                            except Exception as exc:
                                # an unexpected error is a bug, not a flaky feed — surface it
                                # at WARNING (with traceback) but still drop just this row.
                                logger.warning("research screen row failed %s: %s", sym, exc, exc_info=True)
                                return None

                    rows = await asyncio.gather(*(one(s, lbl) for s, lbl in universe))
                    base_rows = [r for r in rows if r is not None]
                    try:
                        await self._redis.set(
                            cache_key, json.dumps([r.model_dump() for r in base_rows]),
                            ex=self._settings.research_cache_ttl_seconds,
                        )
                    except Exception as exc:
                        logger.warning("research screen cache set failed: %s", exc)

        filtered = [r for r in base_rows if _passes(r, filters)]
        report = ScreenReport(rows=filtered, universe_size=len(universe),
                              filtered=len(filtered), filters=filters)
        return report, {"source": "yfinance", "stale": False, "warnings": []}

    async def _screen_row(self, symbol: str, label: str) -> ScreenRow:
        fund, _ = await self._fund.statements(symbol)
        series = await self._price_series(symbol)
        price = float(series.iloc[-1]) if series is not None else None
        market_cap = await self._market_cap(fund, price)
        years = fund.get("fiscal_years") or []
        inc, bal = fund.get("income", {}), fund.get("balance", {})
        t = years[0] if years else None
        ni = _val(inc, t, "net_income") if t else None
        rev = _val(inc, t, "revenue") if t else None
        equity = _val(bal, t, "equity") if t else None
        f_card, z_card, m_card = _piotroski(fund), _altman(fund, market_cap), _beneish(fund)
        return ScreenRow(
            symbol=fund.get("symbol", symbol.upper()), name=fund.get("name", label),
            sector=fund.get("sector"), price=_f(price, 2), market_cap=_f(market_cap, 0),
            pe=_f(_div(market_cap, ni), 2), pb=_f(_div(market_cap, equity), 2),
            ps=_f(_div(market_cap, rev), 2),
            div_yield=_f((_div(fund.get("dividend_rate"), price) or 0) * 100, 2)
            if _div(fund.get("dividend_rate"), price) is not None else None,
            roe=_f((_div(ni, equity) or 0) * 100, 2) if _div(ni, equity) is not None else None,
            f_score=f_card.score, z_score=z_card.score, m_score=m_card.score,
        )


# ── screen filter predicates ────────────────────────────────────
# filter key -> (row attribute, comparison) ; min_* = floor, max_* = ceiling
_FILTER_SPEC: dict[str, tuple[str, str]] = {
    "min_f_score": ("f_score", "ge"), "min_z_score": ("z_score", "ge"),
    "max_m_score": ("m_score", "le"), "max_pe": ("pe", "le"), "min_pe": ("pe", "ge"),
    "max_pb": ("pb", "le"), "max_ps": ("ps", "le"),
    "min_div_yield": ("div_yield", "ge"), "min_roe": ("roe", "ge"),
    "min_market_cap": ("market_cap", "ge"),
}


def _passes(row: ScreenRow, filters: dict[str, float]) -> bool:
    for fkey, threshold in filters.items():
        spec = _FILTER_SPEC.get(fkey)
        if spec is None:
            continue
        attr, op = spec
        val = getattr(row, attr, None)
        if val is None:  # a metric we can't evaluate excludes the row from a binding filter
            return False
        if op == "ge" and val < threshold:
            return False
        if op == "le" and val > threshold:
            return False
    return True
