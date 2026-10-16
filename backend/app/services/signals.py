"""Signal self-scoring core (Phase 9, deliverable #1) — the honest heart of a
backtester, built BEFORE the engine.

Logs emitted cross-sectional SCORES (the `signals` table) and grades them against
REALISED forward returns, COMPUTED (never fetched) from the F0 daily-history
backbone. No new data source: prices come from `HistoryStore` (Postgres-backed,
idempotent yfinance backfill), the demo universe from `WealthfolioBridge` resolved
exactly as the risk lens resolves it.

Design (judge-panel synthesis):
  * A signal row is a CONTINUOUS cross-sectional score (higher value ⇒ higher
    expected forward return, sign-normalised at emission), keyed UNIQUE
    (signal_id, as_of_date, symbol) for idempotent upsert. `symbol` is the
    F0-priceable symbol (e.g. SAP.DE), `source` its adapter.
  * Grading is LAZY and pure — recomputed from F0 so a derived number can never go
    stale against a restated bar. The lookahead guard: entry = first bar STRICTLY
    after the decision date T, exit = H trading-bars later by position, graded ONLY
    if the exit bar exists AND is settled (≥ `signal_settle_days` bars trail it —
    history.py finalises provisional closes via on_conflict_do_update, so the tail
    bar is not yet trustworthy). Pending/uncovered pairs are counted, never dropped.
  * Primary grade = cross-sectional Rank-IC per date → ICIR = mean/std → t =
    ICIR·√N_dates (two-sided p via math.erf; no scipy). Quintile long/short spread
    + monotonicity as the economic check; hit-rate (Wilson-LB) secondary. A pooled
    fallback (loudly flagged "non-independent; indicative only") covers thin /
    single-name future scanner signals.

HONESTY (surfaced as Envelope warnings, not buried): the seeded demo grades over a
tiny, BTC-dominated personal book using TODAY's holdings applied to PAST dates
(survivorship/selection lookahead at the universe level) — descriptive machinery,
not research-grade edge. The SAME grader is built to score a Phase-11 S&P-wide
cross-section a scanner will later POST.
"""
from __future__ import annotations

import asyncio
import bisect
import hashlib
import json
import logging
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.adapters.factory import build_adapter
from app.config import Settings
from app.db import session_scope
from app.errors import DemoModeReadOnly, InvalidRequest, NotFound, RateLimitExceeded, UpstreamError
from app.models import SignalRow
from app.services.history import HistoryStore
from app.services.risk import RiskEngine

logger = logging.getLogger("catallax.signals")

_Z95 = 1.959963984540054  # normal 95% two-sided z (Wilson lower bound)
_MAX_SIGNAL_ROWS = 500_000  # defensive cap on rows graded for one signal_id (OOM guard)
_MAX_META_BYTES = 8192  # cap on a row's `meta` JSONB payload

_SURVIVORSHIP = (
    "demo universe = TODAY's Wealthfolio holdings applied to PAST decision dates "
    "(survivorship/selection lookahead at the universe level — per-signal pricing is "
    "lookahead-clean, but the basket is not point-in-time)"
)
_LUMPY = (
    "tiny, BTC-dominated personal book — Rank-IC/ICIR here are descriptive machinery, "
    "not research-grade edge; the same grader is built to score a broad cross-section"
)


# ── response models ────────────────────────────────────────────
class SignalCatalogEntry(BaseModel):
    signal_id: str
    n_rows: int
    n_symbols: int
    n_dates: int
    first_date: str | None
    last_date: str | None


class SignalCatalog(BaseModel):
    signals: list[SignalCatalogEntry]


class ICPoint(BaseModel):
    date: str
    n_names: int
    rank_ic: float | None
    pearson_ic: float | None
    ls_spread_pct: float | None


class QuantileBucket(BaseModel):
    bucket: int  # 1 = lowest score, Q = highest
    mean_forward_return_pct: float | None
    n_obs: int


class SignalCoverage(BaseModel):
    n_rows: int
    n_pairs_matured: int
    n_pairs_pending: int
    n_pairs_uncovered: int
    n_dates_total: int
    n_dates_graded: int
    mean_names_per_date: float | None
    window_start: str | None
    window_end: str | None
    by_status: dict[str, int]


class SignalGrade(BaseModel):
    signal_id: str
    as_of: str
    horizon_bars: int
    quantiles: int
    settle_days: int
    methodology: str  # "cross-sectional" | "pooled (non-independent; indicative only)"
    n_dates_graded: int
    mean_rank_ic: float | None
    mean_ic: float | None
    icir: float | None
    t_stat: float | None
    p_value: float | None
    significant: bool
    low_confidence: bool
    ls_spread_mean_pct: float | None
    ls_spread_t: float | None
    ls_cumulative_pct: float | None  # compounded top-minus-bottom equity (demeaned)
    monotonicity: float | None
    hit_rate: float | None
    hit_rate_wilson_lb: float | None
    n_hit_pairs: int
    long_mean_fwd_pct: float | None
    short_mean_fwd_pct: float | None
    per_date: list[ICPoint]
    quantile_buckets: list[QuantileBucket]
    coverage: SignalCoverage
    warnings: list[str]


class SignalGradeSummary(BaseModel):
    signal_id: str
    horizon_bars: int
    methodology: str
    n_dates_graded: int
    mean_rank_ic: float | None
    icir: float | None
    t_stat: float | None
    significant: bool
    low_confidence: bool
    ls_spread_mean_pct: float | None
    ls_cumulative_pct: float | None
    hit_rate: float | None
    hit_rate_wilson_lb: float | None
    n_pairs_matured: int
    n_pairs_pending: int


class Scoreboard(BaseModel):
    as_of: str
    horizon_bars: int
    quantiles: int
    rows: list[SignalGradeSummary]
    warnings: list[str]


class CrossSectionRow(BaseModel):
    symbol: str
    value: float
    status: str  # matured | pending | uncovered
    entry_date: str | None
    exit_date: str | None
    forward_return_pct: float | None


class CrossSection(BaseModel):
    signal_id: str
    as_of_date: str
    horizon_bars: int
    settle_days: int
    n_matured: int
    n_pending: int
    n_uncovered: int
    rows: list[CrossSectionRow]


class EmitResult(BaseModel):
    n_received: int
    n_written: int
    signal_ids: list[str]


