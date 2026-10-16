"""Portfolio Risk lens (Phase 8.5) — derived analytics over a Wealthfolio
portfolio, COMPUTED (not fetched) from the F0 daily-history backbone.

No new data source: holdings come from `WealthfolioBridge`, daily prices from
`HistoryStore` (Postgres-backed, idempotent yfinance backfill). Everything is
expressed in EUR (the investor's currency); per-asset price series are converted
from their listing/quote currency via the matching `EUR{CCY}=X` series.

Design (judge-panel synthesis): SIMPLE EUR returns for vol/beta/maxDD/VaR (so the
weighted P&L is exact and historical-simulation VaR is honest), LOG returns ONLY
inside the FX-drag decomposition (where r_eur = r_local - r_fx is additive). The
beta benchmark is a composite of the book's own equity/crypto weights (S&P500 +
BTC, both in EUR) because a crypto-heavy book is poorly described by the S&P alone. Covariance is
pairwise-complete + PSD-repaired. The whole computed bundle is cached in Redis
(keyed by a holdings hash + the date) so /xray, /var, /fx-drag share one build.

Symbol resolution + economic currency are explicit config (`RISK_SYMBOL_MAP`),
never an exporter field (which doesn't exist) — surfaced as an assumption.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel
from redis.asyncio import Redis

from app.adapters.factory import build_adapter
from app.adapters.yahoo_symbols import crypto_hint, to_yahoo_symbol
from app.adapters.yfinance_adapter import YFinanceAdapter
from app.config import Settings
from app.db import session_scope
from app.errors import RateLimitExceeded, UpstreamError
from app.services.history import HistoryStore
from app.services.wealthfolio import WealthfolioBridge, make_wealthfolio

logger = logging.getLogger("catallax.risk")

# EUR{CCY}=X = units of CCY per 1 EUR -> price_eur = price_local / fx.
_FX_SYMBOL = {"USD": "EURUSD=X", "GBP": "EURGBP=X", "CHF": "EURCHF=X"}
# Normal-quantile z for a parametric (Gaussian) VaR comparison (no scipy dep).
_Z = {0.95: 1.6448536269514722, 0.99: 2.3263478740408408}
_GEO_BY_CCY = {"USD": "US", "GBP": "UK", "CHF": "CH", "EUR": "EU"}


# ── response models ────────────────────────────────────────────
class Coverage(BaseModel):
    n_assets_total: int
    n_covered: int
    n_skipped: int
    covered_weight_pct: float
    n_observations: int
    window_start: str | None
    window_end: str | None
    by_reason: dict[str, int]
    cash_weight_pct: float
    assumption: str


class Bucket(BaseModel):
    bucket: str
    weight_pct: float
    value_eur: float


class Slice(BaseModel):
    key: str
    weight_pct: float
    value_eur: float


class RiskContributor(BaseModel):
    symbol: str
    contribution_pct: float
    weight_pct: float


class RiskXray(BaseModel):
    base_currency: str = "EUR"
    as_of: str
    lookback_days: int
    n_observations: int
    total_value_eur: float
    vol_daily_pct: float | None
    vol_annual_pct: float | None
    beta: float | None
    beta_vs_sp500: float | None
    beta_vs_btc: float | None
    benchmark: str
    r_squared: float | None
    alpha_annual_pct: float | None
    max_drawdown_pct: float | None
    max_drawdown_peak: str | None
    max_drawdown_trough: str | None
    max_drawdown_recovery: str | None
    hhi: float
    effective_n: float
    top1_pct: float
    top3_pct: float
    top5_pct: float
    concentration_by_class: list[Slice]
    top_holdings: list[Slice]
    risk_contributors: list[RiskContributor]
    exposure_by_class: list[Bucket]
    exposure_by_currency: list[Bucket]
    exposure_by_geo: list[Bucket]
    coverage: Coverage


class VarReport(BaseModel):
    base_currency: str = "EUR"
    as_of: str
    method: str = "historical-simulation"
    confidence: float
    horizon_days: int = 1
    n_scenarios: int
    total_value_eur: float
    var_eur: float | None
    var_pct: float | None
    cvar_eur: float | None
    cvar_pct: float | None
    var99_eur: float | None
    cvar99_eur: float | None
    gaussian_var_eur: float | None
    fat_tail_ratio: float | None  # CVaR95 / VaR95
    hist_vs_gaussian: float | None  # VaR95_hist / VaR95_gaussian
    worst_day: str | None
    worst_day_pct: float | None
    low_confidence: bool
    coverage: Coverage


class FxAsset(BaseModel):
    symbol: str
    currency: str
    currency_source: str
    weight_pct: float
    local_return_pct: float | None
    eur_return_pct: float | None
    fx_contrib_pct: float | None


class FxBucket(BaseModel):
    currency: str
    weight_pct: float
    fx_drag_pct: float


class FxDragReport(BaseModel):
    base_currency: str = "EUR"
    as_of: str
    lookback_days: int
    portfolio_eur_return_pct: float | None
    portfolio_local_return_pct: float | None
    portfolio_fx_drag_pct: float | None
    cross_term_pct: float | None
    by_currency: list[FxBucket]
    by_asset: list[FxAsset]
    excluded: list[dict[str, str]]
    assumption: str
    coverage: Coverage


class StressScenario(BaseModel):
    name: str
    portfolio_return_pct: float
    portfolio_pnl_eur: float
    by_class: list[Slice]
    worst: list[Slice]
    synthetic_classes: list[str]


class StressReport(BaseModel):
    base_currency: str = "EUR"
    as_of: str
    total_value_eur: float
    mode: str = "asset-class proxy shocks"
    scenarios: list[StressScenario]
    assumption: str


_ASSUMPTION = (
    "symbol resolution + economic currency are inferred from config (RISK_SYMBOL_MAP), "
    "not an authoritative exporter field; index ETFs use their listing currency for the "
    "price/FX math (look-through exposure is out of scope)"
)


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
    # Guard the "TRANSIENT" sentinel + anything non-tuple.
    if not isinstance(raw, (tuple, list)) or not raw or not raw[0]:
        return None
    s = pd.Series(raw[1], index=pd.to_datetime(raw[0]))
    s = s[~s.index.duplicated(keep="last")].sort_index()
    s = s[s > 0]  # drop non-positive closes (can't take returns/logs)
    return s if not s.empty else None


class RiskEngine:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        # No request-scoped DB session: every history read uses its own
        # session_scope() (concurrent asyncpg reads can't share one session).
        self._redis = redis
        self._settings = settings
        self._wf: WealthfolioBridge = make_wealthfolio(redis, settings)
        # Populated by bundle() on an actual (cache-miss) fetch; reused by
        # recompute_with_weights() so a weights-only recompute never re-hits the
        # rate-limited price adapters.
        self._context: dict[str, Any] | None = None

    # ── resolution ───────────────────────────────────────────
    def _resolve(self, symbol: str, asset_class: str) -> dict[str, Any]:
        """WF symbol -> {price_symbol, source, currency, currency_source} or skip."""
        sym = symbol.upper()
        if sym in self._settings.risk_skip_set:
            return {"skip": "junk/stablecoin"}
        ov = self._settings.risk_symbol_overrides.get(sym)
        if ov:
            return {
                "price_symbol": ov["price_symbol"],
                "source": ov["source"],
                "currency": ov["currency"],
                "currency_source": "mapped",
                "native_yahoo_ticker": ov["source"] == "yfinance",
                # Explicit Yahoo symbols are authoritative, even when bare.
                "crypto_hint": False if ov["source"] == "yfinance" else None,
            }
        if asset_class.upper() == "CRYPTO":
            # Shared mapping: an alias (EXMPLCOIN -> EXMPL2-USD) beats the naive pair.
            return {"price_symbol": to_yahoo_symbol(sym, self._settings, crypto=True), "source": "yfinance", "currency": "USD", "currency_source": "inferred-default", "native_yahoo_ticker": True}
        return {"price_symbol": sym, "source": "yfinance", "currency": "USD", "currency_source": "inferred-default"}

    def _adapter_for(self, source: str, asset_class: str | bool | None, native_yahoo_ticker: bool):  # noqa: ANN202
        hint = asset_class if isinstance(asset_class, bool) else crypto_hint(asset_class)
        if source == "yfinance":
            return YFinanceAdapter(self._redis, self._settings, crypto=hint, native_ticker=native_yahoo_ticker)
        return build_adapter(source, self._redis, self._settings)

    def history_targets(self, assets: list[tuple[str, str]]) -> list[tuple[Any, str]]:
        """[(adapter, price_symbol)] this lens would read daily bars through for
        `assets` = [(symbol, asset_class)]: holdings, FX legs and both benchmarks.
        The demo warm job stores bars through exactly these adapters, so the
        `daily_bars` keys it writes are the keys `bundle()` reads."""
        held = [SimpleNamespace(symbol=sym, asset_class=cls, value=0.0) for sym, cls in assets]
        specs = self._series_specs(self._resolve_all(held))
        return [(self._adapter_for(src, ac, native), ps) for _k, ps, src, ac, native in specs]

    # ── per-series fetch (own session, bounded concurrency) ──
    async def _fetch_one(
        self, price_symbol: str, source: str, asset_class: str | bool | None,
        native_yahoo_ticker: bool, start: datetime, end: datetime
    ) -> tuple[list[str], list[float], bool]:
        async with session_scope() as session:
            adapter = self._adapter_for(source, asset_class, native_yahoo_ticker)
            env = await HistoryStore(session, self._redis, self._settings).get_daily(
                adapter, price_symbol, start, end
            )
        return (
            [c.ts.date().isoformat() for c in env.data],
            [float(c.close) for c in env.data],
            bool(env.stale),
        )

    async def _gather(
        self, specs: list[tuple[str, str, str, str | bool | None, bool]], start: datetime, end: datetime
    ) -> dict[str, tuple[list[str], list[float], bool] | None]:
        sem = asyncio.Semaphore(self._settings.risk_fetch_concurrency)

        async def one(key: str, ps: str, src: str, asset_class: str | bool | None, native_yahoo_ticker: bool):
            async with sem:
                try:
                    return key, await self._fetch_one(ps, src, asset_class, native_yahoo_ticker, start, end)
                except (RateLimitExceeded, UpstreamError) as exc:
                    # TRANSIENT: don't conflate with a genuinely-unresolvable symbol,
                    # and don't freeze it in the 6h bundle (see bundle()).
                    logger.warning("risk history TRANSIENT fetch failure for %s: %s", ps, exc)
                    return key, "TRANSIENT"
                except Exception as exc:  # genuine no-data → uncovered
                    logger.info("risk history fetch failed for %s: %s", ps, exc)
                    return key, None

        return dict(await asyncio.gather(*(one(k, p, s, a, n) for k, p, s, a, n in specs)))

    # ── holdings fetch + resolution (split out of bundle() so the raw fetched
    # series can be cached and reused by recompute_with_weights) ────────
    async def _holdings_meta(self) -> tuple[list[Any], float, float, int]:
        s = self._settings
        h = await self._wf.holdings()
        by_asset = h.by_asset
        # Defensive cap: bound worst-case fan-out + O(n²) covariance regardless of
        # what the (trusted) exporter returns. Keep the largest positions by value.
        cap = s.risk_max_holdings
        if len(by_asset) > cap:
            logger.warning("risk: %d holdings exceed RISK_MAX_HOLDINGS=%d; capping", len(by_asset), cap)
            by_asset = sorted(by_asset, key=lambda a: float(a.value), reverse=True)[:cap]
        total = float(h.total) or 0.0
        cash = float(h.cash) or 0.0
        return by_asset, cash, total, s.risk_lookback_days

    def _resolve_all(self, by_asset: list[Any]) -> list[dict[str, Any]]:
        # resolve every holding (currency is needed even for skipped ones, for the
        # exposure lens; skipped ones just don't enter the return matrix).
        resolved: list[dict[str, Any]] = []
        for a in by_asset:
            r = self._resolve(a.symbol, a.asset_class)
            # skipped assets still get a currency (USD default) for the exposure lens
            ccy = r.get("currency", "USD") if "skip" not in r else "USD"
            resolved.append({
                "wf": a.symbol.upper(),
                "asset_class": a.asset_class.upper(),
                "value": float(a.value),
                "skip": r.get("skip"),
                "price_symbol": r.get("price_symbol"),
                "source": r.get("source"),
                "currency": ccy,
                "currency_source": r.get("currency_source", "skipped"),
                "crypto_hint": r.get("crypto_hint", crypto_hint(a.asset_class)),
                "native_yahoo_ticker": r.get("native_yahoo_ticker", False),
            })
        return resolved

    def _series_specs(self, resolved: list[dict[str, Any]]) -> list[tuple[str, str, str, str | bool | None, bool]]:
        specs: list[tuple[str, str, str, str | bool | None, bool]] = []
        for r in resolved:
            if not r["skip"] and r["price_symbol"]:
                specs.append((f"A:{r['wf']}", r["price_symbol"], r["source"], r.get("crypto_hint", r["asset_class"]), r.get("native_yahoo_ticker", False)))
        ccys = {r["currency"] for r in resolved if not r["skip"] and r["currency"] != "EUR"}
        for c in ccys:
            if c in _FX_SYMBOL:
                specs.append((f"FX:{c}", _FX_SYMBOL[c], "yfinance", None, False))
        specs.append(("B:EQ", self._settings.risk_bench_equity, "yfinance", None, False))
        specs.append(("B:CR", self._settings.risk_bench_crypto, "yfinance", None, False))
        return specs

    async def _fetch_series(self, resolved: list[dict[str, Any]], lookback: int) -> dict[str, Any]:
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=lookback)
        specs = self._series_specs(resolved)
        return await self._gather(specs, start, end)

    def _params(self, lookback: int) -> dict[str, Any]:
        return {
            "min_history": self._settings.risk_min_history_days,
            "trading_days": self._settings.risk_trading_days,
            "var_conf": self._settings.risk_var_confidence,
            "var_min": self._settings.risk_var_min_scenarios,
            "ffill": self._settings.risk_fx_ffill_max_days,
            "lookback": lookback,
            "bench_label": f"composite({self._settings.risk_bench_equity}+{self._settings.risk_bench_crypto}, EUR)",
        }

    async def _context_or_fetch(self) -> dict[str, Any]:
        """The raw fetched context (resolved holdings + price/FX/benchmark series +
        params) a prior bundle() call cached on this instance, or a fresh fetch if
        none happened yet (e.g. recompute_with_weights() called standalone)."""
        if self._context is not None:
            return self._context
        by_asset, cash, total, lookback = await self._holdings_meta()
        resolved = self._resolve_all(by_asset)
        series = await self._fetch_series(resolved, lookback)
        ctx = {"resolved": resolved, "series": series, "cash": cash, "total": total,
               "params": self._params(lookback)}
        self._context = ctx
        return ctx

    # ── the cached computed bundle (shared by all endpoints) ─
    async def bundle(self, refresh: bool = False) -> dict[str, Any]:
        s = self._settings
        by_asset, cash, total, lookback = await self._holdings_meta()

        # Cache key must change when any RESOLUTION-affecting input changes — incl.
        # asset_class (drives crypto/equity resolution + the beta split) and the
        # risk_* config — else a reclassification/config edit serves a stale bundle.
        sig = json.dumps(
            sorted((a.symbol, a.asset_class, str(a.quantity), str(a.value)) for a in by_asset)
        )
        cfg_sig = json.dumps({
            "map": dict(sorted(s.risk_symbol_overrides.items())),
            "skip": sorted(s.risk_skip_set),
            "bench": [s.risk_bench_equity, s.risk_bench_crypto],
            "min_hist": s.risk_min_history_days, "td": s.risk_trading_days,
            "var_conf": s.risk_var_confidence, "ffill": s.risk_fx_ffill_max_days,
        }, sort_keys=True, default=str)
        digest = hashlib.sha256(f"{sig}|{cfg_sig}|{lookback}|{total}".encode()).hexdigest()[:16]
        key = f"risk:bundle:{digest}:{datetime.now(timezone.utc).date().isoformat()}"
        if not refresh:
            cached = await self._redis.get(key)
            if cached is not None:
                try:
                    return json.loads(cached)
                except Exception as exc:
                    logger.debug("risk bundle cache deserialise failed: %s", exc)

        resolved = self._resolve_all(by_asset)
        series = await self._fetch_series(resolved, lookback)
        params = self._params(lookback)
        self._context = {"resolved": resolved, "series": series, "cash": cash, "total": total, "params": params}

        result = await asyncio.to_thread(
            _compute, resolved, cash, total, series, params
        )
        # Don't freeze a rate-limit-degraded snapshot for the full 6h — let it
        # self-heal within 5 min (warm history then resolves the missing symbols).
        ttl = s.risk_cache_ttl_seconds
        if result.get("meta", {}).get("transient"):
            ttl = min(ttl, 300)
        try:
            await self._redis.set(key, json.dumps(result), ex=ttl)
        except Exception as exc:
            logger.warning("risk bundle cache write failed: %s", exc)
        return result

    # ── weights-injected weekly recompute (advisor sell-gate CVaR) ──────
    async def recompute_with_weights(
        self, weights: dict[str, float], frequency: str = "weekly"
    ) -> tuple[VarReport, dict[str, Any]]:
        """Historical-simulation VaR/CVaR (CVaR95 is what the sell gate consumes) for a
        caller-supplied weight vector at `frequency`, reusing the price series already
        fetched by bundle()/xray()/var()/fx_drag() on this instance — no second
        rate-limited fetch. `weights` maps WF symbol -> portfolio fraction; keys not
        found among the currently-resolved holdings are dropped and surfaced as a
        warning (the gate must still get a usable number). Deterministic: same
        weights + same cached series -> identical output, no RNG anywhere.

        Call-order contract: call bundle() (or xray()/var()/fx_drag(), which all go
        through bundle()) FIRST on this same engine instance to guarantee this reuses
        that single fetch. Calling recompute_with_weights() before any of those
        triggers its own independent fetch via _context_or_fetch() (self-sufficient,
        but not fetch-free) — and a LATER bundle() cache-miss still does its own fetch
        and overwrites self._context, so don't rely on a recompute-first call's
        context surviving a subsequent bundle() call on the same instance.
        """
        ctx = await self._context_or_fetch()
        result = await asyncio.to_thread(
            _compute_weighted, ctx["resolved"], ctx["series"], ctx["total"], weights, frequency, ctx["params"],
        )
        return VarReport.model_validate(result["var"]), result["meta"]

    # ── public endpoints (slice the bundle) ──────────────────
    async def xray(self) -> tuple[RiskXray, dict[str, Any]]:
        b = await self.bundle()
        return RiskXray.model_validate(b["xray"]), b["meta"]

    async def var(self) -> tuple[VarReport, dict[str, Any]]:
        b = await self.bundle()
        return VarReport.model_validate(b["var"]), b["meta"]

    async def fx_drag(self) -> tuple[FxDragReport, dict[str, Any]]:
        b = await self.bundle()
        return FxDragReport.model_validate(b["fx_drag"]), b["meta"]

    async def stress(self) -> StressReport:
        """Static class-proxy shocks on current weights — no history fetch."""
        if not self._settings.risk_stress_enabled:
            return StressReport(
                as_of=datetime.now(timezone.utc).isoformat(), total_value_eur=0.0,
                scenarios=[], assumption="stress testing disabled (RISK_STRESS_ENABLED=false)",
            )
        h = await self._wf.holdings()
        total = float(h.total) or 1.0
        cash = float(h.cash) or 0.0
        positions: list[dict[str, Any]] = []
        for a in h.by_asset:
            positions.append({"sym": a.symbol.upper(), "cls": a.asset_class.upper(), "val": float(a.value),
                              "ccy": self._resolve(a.symbol, a.asset_class).get("currency", "USD")})
        scenarios: list[StressScenario] = []
        for sc in self._settings.risk_stress_list:
            cls_shock = sc["class_shocks"]  # type: ignore[assignment]
            fx_shock = sc["fx_shocks"]  # type: ignore[assignment]
            synth = sc["synthetic"]  # type: ignore[assignment]
            by_class: dict[str, float] = {}
            legs: list[tuple[str, float]] = []
            total_pnl = 0.0
            for p in positions:
                shock = float(cls_shock.get(p["cls"], 0.0)) + float(fx_shock.get(p["ccy"], 0.0))  # type: ignore[union-attr]
                pnl = p["val"] * shock
                total_pnl += pnl
                by_class[p["cls"]] = by_class.get(p["cls"], 0.0) + pnl
                legs.append((p["sym"], pnl))
            # cash: unshocked (CASH class shock if provided)
            cash_shock = float(cls_shock.get("CASH", 0.0))  # type: ignore[union-attr]
            total_pnl += cash * cash_shock
            legs.sort(key=lambda x: x[1])
            scenarios.append(StressScenario(
                name=str(sc["name"]),
                portfolio_return_pct=_f(total_pnl / total * 100) or 0.0,
                portfolio_pnl_eur=_f(total_pnl, 2) or 0.0,
                by_class=[Slice(key=k, weight_pct=_f(v / total * 100) or 0.0, value_eur=_f(v, 2) or 0.0)
                          for k, v in sorted(by_class.items(), key=lambda kv: kv[1])],
                worst=[Slice(key=s, weight_pct=_f(v / total * 100) or 0.0, value_eur=_f(v, 2) or 0.0)
                       for s, v in legs[:5]],
                synthetic_classes=list(synth),  # type: ignore[arg-type]
            ))
        return StressReport(
            as_of=datetime.now(timezone.utc).isoformat(),
            total_value_eur=_f(total, 2) or 0.0,
            scenarios=scenarios,
            assumption="asset-class proxy shocks applied to current weights, not a per-asset historical replay",
        )


# ── pure compute (runs in a thread; no async/Redis here) ───────
def _to_eur(local: pd.Series, ccy: str, fx_map: dict[str, pd.Series], ffill: int) -> pd.Series | None:
    if ccy == "EUR":
        return local
    fx = fx_map.get(ccy)
    if fx is None:
        return None
    fx_al = fx.reindex(local.index).ffill(limit=ffill)
    eur = local / fx_al
    return eur.dropna()


def _returns(px: pd.Series, master: pd.DatetimeIndex, ffill: int) -> pd.Series:
    al = px.reindex(master).ffill(limit=ffill)
    return al.pct_change(fill_method=None)


def _compute(
    resolved: list[dict[str, Any]],
    cash: float,
    total: float,
    series: dict[str, tuple[list[str], list[float], bool] | None],
    params: dict[str, Any],
) -> dict[str, Any]:
    td = int(params["trading_days"])
    min_hist = int(params["min_history"])
    ffill = int(params["ffill"])
    conf = float(params["var_conf"])
    now = datetime.now(timezone.utc).isoformat()
    warnings: list[str] = []
    stale_any = False
    transient_any = False  # a transient (rate-limit/upstream) fetch failure occurred

    total = total or 1.0
    fx_map: dict[str, pd.Series] = {}
    for c, sym in _FX_SYMBOL.items():
        s = _series(series.get(f"FX:{c}"))
        if s is not None:
            fx_map[c] = s

    # benchmark master calendar = equity benchmark (^GSPC) trading days
    bench_eq = _series(series.get("B:EQ"))
    bench_cr = _series(series.get("B:CR"))
    if bench_eq is not None:
        master = bench_eq.index
    elif bench_cr is not None:
        # ^GSPC degraded: BTC trades 7d/wk → a valid, richer calendar than an
        # equity-only union. Keeps a crypto-heavy book measurable (beta nulls out).
        master = bench_cr.index
    else:
        # last resort: union of ALL covered asset dates (crypto included)
        idxs = [s.index for r in resolved if not r["skip"]
                and (s := _series(series.get(f"A:{r['wf']}"))) is not None]
        master = pd.DatetimeIndex([])
        for ix in idxs:
            master = master.union(ix)

    # ── per-asset EUR price + coverage ──
    covered: list[dict[str, Any]] = []  # {wf, cls, value, ccy, csrc, eur_px(aligned), local_px, ret}
    by_reason: dict[str, int] = {}
    n_skipped = 0
    for r in resolved:
        if r["skip"]:
            n_skipped += 1
            by_reason["skip"] = by_reason.get("skip", 0) + 1
            continue
        raw = series.get(f"A:{r['wf']}")
        if raw == "TRANSIENT":
            by_reason["rate-limited"] = by_reason.get("rate-limited", 0) + 1
            transient_any = True
            continue
        if isinstance(raw, (tuple, list)) and len(raw) > 2 and raw[2]:
            stale_any = True
        local = _series(raw)
        if local is None:
            by_reason["unresolved"] = by_reason.get("unresolved", 0) + 1
            continue
        eur = _to_eur(local, r["currency"], fx_map, ffill)
        if eur is None:
            by_reason["fx-unavailable"] = by_reason.get("fx-unavailable", 0) + 1
            continue
        if len(master) == 0:
            by_reason["no-calendar"] = by_reason.get("no-calendar", 0) + 1
            continue
        ret = _returns(eur, master, ffill)
        nobs = int(ret.notna().sum())
        if nobs < min_hist:
            by_reason["insufficient-history"] = by_reason.get("insufficient-history", 0) + 1
            continue
        covered.append({**r, "eur": eur.reindex(master).ffill(limit=ffill),
                        "local": local.reindex(master).ffill(limit=ffill), "ret": ret})

    n_total = len(resolved)
    cov_value = sum(c["value"] for c in covered)
    covered_weight_pct = _f(cov_value / total * 100) or 0.0
    window_start = master.min().date().isoformat() if len(master) else None
    window_end = master.max().date().isoformat() if len(master) else None

    # ── return matrix (covered) ──
    if covered:
        ret_df = pd.concat({c["wf"]: c["ret"] for c in covered}, axis=1).iloc[1:]
        value_vec = np.array([c["value"] for c in covered], dtype=float)
        # absolute EUR daily P&L (uncovered + cash treated as zero-vol)
        pnl = (ret_df.fillna(0.0) * value_vec).sum(axis=1)
        r_p = pnl / total
        n_obs = int(r_p.shape[0])
    else:
        ret_df = pd.DataFrame()
        value_vec = np.array([])
        pnl = pd.Series(dtype=float)
        r_p = pd.Series(dtype=float)
        n_obs = 0
        warnings.append("no covered holdings with sufficient history; risk metrics unavailable")

    coverage = {
        "n_assets_total": n_total, "n_covered": len(covered), "n_skipped": n_skipped,
        "covered_weight_pct": covered_weight_pct, "n_observations": n_obs,
        "window_start": window_start, "window_end": window_end, "by_reason": by_reason,
        "cash_weight_pct": _f(cash / total * 100) or 0.0, "assumption": _ASSUMPTION,
    }
    if covered_weight_pct < 100:
        warnings.append(f"covered {covered_weight_pct:.0f}% of portfolio value; {n_total - len(covered)} assets excluded")

    # ── volatility ──
    vol_daily = vol_annual = None
    if n_obs >= 2:
        sd = float(r_p.std(ddof=1))
        vol_daily = _f(sd * 100, 4)
        vol_annual = _f(sd * math.sqrt(td) * 100, 4)

    # ── beta (composite benchmark) ──
    beta = beta_sp = beta_btc = r_squared = alpha_annual = None
    bench_label = params["bench_label"]
    if n_obs >= min_hist and bench_eq is not None and bench_cr is not None:
        gspc = _to_eur(bench_eq, "USD", fx_map, ffill)
        btc = _to_eur(bench_cr, "USD", fx_map, ffill)
        if gspc is not None and btc is not None:
            g_ret = _returns(gspc, master, ffill).iloc[1:]
            b_ret = _returns(btc, master, ffill).iloc[1:]
            cov_eq = sum(c["value"] for c in covered if c["asset_class"] != "CRYPTO")
            cov_cr = sum(c["value"] for c in covered if c["asset_class"] == "CRYPTO")
            wsum = cov_eq + cov_cr
            if wsum > 0:
                comp = (cov_eq / wsum) * g_ret + (cov_cr / wsum) * b_ret
                beta = _beta(r_p, comp)
                beta_sp = _beta(r_p, g_ret)
                beta_btc = _beta(r_p, b_ret)
                reg = _ols(r_p, comp)
                if reg is not None:
                    r_squared = _f(reg[2], 4)
                    alpha_annual = _f(reg[1] * td * 100, 4)

    # ── max drawdown ──
    mdd = peak_d = trough_d = recov_d = None
    if n_obs >= 2:
        w = (1.0 + r_p).cumprod()
        peak = w.cummax()
        dd = w / peak - 1.0
        mdd = _f(float(dd.min()) * 100, 4)
        t = dd.idxmin()
        trough_d = t.date().isoformat()
        pk = w.loc[:t].idxmax()
        peak_d = pk.date().isoformat()
        after = w.loc[t:]
        rec = after[after >= w.loc[pk]]
        recov_d = rec.index[0].date().isoformat() if len(rec) else None

    # ── risk contributors (pairwise cov + PSD repair) ──
    contributors: list[dict[str, Any]] = []
    if len(covered) >= 2 and n_obs >= min_hist:
        cov = ret_df.cov(min_periods=min_hist)
        cov = cov.fillna(0.0)
        sigma = _psd_repair(cov.to_numpy())
        wv = value_vec / value_vec.sum()
        sig_w = sigma @ wv
        port_var = float(wv @ sig_w)
        if port_var > 0:
            contrib = wv * sig_w  # sums to port_var
            csum = contrib.sum() or 1.0
            order = np.argsort(contrib)[::-1]
            for i in order[:6]:
                contributors.append({
                    "symbol": covered[i]["wf"],
                    "contribution_pct": _f(contrib[i] / csum * 100, 2) or 0.0,
                    "weight_pct": _f(wv[i] * 100, 2) or 0.0,
                })

    # ── concentration (all positions + cash, by value) ──
    pos = [(r["wf"], r["value"]) for r in resolved]
    if cash > 0:
        pos.append(("CASH", cash))
    weights_all = np.array([v / total for _, v in pos], dtype=float)
    hhi = float((weights_all ** 2).sum()) if len(weights_all) else 1.0
    eff_n = 1.0 / hhi if hhi > 0 else 0.0
    pos_sorted = sorted(pos, key=lambda x: x[1], reverse=True)
    cw = np.array([v / total for _, v in pos_sorted])
    top1 = _f(cw[:1].sum() * 100) or 0.0
    top3 = _f(cw[:3].sum() * 100) or 0.0
    top5 = _f(cw[:5].sum() * 100) or 0.0
    top_holdings = [{"key": s, "weight_pct": _f(v / total * 100, 2) or 0.0, "value_eur": _f(v, 2) or 0.0}
                    for s, v in pos_sorted[:8]]
    class_val: dict[str, float] = {}
    for r in resolved:
        class_val[r["asset_class"]] = class_val.get(r["asset_class"], 0.0) + r["value"]
    if cash > 0:
        class_val["CASH"] = class_val.get("CASH", 0.0) + cash
    conc_by_class = [{"key": k, "weight_pct": _f(v / total * 100, 2) or 0.0, "value_eur": _f(v, 2) or 0.0}
                     for k, v in sorted(class_val.items(), key=lambda kv: kv[1], reverse=True)]

    # ── exposure (class / currency / geo) over ALL by_asset + cash ──
    exp_class: dict[str, float] = dict(class_val)
    ccy_val: dict[str, float] = {}
    geo_val: dict[str, float] = {}
    for r in resolved:
        ccy_val[r["currency"]] = ccy_val.get(r["currency"], 0.0) + r["value"]
        geo = "CRYPTO" if r["asset_class"] == "CRYPTO" else _GEO_BY_CCY.get(r["currency"], "OTHER")
        geo_val[geo] = geo_val.get(geo, 0.0) + r["value"]
    if cash > 0:
        ccy_val["EUR"] = ccy_val.get("EUR", 0.0) + cash
        geo_val["EU"] = geo_val.get("EU", 0.0) + cash

    def buckets(d: dict[str, float]) -> list[dict[str, Any]]:
        return [{"bucket": k, "weight_pct": _f(v / total * 100, 2) or 0.0, "value_eur": _f(v, 2) or 0.0}
                for k, v in sorted(d.items(), key=lambda kv: kv[1], reverse=True)]

    # ── VaR / CVaR (historical simulation on EUR P&L) ──
    var_b = _var_block(pnl, total, conf, int(params["var_min"]))

    # ── FX drag ──
    fx_b = _fx_drag_block(covered, fx_map, master, ffill, cov_value, warnings)

    xray = {
        "base_currency": "EUR", "as_of": now, "lookback_days": int(params["lookback"]),
        "n_observations": n_obs, "total_value_eur": _f(total, 2) or 0.0,
        "vol_daily_pct": vol_daily, "vol_annual_pct": vol_annual,
        "beta": beta, "beta_vs_sp500": beta_sp, "beta_vs_btc": beta_btc, "benchmark": bench_label,
        "r_squared": r_squared, "alpha_annual_pct": alpha_annual,
        "max_drawdown_pct": mdd, "max_drawdown_peak": peak_d, "max_drawdown_trough": trough_d,
        "max_drawdown_recovery": recov_d,
        "hhi": _f(hhi, 4) or 0.0, "effective_n": _f(eff_n, 2) or 0.0,
        "top1_pct": top1, "top3_pct": top3, "top5_pct": top5,
        "concentration_by_class": conc_by_class, "top_holdings": top_holdings,
        "risk_contributors": contributors,
        "exposure_by_class": buckets(exp_class), "exposure_by_currency": buckets(ccy_val),
        "exposure_by_geo": buckets(geo_val), "coverage": coverage,
    }
    var_report = {
        "base_currency": "EUR", "as_of": now, "method": "historical-simulation",
        "confidence": conf, "horizon_days": 1, "total_value_eur": _f(total, 2) or 0.0,
        **var_b, "coverage": coverage,
    }
    fx_report = {
        "base_currency": "EUR", "as_of": now, "lookback_days": int(params["lookback"]),
        **fx_b, "assumption": _ASSUMPTION, "coverage": coverage,
    }
    if transient_any:
        warnings.append("some history reads were rate-limited; coverage is temporarily reduced (self-heals)")
    return {
        "xray": xray, "var": var_report, "fx_drag": fx_report,
        "meta": {"stale": stale_any or transient_any, "transient": transient_any,
                 "warnings": warnings, "source": "risk-engine"},
    }


# ── weights-injected weekly recompute ──────────────────────
def _resample_weekly(raw: object) -> object:
    """Weekly-last resample of one raw (dates, closes, stale) series, in the same
    tuple shape `_series()` parses. Non-parseable input (None/"TRANSIENT"/empty) is
    passed through unchanged so downstream coverage accounting still sees it."""
    s = _series(raw)
    if s is None:
        return raw
    wk = s.resample("W-FRI").last().dropna()
    stale = bool(isinstance(raw, (tuple, list)) and len(raw) > 2 and raw[2])
    return ([d.date().isoformat() for d in wk.index], [float(v) for v in wk.to_numpy()], stale)


def _weighted_resolved(
    resolved: list[dict[str, Any]], weights: dict[str, float], total: float
) -> tuple[list[dict[str, Any]], list[str]]:
    """Replace each holding's actual value with `weight * total` so `_compute`'s
    existing value-weighted P&L machinery runs on the caller's alternative
    allocation instead of the real one. Weight keys with no matching (unskipped)
    resolved holding are reported back as unresolved rather than raising."""
    by_sym = {r["wf"]: r for r in resolved if not r["skip"]}
    out: list[dict[str, Any]] = []
    unresolved: list[str] = []
    for sym, w in weights.items():
        if not w:
            continue
        r = by_sym.get(sym.upper())
        if r is None:
            unresolved.append(sym.upper())
            continue
        out.append({**r, "value": float(w) * total})
    return out, unresolved


def _compute_weighted(
    resolved: list[dict[str, Any]],
    series: dict[str, Any],
    total: float,
    weights: dict[str, float],
    frequency: str,
    params: dict[str, Any],
) -> dict[str, Any]:
    if frequency not in ("daily", "weekly"):
        raise ValueError(f"unsupported frequency: {frequency!r}")
    weighted_resolved, unresolved = _weighted_resolved(resolved, weights, total)
    cash = max(total - sum(r["value"] for r in weighted_resolved), 0.0)
    freq_series = series if frequency == "daily" else {k: _resample_weekly(v) for k, v in series.items()}
    result = _compute(weighted_resolved, cash, total, freq_series, params)
    var_report = {**result["var"], "horizon_days": 7 if frequency == "weekly" else 1}
    warnings = list(result["meta"]["warnings"])
    weight_sum = sum(weights.values())
    if weight_sum > 1.0:
        # Over-allocated input: cash = max(total - Σvalues, 0) silently floors at 0
        # rather than going negative, which would otherwise hide the over-allocation
        # from the cash/coverage math — surface it explicitly instead.
        warnings.append(
            f"recompute_with_weights: weights sum to {weight_sum:.4f} (> 1.0); "
            "cash was floored at 0 rather than allowed negative, so downstream "
            "weight_pct figures may sum above 100%"
        )
    if unresolved:
        warnings.append(
            f"recompute_with_weights: {len(unresolved)} weight symbol(s) had no fetched "
            f"series and were excluded: {', '.join(sorted(unresolved))}"
        )
    meta = {**result["meta"], "warnings": warnings, "frequency": frequency}
    return {"var": var_report, "meta": meta}


def _beta(y: pd.Series, x: pd.Series) -> float | None:
    df = pd.concat([y, x], axis=1).dropna()
    if len(df) < 3:
        return None
    yv, xv = df.iloc[:, 0].to_numpy(), df.iloc[:, 1].to_numpy()
    vx = float(np.var(xv, ddof=1))
    if vx <= 0:
        return None
    return _f(float(np.cov(yv, xv, ddof=1)[0, 1]) / vx, 4)


def _ols(y: pd.Series, x: pd.Series) -> tuple[float, float, float] | None:
    """Return (beta, alpha, r2) for y = alpha + beta*x."""
    df = pd.concat([y, x], axis=1).dropna()
    if len(df) < 3:
        return None
    yv, xv = df.iloc[:, 0].to_numpy(), df.iloc[:, 1].to_numpy()
    vx = float(np.var(xv, ddof=1))
    if vx <= 0:
        return None
    b = float(np.cov(yv, xv, ddof=1)[0, 1]) / vx
    a = float(yv.mean() - b * xv.mean())
    pred = a + b * xv
    ss_res = float(((yv - pred) ** 2).sum())
    ss_tot = float(((yv - yv.mean()) ** 2).sum())
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    return b, a, r2


def _psd_repair(m: np.ndarray) -> np.ndarray:
    """Clip negative eigenvalues to 0 so a ragged-history pairwise cov is PSD."""
    try:
        vals, vecs = np.linalg.eigh((m + m.T) / 2.0)
        vals = np.clip(vals, 0.0, None)
        return (vecs * vals) @ vecs.T
    except np.linalg.LinAlgError:
        return m


def _var_block(pnl: pd.Series, total: float, conf: float, min_scen: int) -> dict[str, Any]:
    out: dict[str, Any] = {
        "n_scenarios": 0, "var_eur": None, "var_pct": None, "cvar_eur": None, "cvar_pct": None,
        "var99_eur": None, "cvar99_eur": None, "gaussian_var_eur": None,
        "fat_tail_ratio": None, "hist_vs_gaussian": None, "worst_day": None, "worst_day_pct": None,
        "confidence": conf, "low_confidence": True,
    }
    p = pnl.dropna()
    n = int(p.shape[0])
    out["n_scenarios"] = n
    if n < 2:
        return out
    arr = p.to_numpy()
    out["low_confidence"] = n < min_scen

    def hs(c: float) -> tuple[float | None, float | None]:
        q = float(np.quantile(arr, 1 - c, method="linear"))
        tail = arr[arr <= q]
        var = -q
        cvar = -float(tail.mean()) if tail.size else var
        return var, cvar

    v95, cv95 = hs(conf)
    v99, cv99 = hs(0.99)
    mean, sd = float(arr.mean()), float(arr.std(ddof=1))
    gvar = _Z.get(round(conf, 2), 1.645) * sd - mean  # parametric comparison
    out["var_eur"] = _f(v95, 2)
    out["var_pct"] = _f(v95 / total * 100, 4) if v95 is not None else None
    out["cvar_eur"] = _f(cv95, 2)
    out["cvar_pct"] = _f(cv95 / total * 100, 4) if cv95 is not None else None
    out["var99_eur"] = _f(v99, 2)
    out["cvar99_eur"] = _f(cv99, 2)
    out["gaussian_var_eur"] = _f(gvar, 2)
    out["fat_tail_ratio"] = _f(cv95 / v95, 3) if v95 and cv95 else None
    out["hist_vs_gaussian"] = _f(v95 / gvar, 3) if v95 and gvar > 0 else None
    wi = int(np.argmin(arr))
    out["worst_day"] = p.index[wi].date().isoformat()
    out["worst_day_pct"] = _f(arr[wi] / total * 100, 4)
    return out


def _fx_drag_block(
    covered: list[dict[str, Any]], fx_map: dict[str, pd.Series], master: pd.DatetimeIndex,
    ffill: int, cov_value: float, warnings: list[str],
) -> dict[str, Any]:
    by_asset: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    ccy_drag: dict[str, float] = {}
    ccy_weight: dict[str, float] = {}
    p_local = p_eur = p_fx = 0.0
    cov_value = cov_value or 1.0

    for c in covered:
        w = c["value"] / cov_value
        ccy = c["currency"]
        eur = c["eur"].dropna()
        if ccy == "EUR":
            local_ret = eur.iloc[-1] / eur.iloc[0] - 1.0 if len(eur) >= 2 else None
            eur_ret = local_ret
            fx_contrib = 0.0
        else:
            fx = fx_map.get(ccy)
            if fx is None:
                excluded.append({"symbol": c["wf"], "reason": "fx-unavailable"})
                continue
            # Build local + FX + EUR on the SAME master grid so the log
            # decomposition is exact: eur = local / fx on one index =>
            # eur_log == local_log - fx_log with zero residual by construction.
            local_m = c["local"]  # already master-aligned at bundle build
            fx_m = fx.reindex(master).ffill(limit=ffill)
            eur_m = (local_m / fx_m).dropna()
            if len(eur_m) < 2:
                excluded.append({"symbol": c["wf"], "reason": "fx-unavailable"})
                continue
            local_log = np.log(local_m).diff()
            fx_log = np.log(fx_m).diff()
            eur_log = np.log(eur_m).diff()
            # reconstruction check (now an FX-convention guard, not an alignment one)
            resid = (eur_log - (local_log - fx_log)).reindex(eur_log.index).abs().max()
            if pd.notna(resid) and resid > 1e-6:
                warnings.append(f"fx-drag reconstruction residual {resid:.2e} for {c['wf']}")
            local_ret = math.exp(float(np.nansum(local_log))) - 1.0
            eur_ret = math.exp(float(np.nansum(eur_log))) - 1.0
            fx_contrib = eur_ret - local_ret
        p_local += w * (local_ret or 0.0)
        p_eur += w * (eur_ret or 0.0)
        p_fx += w * fx_contrib
        ccy_drag[ccy] = ccy_drag.get(ccy, 0.0) + w * fx_contrib
        ccy_weight[ccy] = ccy_weight.get(ccy, 0.0) + w
        by_asset.append({
            "symbol": c["wf"], "currency": ccy, "currency_source": c["currency_source"],
            "weight_pct": _f(w * 100, 2) or 0.0,
            "local_return_pct": _f((local_ret or 0.0) * 100, 3),
            "eur_return_pct": _f((eur_ret or 0.0) * 100, 3),
            "fx_contrib_pct": _f(fx_contrib * 100, 3),
        })

    by_asset.sort(key=lambda a: abs(a["fx_contrib_pct"] or 0.0), reverse=True)
    return {
        "portfolio_eur_return_pct": _f(p_eur * 100, 3),
        "portfolio_local_return_pct": _f(p_local * 100, 3),
        "portfolio_fx_drag_pct": _f(p_fx * 100, 3),
        "cross_term_pct": _f((p_eur - (p_local + p_fx)) * 100, 4),
        "by_currency": [{"currency": k, "weight_pct": _f(ccy_weight.get(k, 0.0) * 100, 2) or 0.0,
                         "fx_drag_pct": _f(v * 100, 3) or 0.0}
                        for k, v in sorted(ccy_drag.items(), key=lambda kv: abs(kv[1]), reverse=True)],
        "by_asset": by_asset,
        "excluded": excluded,
    }
