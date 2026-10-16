"""Market Research engine (Phase 10a) — top-down / cross-asset analytics.

Seven report slices all share a single Redis-cached bundle so the fan-out of
~40 ETF history fetches + 15 FRED series happens once per UTC day (6h TTL).

Data sources:
  * ETF / index price history  → F0 yfinance backbone (HistoryStore)
  * FRED macro series          → MacroAdapter.series() (already Redis-cached 6h)

Design mirrors risk.py exactly:
  * Engine.__init__(redis, settings)     — no DB session; session_scope() per read
  * Engine.bundle()                      — cache-first; TRANSIENT degrades gracefully
  * Engine._gather()                     — asyncio.Semaphore + asyncio.gather
  * _compute(…)                          — pure; runs in asyncio.to_thread
  * _f() / _series()                     — verbatim copies from risk.py
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from datetime import date as date_type
from datetime import datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel
from redis.asyncio import Redis

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.adapters.breadth_adapter import BreadthAdapter
from app.adapters.factory import build_adapter
from app.adapters.macro_adapter import MacroAdapter
from app.adapters.sentiment_adapter import SentimentAdapter
from app.adapters.shiller_adapter import ShillerAdapter
from app.models import MarketBreadthRow
from app.config import Settings
from app.db import session_scope
from app.errors import RateLimitExceeded, UpstreamError
from app.services.history import HistoryStore

logger = logging.getLogger("catallax.market_research")


# ── helpers (verbatim from risk.py) ───────────────────────────
def _f(x: Any, nd: int = 6) -> float | None:
    """JSON-safe float: NaN/inf -> None (never serialise a NaN)."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v):
        return None
    return round(v, nd)


def _series(raw: object) -> pd.Series | None:
    """Convert the (dates, closes, stale) tuple returned by HistoryStore to a Series."""
    if not isinstance(raw, (tuple, list)) or not raw or not raw[0]:
        return None
    s = pd.Series(raw[1], index=pd.to_datetime(raw[0]))
    s = s[~s.index.duplicated(keep="last")].sort_index()
    s = s[s > 0]
    return s if not s.empty else None


# ── response models ────────────────────────────────────────────

class RegimeComponent(BaseModel):
    key: str
    label: str
    value: float | None
    score: float | None
    weight: float
    contribution: float | None


class RegimeHistPoint(BaseModel):
    date: str
    score: float | None


class RegimeReport(BaseModel):
    as_of: str
    state: str  # "risk-on" | "neutral" | "risk-off"
    score: float | None
    components: list[RegimeComponent]
    history: list[RegimeHistPoint]
    recession_flag: bool
    sahm: float | None


class CrossAssetSnapshot(BaseModel):
    symbol: str
    label: str
    last: float | None
    chg_1d_pct: float | None
    chg_1w_pct: float | None
    chg_1m_pct: float | None
    chg_ytd_pct: float | None


class CorrHistPoint(BaseModel):
    date: str
    value: float | None


class RatioItem(BaseModel):
    key: str
    label: str
    value: float | None
    chg_1m_pct: float | None


class CrossAssetReport(BaseModel):
    as_of: str
    window_days: int
    symbols: list[str]
    snapshot: list[CrossAssetSnapshot]
    matrix: list[list[float | None]]
    avg_pairwise_corr: float | None
    avg_corr_history: list[CorrHistPoint]
    ratios: list[RatioItem]


class RotationRow(BaseModel):
    symbol: str
    label: str
    group: str  # "sector" | "factor"
    ret_1d: float | None
    ret_1w: float | None
    ret_1m: float | None
    ret_3m: float | None
    ret_6m: float | None
    ret_ytd: float | None
    rs_vs_bench_3m: float | None


class RotationReport(BaseModel):
    as_of: str
    benchmark: str
    rows: list[RotationRow]


class RRGTailPoint(BaseModel):
    date: str
    rs_ratio: float | None
    rs_momentum: float | None


class RRGSeries(BaseModel):
    symbol: str
    label: str
    group: str
    quadrant: str
    tail: list[RRGTailPoint]


class RRGReport(BaseModel):
    as_of: str
    benchmark: str
    tail_weeks: int
    series: list[RRGSeries]


class VolatilityReport(BaseModel):
    as_of: str
    vix: float | None
    vix_percentile: float | None
    vix3m: float | None
    term_ratio: float | None
    term_state: str
    realized_vol_pct: float | None
    implied_minus_realized: float | None
    move: float | None
    vix_history: list[dict[str, Any]]


class ConditionsTile(BaseModel):
    key: str
    label: str
    value: float | None
    unit: str
    zscore: float | None
    percentile: float | None
    trend: str  # "up" | "down" | "flat"
    date: str | None
    higher_is_risk_off: bool


class ConditionsReport(BaseModel):
    as_of: str
    tiles: list[ConditionsTile]


class ValuationMetric(BaseModel):
    key: str
    label: str
    value: float | None
    percentile: float | None
    note: str
    needs_source: bool


class ValuationReport(BaseModel):
    as_of: str
    metrics: list[ValuationMetric]


class SentimentComponent(BaseModel):
    key: str
    label: str
    score: float | None  # 0-100 (CNN sub-indicator)
    rating: str | None


class FearGreedHistPoint(BaseModel):
    date: str
    score: float | None


class CotRow(BaseModel):
    code: str
    label: str
    net: float | None          # non-commercial (large-spec) net contracts
    oi: float | None
    pct_oi: float | None        # net as % of open interest
    z_1y: float | None          # z-score of net vs trailing ~52 weekly reports
    percentile: float | None    # percentile of net within available history
    change_1w: float | None
    inverse: bool               # underlying inverse to risk appetite (e.g. VIX) → flip colors
    report_date: str | None


class SentimentReport(BaseModel):
    as_of: str
    # CNN Fear & Greed
    fg_score: float | None
    fg_rating: str | None
    fg_prev_close: float | None
    fg_prev_week: float | None
    fg_prev_month: float | None
    fg_prev_year: float | None
    fg_components: list[SentimentComponent]
    fg_history: list[FearGreedHistPoint]
    # CFTC Commitments of Traders
    cot_as_of: str | None
    cot: list[CotRow]


class BreadthMarket(BaseModel):
    market: str
    issues: int | None
    advances: int | None
    declines: int | None
    unchanged: int | None
    breadth_pct: float | None     # advances / (advances + declines) * 100
    ad_ratio: float | None
    new_highs: int | None
    new_lows: int | None
    net_highs: int | None         # new_highs − new_lows
    updown_vol_ratio: float | None
    trin: float | None


class BreadthHistPoint(BaseModel):
    date: str
    value: float | None


class BreadthReport(BaseModel):
    as_of: str
    session_date: str | None
    primary_market: str
    markets: list[BreadthMarket]
    # Primary-market oscillators, accumulated forward from the persisted snapshots.
    history_days: int
    warming_up: bool              # < ~40 sessions persisted → McClellan not yet reliable
    ad_line: list[BreadthHistPoint]            # cumulative net advances
    mcclellan_osc: float | None
    mcclellan_summation: float | None
    mcclellan_history: list[BreadthHistPoint]


# ── engine ─────────────────────────────────────────────────────