def _f(x: Any, nd: int = 6) -> float | None:
    """JSON-safe float: NaN/inf -> None."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return round(v, nd) if math.isfinite(v) else None


def _corr(a: np.ndarray, b: np.ndarray) -> float | None:
    if a.size < 3 or np.std(a) == 0 or np.std(b) == 0:
        return None
    c = float(np.corrcoef(a, b)[0, 1])
    return c if math.isfinite(c) else None


def _rank_ic(values: np.ndarray, fwds: np.ndarray) -> float | None:
    """Spearman = Pearson on average ranks (pandas .rank + numpy corrcoef; no scipy)."""
    if values.size < 3:
        return None
    rv = pd.Series(values).rank(method="average").to_numpy()
    rr = pd.Series(fwds).rank(method="average").to_numpy()
    return _corr(rv, rr)


def _wilson_lb(hits: int, n: int, z: float = _Z95) -> float | None:
    if n <= 0:
        return None
    p = hits / n
    denom = 1.0 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt((p * (1 - p) + z * z / (4 * n)) / n)
    return max(0.0, (centre - margin) / denom)


def _two_sided_p(t: float | None) -> float | None:
    """Asymptotic two-sided normal p for an IC t-stat (math.erfc; flagged for small N)."""
    if t is None or not math.isfinite(t):
        return None
    return min(1.0, math.erfc(abs(t) / math.sqrt(2.0)))


class SignalEngine:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        # No request-scoped DB session: each F0 read uses its own session_scope()
        # (concurrent asyncpg reads can't share one session — the risk.py precedent).
        self._redis = redis
        self._settings = settings
        # Reuse the risk lens' WF universe + symbol resolution (same book, same map);
        # the seed is a demo over exactly the holdings the risk lens prices.
        self._risk = RiskEngine(redis, settings)

    # ── per-series F0 fetch (own session, bounded concurrency) ──
    async def _fetch_series(
        self, symbol: str, source: str, start: datetime, end: datetime
    ) -> tuple[list[str], list[float], bool]:
        async with session_scope() as session:
            adapter = build_adapter(source, self._redis, self._settings)
            env = await HistoryStore(session, self._redis, self._settings).get_daily(
                adapter, symbol, start, end
            )
        return (
            [c.ts.date().isoformat() for c in env.data],
            [float(c.close) for c in env.data],
            bool(env.stale),
        )

    async def _gather(
        self, pairs: list[tuple[str, str]], start: datetime, end: datetime
    ) -> dict[tuple[str, str], tuple[list[str], list[float], bool] | str | None]:
        sem = asyncio.Semaphore(self._settings.signal_fetch_concurrency)

        async def one(symbol: str, source: str):
            async with sem:
                try:
                    return (symbol, source), await self._fetch_series(symbol, source, start, end)
                except (RateLimitExceeded, UpstreamError) as exc:
                    logger.warning("signals F0 TRANSIENT fetch for %s/%s: %s", source, symbol, exc)
                    return (symbol, source), "TRANSIENT"
                except Exception as exc:  # genuine no-data → uncovered
                    logger.info("signals F0 fetch failed for %s/%s: %s", source, symbol, exc)
                    return (symbol, source), None

        return dict(await asyncio.gather(*(one(s, src) for s, src in pairs)))

    # ── emission (idempotent upsert) ─────────────────────────
    async def emit(self, rows: list[dict[str, Any]]) -> EmitResult:
        # DEMO_MODE: no writes to the live signal book (also the seam
        # for GET /lab/scan?emit=true, which calls this method directly).
        if self._settings.demo_mode:
            raise DemoModeReadOnly()
        if not rows:
            raise InvalidRequest("no signal rows supplied")
        # Bound a single emission so one POST can't OOM the upsert (single-user LAN
        # app, but the emit surface is generic — future strategies/scanners post here).
        if len(rows) > 50_000:
            raise InvalidRequest("too many signal rows in one request (max 50000)")
        values: list[dict[str, Any]] = []
        ids: set[str] = set()
        for r in rows:
            try:
                sid = str(r["signal_id"]).strip()
                sym = str(r["symbol"]).strip().upper()
                src = str(r.get("source") or self._settings.price_source_default).strip()
                ad = r["as_of_date"]
                as_of = ad if isinstance(ad, date) else date.fromisoformat(str(ad))
                val = float(r["value"])
            except (KeyError, ValueError, TypeError) as exc:
                raise InvalidRequest(f"invalid signal row: {exc}")
            if not sid or not sym or not math.isfinite(val):
                raise InvalidRequest("signal_id, symbol required and value must be finite")
            meta = r.get("meta") or {}
            try:
                if len(json.dumps(meta, default=str)) > _MAX_META_BYTES:
                    raise InvalidRequest(f"signal meta exceeds {_MAX_META_BYTES} bytes")
            except (TypeError, ValueError):
                raise InvalidRequest("signal meta is not JSON-serialisable")
            ids.add(sid)
            values.append({
                "signal_id": sid, "as_of_date": as_of, "symbol": sym, "source": src,
                "value": val, "meta": meta,
            })
        # Dedup within the batch (last write wins): Postgres ON CONFLICT can't resolve
        # two rows sharing the conflict key in one INSERT (CardinalityViolation → 500),
        # so collapse to one row per (signal_id, as_of_date, symbol) before the upsert.
        deduped: dict[tuple[str, date, str], dict[str, Any]] = {}
        for v in values:
            deduped[(v["signal_id"], v["as_of_date"], v["symbol"])] = v
        values = list(deduped.values())
        await self._upsert(values)
        return EmitResult(n_received=len(rows), n_written=len(values), signal_ids=sorted(ids))

    async def _upsert(self, values: list[dict[str, Any]]) -> None:
        if not values:
            return
        async with session_scope() as session:
            stmt = insert(SignalRow).values(values)
            stmt = stmt.on_conflict_do_update(
                constraint="uq_signals_id_date_symbol",
                set_={
                    "value": stmt.excluded.value,
                    "source": stmt.excluded.source,
                    "meta": stmt.excluded.meta,
                    "created_at": func.now(),
                },
            )
            await session.execute(stmt)
            await session.commit()

    # ── catalog ──────────────────────────────────────────────
    async def catalog(self) -> SignalCatalog:
        # DEMO_MODE: the signals table is seeded from the live Wealthfolio
        # book (SignalRow.symbol) — never list it; empty is honest and leak-free.
        if self._settings.demo_empty_reads:
            return SignalCatalog(signals=[])
        async with session_scope() as session:
            stmt = (
                select(
                    SignalRow.signal_id,
                    func.count().label("n_rows"),
                    func.count(func.distinct(SignalRow.symbol)).label("n_symbols"),
                    func.count(func.distinct(SignalRow.as_of_date)).label("n_dates"),
                    func.min(SignalRow.as_of_date).label("first_date"),
                    func.max(SignalRow.as_of_date).label("last_date"),
                )
                .group_by(SignalRow.signal_id)
                .order_by(SignalRow.signal_id)
            )
            res = (await session.execute(stmt)).all()
        return SignalCatalog(signals=[
            SignalCatalogEntry(
                signal_id=r.signal_id, n_rows=r.n_rows, n_symbols=r.n_symbols, n_dates=r.n_dates,
                first_date=r.first_date.isoformat() if r.first_date else None,
                last_date=r.last_date.isoformat() if r.last_date else None,
            ) for r in res
        ])

    async def _signature(self, signal_id: str) -> str:
        async with session_scope() as session:
            row = (await session.execute(
                select(func.count(), func.max(SignalRow.as_of_date), func.max(SignalRow.created_at))
                .where(SignalRow.signal_id == signal_id)
            )).one()
        return f"{row[0]}:{row[1]}:{row[2]}"

    async def _load_rows(self, signal_id: str) -> list[tuple[str, str, str, float]]:
        # DEMO_MODE: shared detail-read chokepoint for grade()/cross_section() —
        # both already raise NotFound on an empty result, so this alone gives the
        # same 404 a real "unknown signal_id" produces, without a new error shape.
        if self._settings.demo_empty_reads:
            return []
        async with session_scope() as session:
            res = (await session.execute(
                select(SignalRow.as_of_date, SignalRow.symbol, SignalRow.source, SignalRow.value)
                .where(SignalRow.signal_id == signal_id)
                .order_by(SignalRow.as_of_date, SignalRow.symbol)
                .limit(_MAX_SIGNAL_ROWS + 1)
            )).all()
        if len(res) > _MAX_SIGNAL_ROWS:
            # Bound worst-case memory + F0 fan-out for one (possibly flooded) signal_id.
            raise InvalidRequest(f"signal '{signal_id}' has too many rows to grade (> {_MAX_SIGNAL_ROWS})")
        return [(r.as_of_date.isoformat(), r.symbol, r.source, float(r.value)) for r in res]

    # ── lazy grade (cached) ──────────────────────────────────
    async def grade(
        self, signal_id: str, horizon: int | None = None, quantiles: int | None = None,
        refresh: bool = False,
    ) -> tuple[SignalGrade, dict[str, Any]]:
        s = self._settings
        h = int(horizon or s.signal_default_horizon_bars)
        q = int(quantiles or s.signal_default_quantiles)
        if h < 1 or q < 2:
            raise InvalidRequest("horizon must be ≥1 and quantiles ≥2")

        rows = await self._load_rows(signal_id)
        if not rows:
            raise NotFound(f"no signals logged for '{signal_id}'")

        today = datetime.now(timezone.utc).date()
        sig = await self._signature(signal_id)
        # Hash the (caller-influenced) signal_id into the key so a crafted id with ':'
        # can't collide with another signal's key segments.
        sid_key = hashlib.sha256(signal_id.encode()).hexdigest()[:16]
        key = f"signal:grade:{sid_key}:{h}:{q}:{sig}:{today.isoformat()}"
        if not refresh:
            cached = await self._redis.get(key)
            if cached is not None:
                try:
                    payload = SignalGrade.model_validate_json(cached)
                    return payload, _meta(payload)
                except Exception as exc:
                    logger.debug("signals grade cache deserialise failed: %s", exc)

        pairs = sorted({(sym, src) for _, sym, src, _ in rows})
        min_date = date.fromisoformat(min(r[0] for r in rows))
        start = datetime.combine(min_date - timedelta(days=7), datetime.min.time(), tzinfo=timezone.utc)
        end = datetime.now(timezone.utc)
        series = await self._gather(pairs, start, end)

        grade, transient = await asyncio.to_thread(
            _compute_grade, signal_id, rows, series, h, q,
            s.signal_settle_days, s.signal_min_names, s.signal_min_dates, today,
        )
        ttl = s.signal_grade_ttl_seconds
        if transient:
            ttl = min(ttl, 300)  # don't freeze a rate-limited snapshot (risk.py self-heal)
        try:
            await self._redis.set(key, grade.model_dump_json(), ex=ttl)
        except Exception as exc:
            logger.warning("signals grade cache write failed: %s", exc)
        return grade, _meta(grade, transient)

    async def scoreboard(
        self, horizon: int | None = None, quantiles: int | None = None
    ) -> Scoreboard:
        cat = await self.catalog()
        h = int(horizon or self._settings.signal_default_horizon_bars)
        q = int(quantiles or self._settings.signal_default_quantiles)
        summaries: list[SignalGradeSummary] = []
        warnings: set[str] = set()
        skipped: list[str] = []
        for entry in cat.signals:
            try:
                g, _ = await self.grade(entry.signal_id, h, q)
            except (NotFound, InvalidRequest, UpstreamError, RateLimitExceeded) as exc:
                # A signal that can't be graded (gone / too large / upstream down) is
                # surfaced, not silently dropped; anything else propagates (real bug).
                logger.info("scoreboard skipped %s: %s", entry.signal_id, exc)
                skipped.append(entry.signal_id)
                continue
            warnings.update(g.warnings)
            summaries.append(SignalGradeSummary(
                signal_id=g.signal_id, horizon_bars=g.horizon_bars, methodology=g.methodology,
                n_dates_graded=g.n_dates_graded, mean_rank_ic=g.mean_rank_ic, icir=g.icir,
                t_stat=g.t_stat, significant=g.significant, low_confidence=g.low_confidence,
                ls_spread_mean_pct=g.ls_spread_mean_pct, ls_cumulative_pct=g.ls_cumulative_pct,
                hit_rate=g.hit_rate, hit_rate_wilson_lb=g.hit_rate_wilson_lb,
                n_pairs_matured=g.coverage.n_pairs_matured, n_pairs_pending=g.coverage.n_pairs_pending,
            ))
        summaries.sort(key=lambda r: (r.icir if r.icir is not None else -1e9), reverse=True)
        if skipped:
            warnings.add(f"{len(skipped)} signal(s) skipped (ungradeable): {', '.join(sorted(skipped))}")
        return Scoreboard(
            as_of=datetime.now(timezone.utc).isoformat(), horizon_bars=h, quantiles=q,
            rows=summaries, warnings=sorted(warnings),
        )

    async def cross_section(
        self, signal_id: str, as_of: str, horizon: int | None = None
    ) -> CrossSection:
        """Auditable raw view of one decision date: the lookahead rule made visible."""
        h = int(horizon or self._settings.signal_default_horizon_bars)
        try:
            t = date.fromisoformat(as_of)
        except ValueError:
            raise InvalidRequest("as_of must be YYYY-MM-DD")
        rows = [r for r in await self._load_rows(signal_id) if r[0] == as_of]
        if not rows:
            raise NotFound(f"no '{signal_id}' cross-section on {as_of}")
        pairs = sorted({(sym, src) for _, sym, src, _ in rows})
        start = datetime.combine(t - timedelta(days=7), datetime.min.time(), tzinfo=timezone.utc)
        series = await self._gather(pairs, start, datetime.now(timezone.utc))
        out, n_m, n_p, n_u = await asyncio.to_thread(
            _cross_section_rows, rows, series, h, self._settings.signal_settle_days,
        )
        return CrossSection(
            signal_id=signal_id, as_of_date=as_of, horizon_bars=h,
            settle_days=self._settings.signal_settle_days,
            n_matured=n_m, n_pending=n_p, n_uncovered=n_u, rows=out,
        )

    def _resolve_pairs(self, assets: list[tuple[str, str]]) -> list[tuple[str, str]]:
        """[(symbol, asset_class)] -> sorted F0-priceable (price_symbol, source);
        reuses the risk lens map."""
        resolved: set[tuple[str, str]] = set()
        for sym, cls in assets:
            r = self._risk._resolve(sym, cls)
            if r.get("skip") or not r.get("price_symbol"):
                continue
            resolved.add((r["price_symbol"], r["source"]))
        return sorted(resolved)

    def history_targets(self, assets: list[tuple[str, str]]) -> list[tuple[Any, str]]:
        """[(adapter, price_symbol)] the reference seed reads daily bars through
        (holdings + equity benchmark); the demo warm job stores these keys."""
        pairs = self._resolve_pairs(assets) + [(self._settings.risk_bench_equity, "yfinance")]
        return [(build_adapter(src, self._redis, self._settings), sym) for sym, src in sorted(set(pairs))]

    # ── reference seed (two real F0 factors over the WF book) ─
    async def seed(self, refresh: bool = False) -> EmitResult:
        if self._settings.demo_mode:
            raise DemoModeReadOnly()
        if not self._settings.signal_seed_enabled:
            raise InvalidRequest("seeding disabled (SIGNAL_SEED_ENABLED=false)")
        s = self._settings
        holdings = await self._risk._wf.holdings()
        resolved = self._resolve_pairs([(a.symbol, a.asset_class) for a in holdings.by_asset])
        if not resolved:
            raise InvalidRequest("no resolvable holdings to seed from")

        lookback = s.signal_momentum_lookback_bars + s.signal_momentum_skip_bars
        # Read window must cover the oldest decision date minus the momentum formation
        # window. seed_months back + ~1.6 cal-days/bar of formation history.
        span_days = int(s.signal_seed_months * 31 + lookback * 1.7 + 30)
        end = datetime.now(timezone.utc)
        start = end - timedelta(days=span_days)

        # Anchor the monthly decision grid to the equity-benchmark trading calendar.
        pairs = resolved + [(s.risk_bench_equity, "yfinance")]
        series = await self._gather(sorted(set(pairs)), start, end)

        bench = series.get((s.risk_bench_equity, "yfinance"))
        if bench == "TRANSIENT":
            # The benchmark calendar was rate-limited — a retryable condition, not a
            # bad request (a truncated grid would silently under-seed; see TRACKER).
            raise UpstreamError("benchmark history rate-limited; retry the seed shortly")
        decision_dates = _month_end_grid(bench, s.signal_seed_months)
        if not decision_dates:
            raise InvalidRequest("could not build a decision-date calendar (benchmark history missing)")

        rows = await asyncio.to_thread(
            _build_reference_signals, resolved, series, decision_dates,
            s.signal_momentum_lookback_bars, s.signal_momentum_skip_bars,
            s.signal_lowvol_lookback_bars, s.signal_min_names,
        )
        await self._upsert(rows)
        ids = sorted({r["signal_id"] for r in rows})
        logger.info("signals seed: %d rows over %d dates, %d symbols → %s",
                    len(rows), len(decision_dates), len(resolved), ids)
        return EmitResult(n_received=len(rows), n_written=len(rows), signal_ids=ids)


def _meta(g: SignalGrade, transient: bool = False) -> dict[str, Any]:
    return {
        "source": "signal-engine",
        "stale": transient or any("rate-limited" in w for w in g.warnings),
        "warnings": g.warnings,
    }


# ── pure compute (runs in a thread; no async/Redis) ────────────
def _bars(raw: object) -> tuple[list[date], np.ndarray, bool] | None:
    """Parse a gathered F0 series into (sorted dates, closes, stale). None if absent."""
    if not isinstance(raw, (tuple, list)) or len(raw) < 2 or not raw[0]:
        return None
    pairs = sorted({date.fromisoformat(d): float(c) for d, c in zip(raw[0], raw[1])}.items())
    if not pairs:
        return None
    dates = [d for d, _ in pairs]
    closes = np.array([c for _, c in pairs], dtype=float)
    stale = bool(raw[2]) if len(raw) > 2 else False
    return dates, closes, stale


def _forward(
    dates: list[date], closes: np.ndarray, t: date, h: int, settle: int
) -> tuple[str, int | None, int | None, float | None]:
    """The lookahead/maturation rule. entry = first bar STRICTLY after T; exit = H
    trading-bars later; matured only if the exit bar exists AND is trailed by ≥ settle
    bars (so it isn't the still-provisional tail). Returns (status, entry, exit, fwd)."""
    n = len(dates)
    entry = bisect.bisect_right(dates, t)  # first index with date > T
    if entry >= n:
        return "uncovered", None, None, None  # no bar after the decision date
    exit_i = entry + h
    if exit_i >= n:
        return "pending", entry, None, None  # horizon not elapsed yet
    if (n - 1 - exit_i) < settle:
        return "pending", entry, exit_i, None  # exit bar not settled (provisional tail)
    ce, cx = closes[entry], closes[exit_i]
    if ce <= 0 or cx <= 0:
        return "uncovered", entry, exit_i, None
    return "matured", entry, exit_i, float(cx / ce - 1.0)


def _cross_section_rows(
    rows: list[tuple[str, str, str, float]],
    series: dict[tuple[str, str], Any],
    h: int, settle: int,
) -> tuple[list[CrossSectionRow], int, int, int]:
    out: list[CrossSectionRow] = []
    n_m = n_p = n_u = 0
    for as_of, sym, src, val in rows:
        t = date.fromisoformat(as_of)
        parsed = _bars(series.get((sym, src)))
        if parsed is None:
            n_u += 1
            out.append(CrossSectionRow(symbol=sym, value=val, status="uncovered",
                                       entry_date=None, exit_date=None, forward_return_pct=None))
            continue
        dates, closes, _ = parsed
        status, ei, xi, fwd = _forward(dates, closes, t, h, settle)
        n_m += status == "matured"
        n_p += status == "pending"
        n_u += status == "uncovered"
        out.append(CrossSectionRow(
            symbol=sym, value=val, status=status,
            entry_date=dates[ei].isoformat() if ei is not None else None,
            exit_date=dates[xi].isoformat() if xi is not None else None,
            forward_return_pct=_f(fwd * 100, 3) if fwd is not None else None,
        ))
    out.sort(key=lambda r: r.value, reverse=True)
    return out, n_m, n_p, n_u


def _compute_grade(
    signal_id: str,
    rows: list[tuple[str, str, str, float]],
    series: dict[tuple[str, str], Any],
    h: int, q: int, settle: int, min_names: int, min_dates: int, today: date,
) -> tuple[SignalGrade, bool]:
    transient = any(v == "TRANSIENT" for v in series.values())
    stale_any = False

    matured_by_date: dict[str, list[tuple[float, float]]] = {}  # as_of -> [(value, fwd)]
    n_matured = n_pending = n_uncovered = 0
    all_dates: set[str] = set()
    for as_of, sym, src, val in rows:
        all_dates.add(as_of)
        parsed = _bars(series.get((sym, src)))
        if parsed is None:
            n_uncovered += 1
            continue
        dates, closes, stale = parsed
        stale_any = stale_any or stale
        status, _ei, _xi, fwd = _forward(dates, closes, date.fromisoformat(as_of), h, settle)
        if status == "matured" and fwd is not None:
            n_matured += 1
            matured_by_date.setdefault(as_of, []).append((val, fwd))
        elif status == "pending":
            n_pending += 1
        else:
            n_uncovered += 1

    per_date: list[ICPoint] = []
    rank_ics: list[float] = []
    pearson_ics: list[float] = []
    ls_spreads: list[float] = []
    monos: list[float] = []
    bucket_sums = np.zeros(q)
    bucket_cnts = np.zeros(q)
    hits = total_hit = 0
    long_fwds: list[float] = []
    short_fwds: list[float] = []
    names_per_date: list[int] = []

    for as_of in sorted(matured_by_date):
        pairs = matured_by_date[as_of]
        vals = np.array([p[0] for p in pairs], dtype=float)
        fwds = np.array([p[1] for p in pairs], dtype=float)
        n = vals.size
        names_per_date.append(n)
        if n < min_names:
            per_date.append(ICPoint(date=as_of, n_names=n, rank_ic=None, pearson_ic=None, ls_spread_pct=None))
            continue
        # hit-rate + directional split — only over GRADED (sufficient-breadth) dates,
        # so it can't count dates the IC dropped. Both axes median-demeaned with a
        # symmetric tie exclusion (a name AT either median is neither up nor down).
        mv, mf = float(np.median(vals)), float(np.median(fwds))
        for v, fw in zip(vals, fwds):
            if v > 0:
                long_fwds.append(fw)
            elif v < 0:
                short_fwds.append(fw)
            dv, df = v - mv, fw - mf
            if dv == 0 or df == 0:
                continue
            total_hit += 1
            if (dv > 0) == (df > 0):
                hits += 1
        ric = _rank_ic(vals, fwds)
        pic = _corr(vals, fwds)
        spread = mono = None
        if n >= q:
            order = np.argsort(vals)  # ascending by score
            buckets = np.array_split(order, q)
            means = np.array([float(fwds[b].mean()) for b in buckets])
            spread = float(means[-1] - means[0])  # top minus bottom (demeaned by construction)
            mono = _corr(np.arange(q, dtype=float), means)
            bucket_sums += means
            bucket_cnts += 1
            ls_spreads.append(spread)
        if ric is not None:
            rank_ics.append(ric)
        if pic is not None:
            pearson_ics.append(pic)
        if mono is not None:
            monos.append(mono)
        per_date.append(ICPoint(
            date=as_of, n_names=n, rank_ic=_f(ric, 4), pearson_ic=_f(pic, 4),
            ls_spread_pct=_f(spread * 100, 3) if spread is not None else None,
        ))

    n_dates_graded = len(rank_ics)
    warnings: list[str] = []
    methodology = "cross-sectional"
    low_confidence = False

    # Overlapping-windows honesty: if the holding horizon exceeds the spacing between
    # decision dates, the IC series is autocorrelated and the naive t-stat is
    # anti-conservative — so it must VETO the significance verdict, not just warn.
    graded_dates = sorted(date.fromisoformat(p.date) for p in per_date if p.rank_ic is not None)
    overlapping = False
    if len(graded_dates) >= 2:
        gaps = [(graded_dates[i + 1] - graded_dates[i]).days for i in range(len(graded_dates) - 1)]
        median_gap = float(np.median(gaps)) if gaps else 0.0
        if median_gap and (h * 1.4) > median_gap:
            overlapping = True
            warnings.append("overlapping forward windows (H > rebalance gap) — naive t-stat is anti-conservative")

    mean_rank_ic = mean_ic = icir = t_stat = p_value = None
    significant = False
    if n_dates_graded == 0:
        # No date has the breadth for a cross-sectional IC → pooled fallback over all
        # matured pairs (loudly flagged — pooled mixes dates, the t-stat is not honest).
        pool = [p for ps in matured_by_date.values() for p in ps]
        if pool:
            pv = np.array([p[0] for p in pool]); pf = np.array([p[1] for p in pool])
            mean_rank_ic = _f(_rank_ic(pv, pf), 4)
            mean_ic = _f(_corr(pv, pf), 4)
        methodology = "pooled (non-independent; indicative only)"
        low_confidence = True
        warnings.append("insufficient cross-sectional breadth — pooled IC shown (indicative only)")
    else:
        mean_rank_ic = _f(float(np.mean(rank_ics)), 4)
        mean_ic = _f(float(np.mean(pearson_ics)), 4) if pearson_ics else None
        if n_dates_graded >= 2:
            sd = float(np.std(rank_ics, ddof=1))
            if sd > 0:
                icir = _f(float(np.mean(rank_ics)) / sd, 4)
                t_stat = _f(icir * math.sqrt(n_dates_graded), 4) if icir is not None else None
                p_value = _f(_two_sided_p(t_stat), 5)
        low_confidence = n_dates_graded < min_dates
        significant = bool(
            t_stat is not None and abs(t_stat) >= 2.0 and not low_confidence and not overlapping
        )
        if low_confidence:
            warnings.append(f"only {n_dates_graded} graded cross-sections (< {min_dates}) — ICIR/t-stat indicative, no significance verdict")

    ls_mean = ls_t = ls_cum = None
    if ls_spreads:
        arr = np.array(ls_spreads)
        ls_mean = _f(float(arr.mean()) * 100, 3)
        if arr.size >= 2 and float(arr.std(ddof=1)) > 0:
            ls_t = _f(float(arr.mean()) / (float(arr.std(ddof=1)) / math.sqrt(arr.size)), 3)
        if np.all(arr > -1.0):
            # compounded top-minus-bottom equity (log-domain for stability)
            ls_cum = _f(float(np.expm1(np.log1p(arr).sum())) * 100, 3)
        else:
            # a date's L/S spread breached -100% (a bottom bucket out-rallied the top by
            # >1×) — compounding would flip sign / fall below -100%; report the
            # arithmetic sum and flag instead of a nonsensical equity curve.
            ls_cum = _f(float(arr.sum()) * 100, 3)
            warnings.append("L/S spread breached -100% on some date; cumulative shown as arithmetic sum")

    quantile_buckets = [
        QuantileBucket(
            bucket=i + 1,
            mean_forward_return_pct=_f(bucket_sums[i] / bucket_cnts[i] * 100, 3) if bucket_cnts[i] else None,
            n_obs=int(bucket_cnts[i]),
        )
        for i in range(q)
    ]

    if stale_any:
        warnings.append("some F0 series served stale (upstream degraded; last-good bars used)")
    if transient:
        warnings.append("some F0 reads were rate-limited; coverage temporarily reduced (self-heals)")
    if n_matured == 0:
        warnings.append("no matured windows yet — every signal's forward horizon is still pending")
    # Caveats specific to the seeded demo factors (don't slander a real external signal).
    if signal_id.startswith("xs_"):
        warnings.append(_SURVIVORSHIP)
        warnings.append(_LUMPY)

    coverage = SignalCoverage(
        n_rows=len(rows), n_pairs_matured=n_matured, n_pairs_pending=n_pending,
        n_pairs_uncovered=n_uncovered, n_dates_total=len(all_dates),
        n_dates_graded=n_dates_graded,
        mean_names_per_date=_f(float(np.mean(names_per_date)), 2) if names_per_date else None,
        window_start=min(matured_by_date) if matured_by_date else None,
        window_end=max(matured_by_date) if matured_by_date else None,
        by_status={"matured": n_matured, "pending": n_pending, "uncovered": n_uncovered},
    )

    grade = SignalGrade(
        signal_id=signal_id, as_of=datetime.now(timezone.utc).isoformat(),
        horizon_bars=h, quantiles=q, settle_days=settle, methodology=methodology,
        n_dates_graded=n_dates_graded, mean_rank_ic=mean_rank_ic, mean_ic=mean_ic,
        icir=icir, t_stat=t_stat, p_value=p_value, significant=significant,
        low_confidence=low_confidence, ls_spread_mean_pct=ls_mean, ls_spread_t=ls_t,
        ls_cumulative_pct=ls_cum, monotonicity=_f(float(np.mean(monos)), 4) if monos else None,
        hit_rate=_f(hits / total_hit, 4) if total_hit else None,
        hit_rate_wilson_lb=_f(_wilson_lb(hits, total_hit), 4),
        n_hit_pairs=total_hit,
        long_mean_fwd_pct=_f(float(np.mean(long_fwds)) * 100, 3) if long_fwds else None,
        short_mean_fwd_pct=_f(float(np.mean(short_fwds)) * 100, 3) if short_fwds else None,
        per_date=per_date, quantile_buckets=quantile_buckets, coverage=coverage, warnings=warnings,
    )
    return grade, transient


def _month_end_grid(bench_raw: object, n_months: int) -> list[date]:
    """Last trading bar of each month from the benchmark calendar; the most recent
    n_months of them (the monthly decision grid that anchors the seed)."""
    parsed = _bars(bench_raw)
    if parsed is None:
        return []
    dates, _, _ = parsed
    by_month: dict[tuple[int, int], date] = {}
    for d in dates:  # ascending → last write per month wins = month-end trading day
        by_month[(d.year, d.month)] = d
    months = sorted(by_month.values())
    return months[-n_months:]


def _zscore_rows(
    signal_id: str, t: date, raw: dict[tuple[str, str], float], min_names: int,
    meta_extra: dict[str, Any],
) -> list[dict[str, Any]]:
    """Cross-sectionally z-score one factor on date T → one signal row per symbol."""
    if len(raw) < min_names:
        return []
    keys = list(raw)
    vals = np.array([raw[k] for k in keys], dtype=float)
    mu = float(vals.mean())
    sd = float(vals.std(ddof=0))
    if sd == 0 or not math.isfinite(sd):
        return []
    out: list[dict[str, Any]] = []
    for k, v in zip(keys, vals):
        sym, src = k
        out.append({
            "signal_id": signal_id, "as_of_date": t, "symbol": sym, "source": src,
            "value": float((v - mu) / sd),
            "meta": {**meta_extra, "raw": _f(v, 6), "universe_size": len(keys)},
        })
    return out


def _build_reference_signals(
    resolved: list[tuple[str, str]],
    series: dict[tuple[str, str], Any],
    decision_dates: list[date],
    mom_lb: int, mom_skip: int, lowvol_lb: int, min_names: int,
) -> list[dict[str, Any]]:
    """Two REAL F0-computed factors over the WF universe, lookahead-clean (every input
    close ≤ T): 12-1 cross-sectional momentum (xs_mom_252_21) and a low-vol anomaly
    (xs_lowvol_63). Cross-sectionally z-scored per date so scores are comparable."""
    parsed: dict[tuple[str, str], tuple[list[date], np.ndarray, bool]] = {}
    for key in resolved:
        p = _bars(series.get(key))
        if p is not None:
            parsed[key] = p

    rows: list[dict[str, Any]] = []
    for t in decision_dates:
        mom_raw: dict[tuple[str, str], float] = {}
        vol_raw: dict[tuple[str, str], float] = {}
        for key, (dates, closes, _) in parsed.items():
            i = bisect.bisect_right(dates, t) - 1  # last bar with date ≤ T
            if i < 0:
                continue
            a, b = i - mom_skip, i - mom_skip - mom_lb
            if b >= 0 and closes[b] > 0 and closes[a] > 0:
                mom_raw[key] = float(closes[a] / closes[b] - 1.0)
            if i - lowvol_lb >= 0:
                window = closes[i - lowvol_lb : i + 1]
                rets = np.diff(window) / window[:-1]
                if rets.size >= 2:
                    sd = float(np.std(rets, ddof=1))
                    if math.isfinite(sd):
                        vol_raw[key] = -sd  # low vol = high score
        rows += _zscore_rows("xs_mom_252_21", t, mom_raw, min_names, {"lookback": mom_lb, "skip": mom_skip})
        rows += _zscore_rows("xs_lowvol_63", t, vol_raw, min_names, {"lookback": lowvol_lb})
    return rows


# ════════════════════════════════════════════════════════════════
# Technical scanners — point-in-time chart-pattern
# detectors over the F0 OHLCV backbone. Pure numpy, synchronous in
# the main API (no heavy worker needed) — same bounded-gather F0
# read pattern as the signal grader. Each detection carries a
# directional bias so it can OPTIONALLY be POSTed as a Phase-9
# signal (signal_id = `scan_<pattern>`, value = ±1) and graded by
# the same Rank-IC machinery.
# ════════════════════════════════════════════════════════════════

# pattern key -> human label (the scanner catalog)
SCAN_PATTERNS: dict[str, str] = {
    "engulfing": "Engulfing candle",
    "hammer_star": "Hammer / shooting star",
    "keltner_break": "Keltner channel break",
    "volume_spike": "Volume spike",
    "crash": "Crash / capitulation",
    "moonshot": "Moonshot / blow-off",
    "bull_trap": "Bull trap (failed breakout)",
    "bear_trap": "Bear trap (failed breakdown)",
}


class ScanPattern(BaseModel):
    name: str  # pattern key
    label: str
    bias: int  # +1 bullish, -1 bearish
    detail: str


class ScanRow(BaseModel):
    symbol: str
    label: str
    close: float | None
    score: int  # net bias of all matched patterns (sum of ±1)
    patterns: list[ScanPattern]


class ScanReport(BaseModel):
    as_of: str | None
    n_symbols: int
    n_hits: int  # symbols with ≥1 matched pattern
    requested: list[str]
    rows: list[ScanRow]
    emitted: int  # signal rows POSTed (0 unless emit=True)
    warnings: list[str]


def _ema(a: np.ndarray, n: int) -> np.ndarray:
    alpha = 2.0 / (n + 1.0)
    out = np.empty_like(a, dtype=float)
    out[0] = a[0]
    for i in range(1, a.size):
        out[i] = alpha * a[i] + (1 - alpha) * out[i - 1]
    return out


def _atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, n: int) -> np.ndarray:
    prev_close = np.concatenate(([close[0]], close[:-1]))
    tr = np.maximum.reduce([high - low, np.abs(high - prev_close), np.abs(low - prev_close)])
    # simple rolling-mean ATR (Wilder-equivalent enough for a screen)
    atr = np.full(tr.size, np.nan)
    if tr.size >= n:
        c = np.cumsum(tr)
        atr[n - 1:] = np.concatenate(([c[n - 1]], c[n:] - c[:-n])) / n
    return atr


def _detect(o: np.ndarray, h: np.ndarray, l: np.ndarray, c: np.ndarray, v: np.ndarray,
            wanted: set[str]) -> list[ScanPattern]:
    """Run the requested detectors on one symbol's recent OHLCV arrays (ascending)."""
    pats: list[ScanPattern] = []
    n = c.size
    if n < 25:
        return pats
    body = c - o
    rng = np.where((h - l) == 0, np.nan, h - l)

    if "engulfing" in wanted:
        if body[-2] < 0 and body[-1] > 0 and c[-1] >= o[-2] and o[-1] <= c[-2]:
            pats.append(ScanPattern(name="engulfing", label=SCAN_PATTERNS["engulfing"],
                                    bias=1, detail="bullish engulfing"))
        elif body[-2] > 0 and body[-1] < 0 and o[-1] >= c[-2] and c[-1] <= o[-2]:
            pats.append(ScanPattern(name="engulfing", label=SCAN_PATTERNS["engulfing"],
                                    bias=-1, detail="bearish engulfing"))

    if "hammer_star" in wanted and np.isfinite(rng[-1]):
        upper = h[-1] - max(o[-1], c[-1])
        lower = min(o[-1], c[-1]) - l[-1]
        bodyabs = abs(body[-1])
        if lower > 2 * bodyabs and upper < bodyabs:
            pats.append(ScanPattern(name="hammer_star", label=SCAN_PATTERNS["hammer_star"],
                                    bias=1, detail="hammer (long lower wick)"))
        elif upper > 2 * bodyabs and lower < bodyabs:
            pats.append(ScanPattern(name="hammer_star", label=SCAN_PATTERNS["hammer_star"],
                                    bias=-1, detail="shooting star (long upper wick)"))

    if "keltner_break" in wanted:
        ema = _ema(c, 20)
        atr = _atr(h, l, c, 10)
        if np.isfinite(atr[-1]):
            upper, lower = ema[-1] + 2 * atr[-1], ema[-1] - 2 * atr[-1]
            if c[-1] > upper:
                pats.append(ScanPattern(name="keltner_break", label=SCAN_PATTERNS["keltner_break"],
                                        bias=1, detail="close above upper Keltner band"))
            elif c[-1] < lower:
                pats.append(ScanPattern(name="keltner_break", label=SCAN_PATTERNS["keltner_break"],
                                        bias=-1, detail="close below lower Keltner band"))

    if "volume_spike" in wanted and v[-21:-1].size >= 5:
        base = float(np.nanmean(v[-21:-1]))
        if base > 0 and v[-1] > 2 * base:
            bias = 1 if c[-1] >= c[-2] else -1
            pats.append(ScanPattern(name="volume_spike", label=SCAN_PATTERNS["volume_spike"],
                                    bias=bias, detail=f"volume {v[-1]/base:.1f}× 20d average"))

    ret1 = c[-1] / c[-2] - 1.0
    ret20 = c[-1] / c[-21] - 1.0 if n > 21 else 0.0
    if "crash" in wanted and (ret1 < -0.05 or ret20 < -0.15):
        pats.append(ScanPattern(name="crash", label=SCAN_PATTERNS["crash"], bias=-1,
                                detail=f"1d {ret1:+.1%}, 20d {ret20:+.1%}"))
    if "moonshot" in wanted and (ret1 > 0.05 or ret20 > 0.25):
        pats.append(ScanPattern(name="moonshot", label=SCAN_PATTERNS["moonshot"], bias=1,
                                detail=f"1d {ret1:+.1%}, 20d {ret20:+.1%}"))

    if n > 24:
        prior_high = float(np.max(h[-24:-3]))
        prior_low = float(np.min(l[-24:-3]))
        if "bull_trap" in wanted and h[-3:].max() > prior_high and c[-1] < prior_high:
            pats.append(ScanPattern(name="bull_trap", label=SCAN_PATTERNS["bull_trap"], bias=-1,
                                    detail="broke above 20d high then closed back below"))
        if "bear_trap" in wanted and l[-3:].min() < prior_low and c[-1] > prior_low:
            pats.append(ScanPattern(name="bear_trap", label=SCAN_PATTERNS["bear_trap"], bias=1,
                                    detail="broke below 20d low then closed back above"))
    return pats