class MarketResearchEngine:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings
        self._macro = MacroAdapter(redis, settings)
        self._sentiment = SentimentAdapter(redis, settings)
        self._shiller = ShillerAdapter(redis, settings)
        self._breadth = BreadthAdapter(redis, settings)

    # ── per-ETF/index fetch (own session, bounded concurrency) ─
    async def _fetch_one(
        self, symbol: str, start: datetime, end: datetime
    ) -> tuple[list[str], list[float], bool]:
        async with session_scope() as session:
            adapter = build_adapter("yfinance", self._redis, self._settings)
            env = await HistoryStore(session, self._redis, self._settings).get_daily(
                adapter, symbol, start, end
            )
        return (
            [c.ts.date().isoformat() for c in env.data],
            [float(c.close) for c in env.data],
            bool(env.stale),
        )

    async def _gather(
        self, symbols: list[str], start: datetime, end: datetime
    ) -> dict[str, tuple[list[str], list[float], bool] | str | None]:
        sem = asyncio.Semaphore(self._settings.market_research_fetch_concurrency)

        async def one(sym: str):
            async with sem:
                try:
                    return sym, await self._fetch_one(sym, start, end)
                except (RateLimitExceeded, UpstreamError) as exc:
                    logger.warning("market_research TRANSIENT fetch %s: %s", sym, exc)
                    return sym, "TRANSIENT"
                except Exception as exc:
                    logger.info("market_research fetch failed %s: %s", sym, exc)
                    return sym, None

        return dict(await asyncio.gather(*(one(s) for s in symbols)))

    # ── market breadth: WSJ diary → persist today's snapshot → read series ──
    async def persist_breadth_snapshot(self) -> None:
        """Fetch + persist the diary without computing the report (scheduler entry)."""
        await self._breadth_diary_and_history()

    async def _breadth_diary_and_history(self) -> tuple[dict[str, Any], list[dict[str, Any]], bool]:
        diary, stale = await self._breadth.diary()
        primary = self._settings.market_research_breadth_primary
        sdate = diary.get("session_date")
        markets = diary.get("markets") or {}
        cols = ("issues", "advances", "declines", "unchanged", "new_highs",
                "new_lows", "adv_volume", "decl_volume", "total_volume", "trin")
        # Upsert today's snapshot per market — idempotent on (session date, market).
        if sdate and markets:
            try:
                sd = date_type.fromisoformat(sdate)
                for mkt, f in markets.items():
                    # One session per market so a single bad row can't roll back the rest.
                    try:
                        vals = {c: f.get(c) for c in cols}
                        stmt = pg_insert(MarketBreadthRow).values(
                            breadth_date=sd, market=mkt, **vals
                        ).on_conflict_do_update(
                            constraint="uq_market_breadth_date_market", set_=vals
                        )
                        async with session_scope() as session:
                            await session.execute(stmt)
                            await session.commit()
                    except Exception as exc:
                        logger.warning("market breadth upsert failed for %s: %s", mkt, exc)
            except Exception as exc:
                logger.warning("market breadth upsert setup failed: %s", exc)
            # Backfill the diary's previous-session values into a one-day gap.
            # The diary carries no date for them, so we label the prior weekday —
            # and only when NO row exists in the preceding 4 days: if one does,
            # the previous session is already captured (or the "gap" is a market
            # holiday, where a guessed date would duplicate an existing session).
            prev_markets = diary.get("prev_markets") or {}
            if prev_markets:
                try:
                    sd = date_type.fromisoformat(sdate)
                    pd_ = sd - timedelta(days=1)
                    while pd_.weekday() >= 5:
                        pd_ -= timedelta(days=1)
                    async with session_scope() as session:
                        have = set(
                            (
                                await session.execute(
                                    select(MarketBreadthRow.market).where(
                                        MarketBreadthRow.breadth_date >= sd - timedelta(days=4),
                                        MarketBreadthRow.breadth_date < sd,
                                    )
                                )
                            ).scalars().all()
                        )
                    for mkt, f in prev_markets.items():
                        if mkt in have:
                            continue
                        try:
                            stmt = pg_insert(MarketBreadthRow).values(
                                breadth_date=pd_, market=mkt,
                                **{c: f.get(c) for c in cols},
                            ).on_conflict_do_nothing(
                                constraint="uq_market_breadth_date_market"
                            )
                            async with session_scope() as session:
                                await session.execute(stmt)
                                await session.commit()
                        except Exception as exc:
                            logger.warning("market breadth backfill failed for %s: %s", mkt, exc)
                except Exception as exc:
                    logger.warning("market breadth backfill setup failed: %s", exc)
        # Read the primary-market accumulated series for the oscillators.
        history: list[dict[str, Any]] = []
        try:
            cutoff = (
                datetime.now(timezone.utc)
                - timedelta(days=self._settings.market_research_breadth_history_days)
            ).date()
            async with session_scope() as session:
                rows = (
                    await session.execute(
                        select(MarketBreadthRow)
                        .where(
                            MarketBreadthRow.market == primary,
                            MarketBreadthRow.breadth_date >= cutoff,
                        )
                        .order_by(MarketBreadthRow.breadth_date)
                    )
                ).scalars().all()
            history = [
                {"date": r.breadth_date.isoformat(), "advances": r.advances,
                 "declines": r.declines, "new_highs": r.new_highs, "new_lows": r.new_lows}
                for r in rows
            ]
        except Exception as exc:
            logger.warning("market breadth history read failed: %s", exc)
        return diary, history, stale

    # ── the cached bundle ───────────────────────────────────────
    async def bundle(self, refresh: bool = False) -> dict[str, Any]:
        s = self._settings
        # Cache key: digest of the config + UTC date so a symbol-list change invalidates.
        cfg_sig = json.dumps({
            "cross": s.market_research_cross_asset,
            "sectors": s.market_research_sectors,
            "factors": s.market_research_factors,
            "fred": s.market_research_fred_series,
            "vol": s.market_research_vol_symbols,
            "bench": s.market_research_benchmark,
            "lookback": s.market_research_lookback_days,
            "weights": s.market_research_regime_weights,
            "cot": s.market_research_cot_markets,
            "breadth": s.market_research_breadth_primary,
        }, sort_keys=True)
        cfg_digest = hashlib.sha256(cfg_sig.encode()).hexdigest()[:16]
        key = f"market_research:bundle:{cfg_digest}:{datetime.now(timezone.utc).date().isoformat()}"

        if not refresh:
            cached = await self._redis.get(key)
            if cached is not None:
                try:
                    return json.loads(cached)
                except Exception as exc:
                    logger.debug("market_research bundle cache deserialise failed: %s", exc)

        end = datetime.now(timezone.utc)
        start = end - timedelta(days=s.market_research_lookback_days)

        # ── build symbol universe (deduplicated, order preserved) ──
        ca_map = s.market_research_cross_asset_map
        sec_map = s.market_research_sectors_map
        fac_map = s.market_research_factors_map
        bench = s.market_research_benchmark

        # all ETF symbols (dedup while preserving first-seen order)
        seen: set[str] = set()
        etf_syms: list[str] = []
        for sym, _ in [*ca_map, *sec_map, *fac_map, (bench, bench)]:
            if sym not in seen:
                seen.add(sym)
                etf_syms.append(sym)

        # vol symbols fetched separately (they may not be in the history store cleanly)
        vol_syms = s.market_research_vol_list

        # fan-out: ETFs + vol symbols concurrently
        all_price_syms = list(dict.fromkeys(etf_syms + vol_syms))  # dedup, order preserved
        price_series = await self._gather(all_price_syms, start, end)

        # fan-out: FRED series concurrently via MacroAdapter (each is already Redis-cached)
        fred_ids = s.market_research_fred_list

        async def fetch_fred(sid: str):
            try:
                env = await self._macro.series(sid)
                return sid, env
            except Exception as exc:
                logger.warning("market_research FRED %s failed: %s", sid, exc)
                return sid, None

        fred_results = dict(await asyncio.gather(*(fetch_fred(sid) for sid in fred_ids)))

        # ── external sentiment / valuation sources (each Redis-cached in its adapter) ──
        async def fetch_external() -> dict[str, Any]:
            async def safe(coro):
                try:
                    return await coro
                except Exception as exc:  # adapter already guards, but never sink the bundle
                    logger.warning("market_research external fetch failed: %s", exc)
                    return {}, True
            (fg, fg_stale), (cot, cot_stale), (cape, cape_stale) = await asyncio.gather(
                safe(self._sentiment.fear_greed()),
                safe(self._sentiment.cot()),
                safe(self._shiller.cape()),
            )
            return {
                "fear_greed": fg, "cot": cot, "shiller": cape,
                "stale": bool(fg_stale or cot_stale or cape_stale),
            }

        external = await fetch_external()

        # Market breadth: persist today's WSJ snapshot + read the accumulated series.
        breadth_diary, breadth_history, breadth_stale = await self._breadth_diary_and_history()

        params = {
            "external": external,
            "breadth_diary": breadth_diary,
            "breadth_history": breadth_history,
            "breadth_stale": breadth_stale,
            "breadth_primary": s.market_research_breadth_primary,
            "bench": bench,
            "ca_map": ca_map,
            "sec_map": sec_map,
            "fac_map": fac_map,
            "lookback_days": s.market_research_lookback_days,
            "corr_window": s.market_research_corr_window,
            "vol_pct_window": s.market_research_vol_percentile_window,
            "rrg_smooth": s.market_research_rrg_smooth,
            "rrg_norm_weeks": s.market_research_rrg_norm_weeks,
            "rrg_tail_weeks": s.market_research_rrg_tail_weeks,
            "regime_weights": s.market_research_regime_weight_map,
            "vol_syms": vol_syms,
        }

        result = await asyncio.to_thread(
            _compute, price_series, fred_results, params
        )

        ttl = s.market_research_cache_ttl_seconds
        if result.get("meta", {}).get("transient"):
            ttl = min(ttl, 300)
        try:
            await self._redis.set(key, json.dumps(result), ex=ttl)
        except Exception as exc:
            logger.warning("market_research bundle cache write failed: %s", exc)
        return result

    # ── public slice methods (one per report) ─────────────────
    async def regime(self) -> tuple[RegimeReport, dict[str, Any]]:
        b = await self.bundle()
        return RegimeReport.model_validate(b["regime"]), b["meta"]

    async def cross_asset(self) -> tuple[CrossAssetReport, dict[str, Any]]:
        b = await self.bundle()
        return CrossAssetReport.model_validate(b["cross_asset"]), b["meta"]

    async def rotation(self) -> tuple[RotationReport, dict[str, Any]]:
        b = await self.bundle()
        return RotationReport.model_validate(b["rotation"]), b["meta"]

    async def rrg(self) -> tuple[RRGReport, dict[str, Any]]:
        b = await self.bundle()
        return RRGReport.model_validate(b["rrg"]), b["meta"]

    async def volatility(self) -> tuple[VolatilityReport, dict[str, Any]]:
        b = await self.bundle()
        return VolatilityReport.model_validate(b["volatility"]), b["meta"]

    async def conditions(self) -> tuple[ConditionsReport, dict[str, Any]]:
        b = await self.bundle()
        return ConditionsReport.model_validate(b["conditions"]), b["meta"]

    async def valuation(self) -> tuple[ValuationReport, dict[str, Any]]:
        b = await self.bundle()
        return ValuationReport.model_validate(b["valuation"]), b["meta"]

    async def sentiment(self) -> tuple[SentimentReport, dict[str, Any]]:
        b = await self.bundle()
        return SentimentReport.model_validate(b["sentiment"]), b["meta"]

    async def breadth(self) -> tuple[BreadthReport, dict[str, Any]]:
        b = await self.bundle()
        return BreadthReport.model_validate(b["breadth"]), b["meta"]