class ScannerEngine:
    """Technical chart-pattern scanner over the F0 OHLCV backbone.

    Read-only + synchronous (cached): gathers recent daily bars for a universe and
    runs the requested detectors. Detections optionally feed Phase-9 signal scoring
    via `emit` (POSTed as `scan_<pattern>` signals with value = ±1)."""

    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings

    def _universe(self, explicit: list[str] | None) -> list[tuple[str, str]]:
        cap = self._settings.lab_max_symbols
        if explicit:
            return [(s.upper(), s.upper()) for s in explicit][:cap]
        return self._settings.lab_universe_map[:cap]

    async def _fetch_ohlcv(self, symbol: str, source: str, start: datetime, end: datetime):
        async with session_scope() as session:
            adapter = build_adapter(source, self._redis, self._settings)
            env = await HistoryStore(session, self._redis, self._settings).get_daily(
                adapter, symbol, start, end
            )
        rows = sorted({c.ts.date(): c for c in env.data}.items())
        if not rows:
            return None
        o = np.array([float(c.open) for _, c in rows])
        h = np.array([float(c.high) for _, c in rows])
        l = np.array([float(c.low) for _, c in rows])
        cl = np.array([float(c.close) for _, c in rows])
        v = np.array([float(c.volume) if c.volume is not None else np.nan for _, c in rows])
        return rows[-1][0], o, h, l, cl, v, bool(env.stale)

    async def scan(self, patterns: list[str] | None, universe: list[str] | None,
                   emit: bool = False) -> ScanReport:
        wanted = set(patterns) if patterns else set(SCAN_PATTERNS)
        bad = wanted - set(SCAN_PATTERNS)
        if bad:
            raise InvalidRequest(f"unknown pattern(s): {', '.join(sorted(bad))}")
        uni = self._universe(universe)
        source = self._settings.price_source_default
        start = datetime.now(timezone.utc) - timedelta(days=200)
        end = datetime.now(timezone.utc)

        sem = asyncio.Semaphore(self._settings.signal_fetch_concurrency)
        warnings: list[str] = []

        async def one(symbol: str, label: str):
            async with sem:
                try:
                    data = await self._fetch_ohlcv(symbol, source, start, end)
                except (RateLimitExceeded, UpstreamError):
                    return label, symbol, None, True
                except Exception as exc:  # noqa: BLE001
                    logger.info("scan F0 fetch failed %s: %s", symbol, exc)
                    return label, symbol, None, False
                return label, symbol, data, False

        results = await asyncio.gather(*(one(s, lbl) for s, lbl in uni))

        rows: list[ScanRow] = []
        as_of: str | None = None
        stale_any = False
        emit_rows: list[dict[str, Any]] = []
        for label, symbol, data, transient in results:
            if transient:
                stale_any = True
            if data is None:
                continue
            last_date, o, h, l, cl, v, stale = data
            stale_any = stale_any or stale
            as_of = max(as_of, last_date.isoformat()) if as_of else last_date.isoformat()
            pats = _detect(o, h, l, cl, v, wanted)
            if not pats:
                continue
            score = sum(p.bias for p in pats)
            rows.append(ScanRow(symbol=symbol, label=label, close=round(float(cl[-1]), 4),
                                score=score, patterns=pats))
            if emit:
                for p in pats:
                    emit_rows.append({
                        "signal_id": f"scan_{p.name}", "symbol": symbol, "source": source,
                        "as_of_date": last_date, "value": float(p.bias),
                        "meta": {"detail": p.detail, "label": p.label},
                    })

        emitted = 0
        if emit and emit_rows:
            res = await SignalEngine(self._redis, self._settings).emit(emit_rows)
            emitted = res.n_written
        if stale_any:
            warnings.append("some F0 series were stale or rate-limited; scan is partial")

        rows.sort(key=lambda r: abs(r.score), reverse=True)
        return ScanReport(
            as_of=as_of, n_symbols=len(uni), n_hits=len(rows), requested=sorted(wanted),
            rows=rows, emitted=emitted, warnings=warnings,
        )