# ── pure compute (thread) ──────────────────────────────────────

def _pct_ret(s: pd.Series, n: int) -> float | None:
    """n-bar percent return from the tail of s."""
    s = s.dropna()
    if len(s) < n + 1:
        return None
    return _f((s.iloc[-1] / s.iloc[-(n + 1)] - 1.0) * 100)


def _ytd_ret(s: pd.Series) -> float | None:
    """Return since the first trading bar of the current calendar year."""
    s = s.dropna()
    if s.empty:
        return None
    year_start = s.index[s.index.year == s.index[-1].year]
    if year_start.empty:
        return None
    first_bar = s.loc[year_start[0]]
    last_bar = s.iloc[-1]
    return _f((last_bar / first_bar - 1.0) * 100)


def _percentile_rank(series: pd.Series, value: float) -> float | None:
    """Percentile of `value` within the series (0-100)."""
    arr = series.dropna().to_numpy()
    if len(arr) == 0:
        return None
    return _f(float((arr < value).mean() * 100))


def _rolling_percentile(series: pd.Series, window: int) -> pd.Series:
    """For each point compute percentile rank of that value within the trailing window."""
    def _prank(x: np.ndarray) -> float:
        if len(x) < 2:
            return float("nan")
        return float((x[:-1] < x[-1]).mean() * 100)
    return series.rolling(window, min_periods=max(2, window // 4)).apply(_prank, raw=True)


def _zscore_scalar(series: pd.Series, value: float) -> float | None:
    arr = series.dropna().to_numpy()
    if len(arr) < 2:
        return None
    mu, sd = arr.mean(), arr.std(ddof=1)
    if sd <= 0:
        return None
    return _f((value - mu) / sd)


def _rolling_zscore(series: pd.Series, window: int) -> pd.Series:
    mu = series.rolling(window, min_periods=max(2, window // 4)).mean()
    sd = series.rolling(window, min_periods=max(2, window // 4)).std(ddof=1)
    z = (series - mu) / sd
    return z


def _upper_triangle_mean(corr_matrix: np.ndarray) -> float | None:
    n = corr_matrix.shape[0]
    if n < 2:
        return None
    vals = [corr_matrix[i, j] for i in range(n) for j in range(i + 1, n)
            if math.isfinite(corr_matrix[i, j])]
    return _f(float(np.mean(vals))) if vals else None


def _compute(
    price_series: dict[str, Any],
    fred_results: dict[str, Any],
    params: dict[str, Any],
) -> dict[str, Any]:
    now = datetime.now(timezone.utc).isoformat()
    warnings: list[str] = []
    stale_any = False
    transient_any = False

    bench: str = params["bench"]
    ca_map: list[tuple[str, str]] = params["ca_map"]
    sec_map: list[tuple[str, str]] = params["sec_map"]
    fac_map: list[tuple[str, str]] = params["fac_map"]
    lookback_days: int = params["lookback_days"]
    corr_window: int = params["corr_window"]
    vol_pct_window: int = params["vol_pct_window"]
    rrg_smooth: int = params["rrg_smooth"]
    rrg_norm_weeks: int = params["rrg_norm_weeks"]
    rrg_tail_weeks: int = params["rrg_tail_weeks"]
    regime_weights: dict[str, float] = params["regime_weights"]
    vol_syms: list[str] = params["vol_syms"]
    external: dict[str, Any] = params.get("external", {})
    if external.get("stale"):
        stale_any = True
    breadth_diary: dict[str, Any] = params.get("breadth_diary", {})
    breadth_history: list[dict[str, Any]] = params.get("breadth_history", [])
    breadth_primary: str = params.get("breadth_primary", "NYSE")
    if params.get("breadth_stale"):
        stale_any = True

    # ── helper: get a price Series from the raw fetch result ──
    def get_px(sym: str) -> pd.Series | None:
        raw = price_series.get(sym)
        if raw == "TRANSIENT":
            nonlocal transient_any
            transient_any = True
            warnings.append(f"TRANSIENT fetch for {sym}")
            return None
        if isinstance(raw, (tuple, list)) and len(raw) > 2 and raw[2]:
            nonlocal stale_any
            stale_any = True
        s = _series(raw)
        if s is None:
            warnings.append(f"no price data for {sym}")
        return s

    # ── helper: get a FRED series points → pd.Series(date index, float values) ──
    def get_fred(sid: str) -> pd.Series | None:
        env = fred_results.get(sid)
        if env is None:
            warnings.append(f"FRED {sid} unavailable")
            return None
        # env is an Envelope[MacroSeries]; .data.points is list[MacroPoint]
        try:
            pts = env.data.points
        except AttributeError:
            warnings.append(f"FRED {sid} malformed envelope")
            return None
        if not pts:
            warnings.append(f"FRED {sid} returned no points")
            return None
        dates = pd.to_datetime([p.date for p in pts])
        vals = [float(p.value) for p in pts]
        s = pd.Series(vals, index=dates).sort_index()
        s = s[~s.index.duplicated(keep="last")]
        return s

    bench_px = get_px(bench)

    # ── (1) Regime ─────────────────────────────────────────────
    regime_data = _compute_regime(
        bench_px, ca_map, sec_map, get_px, get_fred, regime_weights,
        lookback_days, vol_pct_window, now, warnings,
    )

    # ── (2) Cross-asset ────────────────────────────────────────
    cross_asset_data = _compute_cross_asset(
        ca_map, bench_px, get_px, lookback_days, corr_window, now, warnings,
    )

    # ── (3) Rotation ───────────────────────────────────────────
    rotation_data = _compute_rotation(
        sec_map, fac_map, bench_px, get_px, bench, now, warnings,
    )

    # ── (4) RRG ────────────────────────────────────────────────
    rrg_data = _compute_rrg(
        sec_map, fac_map, bench_px, get_px, bench, rrg_smooth, rrg_norm_weeks, rrg_tail_weeks, now, warnings,
    )

    # ── (5) Volatility ─────────────────────────────────────────
    volatility_data = _compute_volatility(
        bench_px, get_px, get_fred, vol_syms, vol_pct_window, now, warnings,
    )

    # ── (6) Conditions ─────────────────────────────────────────
    conditions_data = _compute_conditions(get_fred, now, warnings)

    # ── (7) Valuation ──────────────────────────────────────────
    valuation_data = _compute_valuation(get_fred, external.get("shiller") or {}, now, warnings)

    # ── (8) Sentiment / positioning ────────────────────────────
    sentiment_data = _compute_sentiment(
        external.get("fear_greed") or {}, external.get("cot") or {}, now, warnings,
    )

    # ── (9) Market breadth ─────────────────────────────────────
    breadth_data = _compute_breadth(
        breadth_diary, breadth_history, breadth_primary, now, warnings,
    )

    return {
        "regime": regime_data,
        "cross_asset": cross_asset_data,
        "rotation": rotation_data,
        "rrg": rrg_data,
        "volatility": volatility_data,
        "conditions": conditions_data,
        "valuation": valuation_data,
        "sentiment": sentiment_data,
        "breadth": breadth_data,
        "meta": {
            "stale": stale_any or transient_any,
            "transient": transient_any,
            "warnings": warnings,
            "source": "market-research",
        },
    }


# ── regime ────────────────────────────────────────────────────

def _compute_regime(
    bench_px: pd.Series | None,
    ca_map: list[tuple[str, str]],
    sec_map: list[tuple[str, str]],
    get_px,
    get_fred,
    regime_weights: dict[str, float],
    lookback_days: int,
    vol_pct_window: int,
    now: str,
    warnings: list[str],
) -> dict[str, Any]:
    components: list[dict[str, Any]] = []
    total_w = sum(regime_weights.values()) or 1.0

    # --- trend: SPY / SMA200 ---
    trend_score = trend_val = None
    if bench_px is not None and len(bench_px) >= 200:
        sma200 = bench_px.rolling(200, min_periods=200).mean()
        last_sma = float(sma200.iloc[-1]) if not pd.isna(sma200.iloc[-1]) else None
        last_px = float(bench_px.iloc[-1])
        if last_sma and last_sma > 0:
            trend_val = (last_px / last_sma - 1.0)
            trend_score = float(np.clip(trend_val / 0.05, -1.0, 1.0))

    # --- breadth: % of sectors above SMA200 ---
    breadth_score = breadth_val = None
    above_flags: list[float] = []
    for sym, _ in sec_map:
        px = get_px(sym)
        if px is not None and len(px) >= 200:
            sma200 = px.rolling(200, min_periods=200).mean()
            last_sma = float(sma200.iloc[-1]) if not pd.isna(sma200.iloc[-1]) else None
            last = float(px.iloc[-1])
            if last_sma and last_sma > 0:
                above_flags.append(1.0 if last > last_sma else 0.0)
    if above_flags:
        breadth_val = float(np.mean(above_flags))
        breadth_score = float((breadth_val - 0.5) * 2.0)

    # --- vol: negative percentile rank of VIX (VIXCLS) ---
    vol_score = vol_val = None
    vixcls = get_fred("VIXCLS")
    if vixcls is not None and len(vixcls) >= 2:
        tail = vixcls.iloc[-vol_pct_window:]
        latest_vix = float(vixcls.iloc[-1])
        prank = _percentile_rank(tail, latest_vix)
        if prank is not None:
            vol_val = latest_vix
            vol_score = float(-((prank / 100.0 - 0.5) * 2.0))

    # --- credit: z-score of HY-OAS (BAMLH0A0HYM2) ---
    credit_score = credit_val = None
    hy_oas = get_fred("BAMLH0A0HYM2")
    if hy_oas is not None and len(hy_oas) >= 10:
        latest = float(hy_oas.iloc[-1])
        z = _zscore_scalar(hy_oas, latest)
        if z is not None:
            credit_val = latest
            credit_score = float(np.clip(-z / 2.0, -1.0, 1.0))

    # --- curve: T10Y2Y clipped ---
    curve_score = curve_val = None
    t10y2y = get_fred("T10Y2Y")
    if t10y2y is not None and not t10y2y.empty:
        latest = float(t10y2y.iloc[-1])
        curve_val = latest
        curve_score = float(np.clip(latest / 1.0, -1.0, 1.0))

    # --- momentum: SPY/TLT ratio 60-day pct change ---
    momentum_score = momentum_val = None
    tlt_px = None
    for sym, _ in ca_map:
        if sym == "TLT":
            tlt_px = get_px(sym)
            break
    if bench_px is not None and tlt_px is not None:
        common = bench_px.index.intersection(tlt_px.index)
        if len(common) >= 61:
            spy_al = bench_px.reindex(common)
            tlt_al = tlt_px.reindex(common)
            ratio = spy_al / tlt_al
            last_ratio = float(ratio.iloc[-1])
            ratio_60d = float(ratio.iloc[-61])
            momentum_val = last_ratio
            pct = (last_ratio / ratio_60d - 1.0) if ratio_60d != 0 else 0.0
            momentum_score = float(np.clip(pct / 0.1, -1.0, 1.0))

    comp_map = [
        ("trend", "Trend", trend_val, trend_score),
        ("breadth", "Breadth", breadth_val, breadth_score),
        ("vol", "Volatility", vol_val, vol_score),
        ("credit", "Credit", credit_val, credit_score),
        ("curve", "Yield Curve", curve_val, curve_score),
        ("momentum", "Momentum", momentum_val, momentum_score),
    ]

    composite = 0.0
    composite_denom = 0.0
    for key, label, val, score in comp_map:
        w = regime_weights.get(key, 0.0)
        contrib = _f(w * (score or 0.0) / total_w) if score is not None else None
        components.append({
            "key": key, "label": label,
            "value": _f(val), "score": _f(score),
            "weight": _f(w) or 0.0, "contribution": contrib,
        })
        if score is not None:
            composite += w * score
            composite_denom += w

    score_val = _f(composite / composite_denom) if composite_denom > 0 else None

    if score_val is not None:
        if score_val > 0.33:
            state = "risk-on"
        elif score_val < -0.33:
            state = "risk-off"
        else:
            state = "neutral"
    else:
        state = "neutral"

    # --- history: vectorized composite over last ~252 trading days ---
    history: list[dict[str, Any]] = []
    _bench = bench_px
    _hy = hy_oas
    _t10y2y_s = t10y2y
    _vixcls = vixcls

    if _bench is not None and len(_bench) >= 252:
        # Use bench index as the master calendar (trim to last 252 bars)
        hist_idx = _bench.index[-252:]

        # trend series
        sma200_s = _bench.rolling(200, min_periods=200).mean()
        trend_s = (_bench / sma200_s - 1.0).clip(-1.0, 1.0)
        trend_s = trend_s.reindex(hist_idx)

        # breadth series: rolling % sectors > SMA200 on each date
        sector_above: list[pd.Series] = []
        for sym, _ in sec_map:
            px = get_px(sym)
            if px is not None and len(px) >= 200:
                sma = px.rolling(200, min_periods=200).mean()
                ab = (px > sma).astype(float).reindex(hist_idx).ffill(limit=5)
                sector_above.append(ab)
        if sector_above:
            breadth_s = pd.concat(sector_above, axis=1).mean(axis=1)
            breadth_s = (breadth_s - 0.5) * 2.0
        else:
            breadth_s = pd.Series(dtype=float, index=hist_idx)

        # vol series: rolling percentile of VIXCLS
        if _vixcls is not None and len(_vixcls) >= vol_pct_window:
            vix_al = _vixcls.reindex(hist_idx).ffill(limit=10)
            vol_s = _rolling_percentile(vix_al, vol_pct_window)
            vol_s = -((vol_s / 100.0 - 0.5) * 2.0)
        else:
            vol_s = pd.Series(dtype=float, index=hist_idx)

        # credit series: rolling z of HY-OAS
        if _hy is not None and len(_hy) >= 10:
            hy_al = _hy.reindex(hist_idx).ffill(limit=10)
            credit_s = _rolling_zscore(hy_al, min(len(hy_al.dropna()), lookback_days))
            credit_s = credit_s.clip(-2.0, 2.0) / 2.0 * -1.0
        else:
            credit_s = pd.Series(dtype=float, index=hist_idx)

        # curve series: T10Y2Y clipped
        if _t10y2y_s is not None and not _t10y2y_s.empty:
            curve_s = _t10y2y_s.reindex(hist_idx).ffill(limit=10).clip(-1.0, 1.0)
        else:
            curve_s = pd.Series(dtype=float, index=hist_idx)

        # momentum series: rolling 60d pct change of SPY/TLT ratio
        if tlt_px is not None:
            common = _bench.index.intersection(tlt_px.index)
            if len(common) >= 61:
                spy_al = _bench.reindex(common)
                tlt_al = tlt_px.reindex(common)
                ratio_s = (spy_al / tlt_al)
                mom_s = ratio_s.pct_change(60).clip(-1.0, 1.0)
                mom_s = (mom_s / 0.1).clip(-1.0, 1.0)
                mom_s = mom_s.reindex(hist_idx)
            else:
                mom_s = pd.Series(dtype=float, index=hist_idx)
        else:
            mom_s = pd.Series(dtype=float, index=hist_idx)

        comp_series = {
            "trend": (trend_s, regime_weights.get("trend", 0.0)),
            "breadth": (breadth_s, regime_weights.get("breadth", 0.0)),
            "vol": (vol_s, regime_weights.get("vol", 0.0)),
            "credit": (credit_s, regime_weights.get("credit", 0.0)),
            "curve": (curve_s, regime_weights.get("curve", 0.0)),
            "momentum": (mom_s, regime_weights.get("momentum", 0.0)),
        }
        composite_hist = pd.Series(0.0, index=hist_idx)
        weight_hist = pd.Series(0.0, index=hist_idx)
        for (s_series, w) in comp_series.values():
            s_al = s_series.reindex(hist_idx)
            valid_mask = s_al.notna()
            composite_hist[valid_mask] += w * s_al[valid_mask].fillna(0.0)
            weight_hist[valid_mask] += w
        composite_hist = composite_hist / weight_hist.replace(0, float("nan"))

        for dt, sc in composite_hist.items():
            history.append({"date": dt.date().isoformat(), "score": _f(sc)})

    # --- recession_flag and sahm ---
    recession_flag = False
    usrec = get_fred("USREC")
    if usrec is not None and not usrec.empty:
        recession_flag = bool(float(usrec.iloc[-1]) == 1.0)

    sahm_val = None
    sahm = get_fred("SAHMREALTIME")
    if sahm is not None and not sahm.empty:
        sahm_val = _f(float(sahm.iloc[-1]))

    return {
        "as_of": now,
        "state": state,
        "score": score_val,
        "components": components,
        "history": history,
        "recession_flag": recession_flag,
        "sahm": sahm_val,
    }


# ── cross-asset ────────────────────────────────────────────────

def _compute_cross_asset(
    ca_map: list[tuple[str, str]],
    bench_px: pd.Series | None,
    get_px,
    lookback_days: int,
    corr_window: int,
    now: str,
    warnings: list[str],
) -> dict[str, Any]:
    symbols = [sym for sym, _ in ca_map]
    label_map = dict(ca_map)

    snapshot: list[dict[str, Any]] = []
    px_series: dict[str, pd.Series] = {}

    for sym in symbols:
        px = get_px(sym)
        snap: dict[str, Any] = {
            "symbol": sym, "label": label_map.get(sym, sym),
            "last": None, "chg_1d_pct": None, "chg_1w_pct": None,
            "chg_1m_pct": None, "chg_ytd_pct": None,
        }
        if px is not None:
            px_series[sym] = px
            snap["last"] = _f(float(px.iloc[-1]))
            snap["chg_1d_pct"] = _pct_ret(px, 1)
            snap["chg_1w_pct"] = _pct_ret(px, 5)
            snap["chg_1m_pct"] = _pct_ret(px, 21)
            snap["chg_ytd_pct"] = _ytd_ret(px)
        snapshot.append(snap)

    # correlation matrix over all available symbols
    if len(px_series) >= 2:
        ret_df = pd.DataFrame({s: p.pct_change() for s, p in px_series.items()}).dropna(how="all")
        corr_mat = ret_df.corr(min_periods=30)
        matrix: list[list[float | None]] = []
        for s1 in symbols:
            row: list[float | None] = []
            for s2 in symbols:
                if s1 in corr_mat.columns and s2 in corr_mat.index:
                    v = corr_mat.loc[s2, s1]
                    row.append(_f(v))
                else:
                    row.append(None)
            matrix.append(row)

        # avg pairwise corr (trailing corr_window)
        tail_ret = ret_df.iloc[-corr_window:].dropna(how="all")
        if len(tail_ret) >= 10:
            tail_corr = tail_ret.corr(min_periods=10)
            n = len(symbols)
            arr = np.full((n, n), float("nan"))
            for i, s1 in enumerate(symbols):
                for j, s2 in enumerate(symbols):
                    if s1 in tail_corr.columns and s2 in tail_corr.index:
                        arr[i, j] = float(tail_corr.loc[s2, s1])
            avg_pairwise_corr = _upper_triangle_mean(arr)
        else:
            avg_pairwise_corr = None

        # rolling avg corr history (last ~252 bars)
        avg_corr_history: list[dict[str, Any]] = []
        if len(ret_df) >= corr_window:
            def _rolling_avg_corr(window_ret: pd.DataFrame) -> float:
                if window_ret.shape[0] < 5 or window_ret.shape[1] < 2:
                    return float("nan")
                c = window_ret.corr(min_periods=5)
                n = c.shape[0]
                vals = [c.iloc[i, j] for i in range(n) for j in range(i + 1, n)
                        if math.isfinite(c.iloc[i, j])]
                return float(np.mean(vals)) if vals else float("nan")

            hist_idx = ret_df.index[-252:]
            rolling_corrs: list[dict[str, Any]] = []
            for i in range(len(hist_idx)):
                loc = ret_df.index.get_loc(hist_idx[i])
                start_loc = max(0, loc - corr_window + 1)
                window_df = ret_df.iloc[start_loc: loc + 1]
                v = _rolling_avg_corr(window_df)
                rolling_corrs.append({"date": hist_idx[i].date().isoformat(), "value": _f(v)})
            avg_corr_history = rolling_corrs
    else:
        matrix = []
        avg_pairwise_corr = None
        avg_corr_history = []

    # ratios
    def _ratio_item(key: str, label: str, num_sym: str, den_sym: str) -> dict[str, Any]:
        num = px_series.get(num_sym)
        den = px_series.get(den_sym)
        if num is None or den is None:
            return {"key": key, "label": label, "value": None, "chg_1m_pct": None}
        common = num.index.intersection(den.index)
        if len(common) < 22:
            return {"key": key, "label": label, "value": None, "chg_1m_pct": None}
        n_al = num.reindex(common)
        d_al = den.reindex(common)
        ratio = n_al / d_al
        last_val = _f(float(ratio.iloc[-1]))
        chg = _pct_ret(ratio, 21)
        return {"key": key, "label": label, "value": last_val, "chg_1m_pct": chg}

    ratios = [
        _ratio_item("stocks_bonds", "Stocks/Bonds", "SPY", "TLT"),
        _ratio_item("credit_appetite", "Credit Appetite", "HYG", "IEF"),
        _ratio_item("gold_stocks", "Gold/Stocks", "GLD", "SPY"),
    ]

    return {
        "as_of": now,
        "window_days": lookback_days,
        "symbols": symbols,
        "snapshot": snapshot,
        "matrix": matrix,
        "avg_pairwise_corr": avg_pairwise_corr,
        "avg_corr_history": avg_corr_history,
        "ratios": ratios,
    }


# ── rotation ───────────────────────────────────────────────────

def _compute_rotation(
    sec_map: list[tuple[str, str]],
    fac_map: list[tuple[str, str]],
    bench_px: pd.Series | None,
    get_px,
    bench_sym: str,
    now: str,
    warnings: list[str],
) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    bench_3m = None
    if bench_px is not None:
        bench_3m = _pct_ret(bench_px, 63)

    for group, sym_map in [("sector", sec_map), ("factor", fac_map)]:
        for sym, label in sym_map:
            px = get_px(sym)
            row: dict[str, Any] = {
                "symbol": sym, "label": label, "group": group,
                "ret_1d": None, "ret_1w": None, "ret_1m": None,
                "ret_3m": None, "ret_6m": None, "ret_ytd": None,
                "rs_vs_bench_3m": None,
            }
            if px is not None:
                row["ret_1d"] = _pct_ret(px, 1)
                row["ret_1w"] = _pct_ret(px, 5)
                row["ret_1m"] = _pct_ret(px, 21)
                r3m = _pct_ret(px, 63)
                row["ret_3m"] = r3m
                row["ret_6m"] = _pct_ret(px, 126)
                row["ret_ytd"] = _ytd_ret(px)
                if r3m is not None and bench_3m is not None:
                    row["rs_vs_bench_3m"] = _f(r3m - bench_3m)
            rows.append(row)

    return {"as_of": now, "benchmark": bench_sym, "rows": rows}


# ── RRG ────────────────────────────────────────────────────────

def _compute_rrg(
    sec_map: list[tuple[str, str]],
    fac_map: list[tuple[str, str]],
    bench_px: pd.Series | None,
    get_px,
    bench_sym: str,
    ema_span: int,
    norm_weeks: int,
    tail_weeks: int,
    now: str,
    warnings: list[str],
) -> dict[str, Any]:
    """Relative Rotation Graph the canonical (JdK-style) way.

    Compute on WEEKLY bars (not daily-then-resampled — that made consecutive
    weekly points near-independent and the tails zigzag as noise). For each
    symbol vs the benchmark:
      RS         = weekly close ratio sym/bench, lightly EMA-smoothed
      RS-Ratio   = 100 + rolling z-score(RS) over `norm_weeks` weeks
      RS-Momentum= 100 + rolling z-score of the *week-over-week change* of RS-Ratio
                   (momentum = rate of change of the ratio → leads it ~90°, so the
                   tail curls counter-clockwise through the four quadrants).
    A long (~14w) window relative to the 1-week step makes successive tail points
    share most of their window → smooth rotational arcs instead of spaghetti.
    """
    series_out: list[dict[str, Any]] = []
    if bench_px is None:
        return {"as_of": now, "benchmark": bench_sym, "tail_weeks": tail_weeks, "series": []}

    bench_w = bench_px.resample("W-FRI").last().dropna()
    min_obs = norm_weeks + tail_weeks + 1
    min_periods = max(4, norm_weeks // 2)
    if len(bench_w) < min_obs:
        warnings.append("RRG: insufficient weekly benchmark history")
        return {"as_of": now, "benchmark": bench_sym, "tail_weeks": tail_weeks, "series": []}

    for group, sym_map in [("sector", sec_map), ("factor", fac_map)]:
        for sym, label in sym_map:
            px = get_px(sym)
            if px is None:
                continue
            sym_w = px.resample("W-FRI").last()
            common = bench_w.index.intersection(sym_w.index)
            if len(common) < min_obs:
                continue
            rs = (sym_w.reindex(common) / bench_w.reindex(common)).dropna()
            if len(rs) < min_obs:
                continue

            rs_s = rs.ewm(span=ema_span, min_periods=min(3, ema_span)).mean()
            m = rs_s.rolling(norm_weeks, min_periods=min_periods).mean()
            sd = rs_s.rolling(norm_weeks, min_periods=min_periods).std(ddof=1).replace(0, float("nan"))
            rs_ratio = 100.0 + (rs_s - m) / sd

            # momentum = normalized rate-of-change of RS-Ratio (the JdK definition)
            roc = rs_ratio.diff()
            m2 = roc.rolling(norm_weeks, min_periods=min_periods).mean()
            sd2 = roc.rolling(norm_weeks, min_periods=min_periods).std(ddof=1).replace(0, float("nan"))
            rs_momentum = 100.0 + (roc - m2) / sd2

            # Safety clip (a near-degenerate window can't blow the chart's domain).
            rs_ratio = rs_ratio.clip(lower=85.0, upper=115.0)
            rs_momentum = rs_momentum.clip(lower=85.0, upper=115.0)

            both = pd.concat(
                [rs_ratio.rename("r"), rs_momentum.rename("m")], axis=1
            ).dropna()
            if both.empty:
                continue
            tail_df = both.iloc[-tail_weeks:]

            tail: list[dict[str, Any]] = [
                {
                    "date": dt.date().isoformat(),
                    "rs_ratio": _f(float(row["r"])),
                    "rs_momentum": _f(float(row["m"])),
                }
                for dt, row in tail_df.iterrows()
            ]

            last = tail[-1]
            rr = last["rs_ratio"] or 100.0
            rm = last["rs_momentum"] or 100.0
            if rr >= 100 and rm >= 100:
                quadrant = "Leading"
            elif rr >= 100 and rm < 100:
                quadrant = "Weakening"
            elif rr < 100 and rm < 100:
                quadrant = "Lagging"
            else:
                quadrant = "Improving"

            series_out.append({
                "symbol": sym, "label": label, "group": group,
                "quadrant": quadrant, "tail": tail,
            })

    return {"as_of": now, "benchmark": bench_sym, "tail_weeks": tail_weeks, "series": series_out}


# ── volatility ────────────────────────────────────────────────

def _compute_volatility(
    bench_px: pd.Series | None,
    get_px,
    get_fred,
    vol_syms: list[str],
    vol_pct_window: int,
    now: str,
    warnings: list[str],
) -> dict[str, Any]:
    vixcls = get_fred("VIXCLS")
    vix_px = get_px("^VIX")
    vix3m_px = get_px("^VIX3M")
    move_px = get_px("^MOVE")

    # VIX: prefer live ^VIX, fallback VIXCLS
    vix_series = vix_px if vix_px is not None else vixcls
    vix_val = _f(float(vix_series.iloc[-1])) if vix_series is not None else None

    # VIX percentile
    vix_percentile = None
    if vix_series is not None and len(vix_series) >= 2:
        tail = vix_series.iloc[-vol_pct_window:] if len(vix_series) >= vol_pct_window else vix_series
        if vix_val is not None:
            vix_percentile = _percentile_rank(tail, float(vix_series.iloc[-1]))

    vix3m_val = _f(float(vix3m_px.iloc[-1])) if vix3m_px is not None else None

    term_ratio = None
    term_state = "n/a"  # unknown until ^VIX3M resolves
    if vix_val is not None and vix3m_val is not None and vix3m_val != 0:
        term_ratio = _f(vix_val / vix3m_val)
        if term_ratio is not None:
            if term_ratio < 0.98:
                term_state = "contango"
            elif term_ratio > 1.0:
                term_state = "backwardation"
            else:
                term_state = "flat"

    # realized vol (SPY, last 20 bars)
    realized_vol_pct = None
    implied_minus_realized = None
    if bench_px is not None and len(bench_px) >= 21:
        ret20 = bench_px.pct_change().iloc[-20:]
        rv = float(ret20.std(ddof=1)) * math.sqrt(252) * 100
        realized_vol_pct = _f(rv)
        if vix_val is not None:
            implied_minus_realized = _f(vix_val - rv)

    move_val = _f(float(move_px.iloc[-1])) if move_px is not None else None

    # VIX history (last ~252 bars)
    vix_history: list[dict[str, Any]] = []
    if vix_series is not None:
        tail = vix_series.iloc[-252:]
        for dt, v in tail.items():
            vix_history.append({"date": dt.date().isoformat(), "value": _f(float(v))})

    return {
        "as_of": now,
        "vix": vix_val,
        "vix_percentile": vix_percentile,
        "vix3m": vix3m_val,
        "term_ratio": term_ratio,
        "term_state": term_state,
        "realized_vol_pct": realized_vol_pct,
        "implied_minus_realized": implied_minus_realized,
        "move": move_val,
        "vix_history": vix_history,
    }


# ── conditions ────────────────────────────────────────────────

_CONDITIONS_META: list[tuple[str, str, str, bool]] = [
    # (fred_id, label, unit, higher_is_risk_off)
    ("NFCI", "NFCI", "index", True),
    ("ANFCI", "ANFCI (Adjusted)", "index", True),
    ("BAMLH0A0HYM2", "HY OAS", "%", True),
    ("BAMLC0A0CM", "IG OAS", "%", True),
    ("T10YIE", "10Y Breakeven Inflation", "%", False),
    ("DFII10", "10Y TIPS Yield", "%", True),
    ("DTWEXBGS", "USD Index (Broad)", "index", False),
    ("M2SL", "M2 Money Supply (YoY %)", "% YoY", False),
    ("WALCL", "Fed Balance Sheet", "USD tn", False),
]


def _compute_conditions(get_fred, now: str, warnings: list[str]) -> dict[str, Any]:
    tiles: list[dict[str, Any]] = []

    for fred_id, label, unit, higher_is_risk_off in _CONDITIONS_META:
        s = get_fred(fred_id)
        if s is None or s.empty:
            continue

        s = s.dropna()
        if fred_id == "M2SL":
            # YoY %: latest vs ~12 months prior
            if len(s) < 13:
                continue
            # monthly series: 12 obs back
            prior = float(s.iloc[-13]) if len(s) >= 13 else None
            latest_val = float(s.iloc[-1])
            value = _f((latest_val / prior - 1.0) * 100) if prior and prior != 0 else None
            # rebuild a YoY series for zscore/percentile
            yoy_vals = (s / s.shift(12) - 1.0) * 100
            yoy_vals = yoy_vals.dropna()
            ref_series = yoy_vals
            ref_val = float(yoy_vals.iloc[-1]) if not yoy_vals.empty else value
        else:
            latest_val = float(s.iloc[-1])
            # WALCL is reported in $ millions; display in $ trillions (z/percentile
            # are scale-invariant, so compute them on the raw series).
            value = _f(latest_val / 1e6) if fred_id == "WALCL" else _f(latest_val)
            ref_series = s
            ref_val = latest_val

        date_val = s.index[-1].date().isoformat()
        zscore = _zscore_scalar(ref_series, ref_val) if ref_val is not None else None
        percentile = _percentile_rank(ref_series, ref_val) if ref_val is not None else None

        # trend: compare latest to ~20 obs prior
        trend = "flat"
        if len(s) >= 21:
            prior_20 = float(s.iloc[-21])
            diff = latest_val - prior_20
            if abs(diff) > 0:
                if (latest_val - prior_20) > 0:
                    trend = "up"
                else:
                    trend = "down"

        tiles.append({
            "key": fred_id, "label": label,
            "value": value if fred_id != "M2SL" else value,
            "unit": unit,
            "zscore": _f(zscore),
            "percentile": _f(percentile),
            "trend": trend,
            "date": date_val,
            "higher_is_risk_off": higher_is_risk_off,
        })

    return {"as_of": now, "tiles": tiles}


# ── valuation ─────────────────────────────────────────────────

def _compute_sentiment(
    fg: dict[str, Any], cot: dict[str, Any], now: str, warnings: list[str]
) -> dict[str, Any]:
    if not fg:
        warnings.append("CNN Fear & Greed unavailable")
    if not cot:
        warnings.append("CFTC COT positioning unavailable")
    comps = [
        {"key": c.get("key"), "label": c.get("label"),
         "score": _f(c.get("score")), "rating": c.get("rating")}
        for c in (fg.get("components") or [])
    ]
    hist = [
        {"date": p.get("date"), "score": _f(p.get("score"))}
        for p in (fg.get("history") or []) if p.get("date")
    ]
    cot_rows = [
        {
            "code": r.get("code"), "label": r.get("label"),
            "net": _f(r.get("net")), "oi": _f(r.get("oi")),
            "pct_oi": _f(r.get("pct_oi")), "z_1y": _f(r.get("z_1y")),
            "percentile": _f(r.get("percentile")), "change_1w": _f(r.get("change_1w")),
            "inverse": bool(r.get("inverse")),
            "report_date": r.get("report_date"),
        }
        for r in (cot.get("rows") or [])
    ]
    return {
        "as_of": now,
        "fg_score": _f(fg.get("score")),
        "fg_rating": fg.get("rating"),
        "fg_prev_close": _f(fg.get("previous_close")),
        "fg_prev_week": _f(fg.get("previous_1_week")),
        "fg_prev_month": _f(fg.get("previous_1_month")),
        "fg_prev_year": _f(fg.get("previous_1_year")),
        "fg_components": comps,
        "fg_history": hist,
        "cot_as_of": cot.get("as_of"),
        "cot": cot_rows,
    }


def _ema_series(values: list[float], span: int) -> list[float]:
    """Recursive EMA seeded at the first value (classic McClellan smoothing)."""
    if not values:
        return []
    alpha = 2.0 / (span + 1.0)
    out = [values[0]]
    for v in values[1:]:
        out.append(alpha * v + (1.0 - alpha) * out[-1])
    return out


def _compute_breadth(
    diary: dict[str, Any], history: list[dict[str, Any]], primary: str,
    now: str, warnings: list[str],
) -> dict[str, Any]:
    mkts = diary.get("markets") or {}
    if not mkts:
        warnings.append("market breadth (WSJ diary) unavailable")

    # Snapshot tiles per market — primary first, then the rest in payload order.
    order = [primary] + [m for m in mkts if m != primary]
    markets_out: list[dict[str, Any]] = []
    for mkt in order:
        f = mkts.get(mkt)
        if not f:
            continue
        adv, dec = f.get("advances"), f.get("declines")
        nh, nl = f.get("new_highs"), f.get("new_lows")
        av, dv = f.get("adv_volume"), f.get("decl_volume")
        total = (adv + dec) if adv is not None and dec is not None else None
        markets_out.append({
            "market": mkt, "issues": f.get("issues"),
            "advances": adv, "declines": dec, "unchanged": f.get("unchanged"),
            "breadth_pct": _f(adv / total * 100) if total else None,
            "ad_ratio": _f(adv / dec) if adv is not None and dec else None,
            "new_highs": nh, "new_lows": nl,
            "net_highs": (nh - nl) if nh is not None and nl is not None else None,
            "updown_vol_ratio": _f(av / dv) if av is not None and dv else None,
            "trin": _f(f.get("trin")),
        })

    # Primary-market oscillators from the accumulated daily series.
    dates: list[str] = []
    net_adv: list[float] = []
    rana: list[float] = []  # ratio-adjusted net advances (scale-stable McClellan input)
    for h in history:
        a, d = h.get("advances"), h.get("declines")
        if a is None or d is None:
            continue
        dates.append(h["date"])
        net_adv.append(float(a - d))
        denom = a + d
        rana.append(((a - d) / denom * 1000.0) if denom > 0 else 0.0)

    n = len(net_adv)
    ad_line: list[dict[str, Any]] = []
    cum = 0.0
    for dt, na in zip(dates, net_adv):
        cum += na
        ad_line.append({"date": dt, "value": _f(cum)})

    mcc_hist: list[dict[str, Any]] = []
    mcc_osc = summation = None
    if n >= 2:
        e19, e39 = _ema_series(rana, 19), _ema_series(rana, 39)
        osc = [a - b for a, b in zip(e19, e39)]
        s = 0.0
        for dt, o in zip(dates, osc):
            s += o
            mcc_hist.append({"date": dt, "value": _f(o)})
        mcc_osc, summation = _f(osc[-1]), _f(s)

    warming = n < 40  # EMA39 needs ~40 sessions to be meaningful
    if 0 < n < 40:
        warnings.append(f"breadth oscillators warming up ({n}/40 sessions persisted)")

    return {
        "as_of": now,
        "session_date": diary.get("session_date"),
        "primary_market": primary,
        "markets": markets_out,
        "history_days": n,
        "warming_up": warming,
        "ad_line": ad_line[-260:],
        "mcclellan_osc": mcc_osc,
        "mcclellan_summation": summation,
        "mcclellan_history": mcc_hist[-260:],
    }


def _compute_valuation(
    get_fred, shiller: dict[str, Any], now: str, warnings: list[str]
) -> dict[str, Any]:
    metrics: list[dict[str, Any]] = []

    # Buffett indicator: total US corporate-equity market value / nominal GDP.
    # Numerator NCBEILQ027S (Z.1 nonfinancial corporate equities, $ MILLIONS),
    # denominator GDP ($ BILLIONS) — both quarterly, same dates. Scale the numerator
    # to $bn (/1000) so the ratio reads as the familiar ~2.0 (≈200%). (OpenBB's FRED
    # provider returns no points for the Wilshire 5000 series, so use the Fed Z.1 measure.)
    will = get_fred("NCBEILQ027S")
    gdp = get_fred("GDP")
    buffett_val = buffett_pct = None
    if will is not None and gdp is not None:
        will_q = will.reindex(gdp.index, method="ffill") / 1000.0  # $mn → $bn
        ratio = (will_q / gdp).dropna()
        if not ratio.empty:
            buffett_val = _f(float(ratio.iloc[-1]))
            buffett_pct = _percentile_rank(ratio, float(ratio.iloc[-1]))

    metrics.append({
        "key": "buffett", "label": "Buffett Indicator",
        "value": buffett_val, "percentile": _f(buffett_pct),
        "note": "proxy: corporate equities (Fed Z.1) / nominal GDP",
        "needs_source": False,
    })

    # Real 10Y (TIPS)
    dfii10 = get_fred("DFII10")
    real10y_val = None
    if dfii10 is not None and not dfii10.empty:
        real10y_val = _f(float(dfii10.iloc[-1]))

    metrics.append({
        "key": "real_10y", "label": "10Y Real Yield (TIPS)",
        "value": real10y_val, "percentile": None,
        "note": "10y TIPS yield", "needs_source": False,
    })

    # Shiller CAPE (multpl.com mirror) — latest value + percentile vs full history.
    cape_val = cape_pct = cape_ey = None
    pts = shiller.get("points") or []
    if pts:
        cape_val = _f(shiller.get("cape"))
        cape_ey = shiller.get("earnings_yield")  # = 100 / CAPE (percent)
        cape_hist = pd.Series([float(p["cape"]) for p in pts if p.get("cape")])
        if cape_val is not None and not cape_hist.empty:
            cape_pct = _percentile_rank(cape_hist, cape_val)
    else:
        warnings.append("Shiller CAPE unavailable")

    metrics.append({
        "key": "cape", "label": "Shiller CAPE",
        "value": cape_val, "percentile": _f(cape_pct),
        "note": f"as of {shiller.get('latest_date')}" if pts else "Shiller CAPE source unavailable",
        "needs_source": not bool(pts),
    })

    # Equity risk premium (Fed-model flavour): CAPE earnings yield − 10y real yield.
    erp_val = None
    if cape_ey is not None and real10y_val is not None:
        erp_val = _f(cape_ey - real10y_val)
    metrics.append({
        "key": "erp", "label": "Equity Risk Premium",
        "value": erp_val, "percentile": None,
        "note": "CAPE earnings yield − 10y real (TIPS)",
        "needs_source": erp_val is None,
    })

    return {"as_of": now, "metrics": metrics}
