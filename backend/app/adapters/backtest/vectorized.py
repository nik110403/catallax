"""Engine #1 — a reimplemented numpy/pandas cross-sectional quantile-rebalance.

The strategy: each rebalance date T, rank the logged signal's cross-section, hold
the top quantile (and short the bottom iff `long_short`) until the next rebalance.

LOOKAHEAD DISCIPLINE (the cardinal rule): the entry bar is the FIRST master-calendar
bar STRICTLY after T — `bisect.bisect_right(master, T)`, the exact primitive the
proven signal grader (`services/signals.py::_forward`) uses. Weights apply only to
returns from that entry onward; the rebalance bar's own close is never traded on.
The unsettled tail (the final bars within `settle_days` of the data end, where
`daily_bars` may still restate a provisional close) is EXCLUDED from the curve and
flagged — matching the grader's maturation rule. There is no second timing path.

Accounting: fixed target weights held between rebalances (daily-rebalanced-to-basket),
so the portfolio daily return is the weighted mean of held names' daily returns;
transaction costs = `costs_bps` on the two-way weight turnover, deducted on the entry
bar; reported turnover is one-way. Honesty caveats (thin/dominated book, synthetic
benchmark, survivorship, dropped tail, unmodelled shorts) ride the report warnings.
"""
from __future__ import annotations

import bisect
import math
from datetime import date

import numpy as np
import pandas as pd

from app.adapters.backtest.base import (
    BacktestConfig,
    BacktestEngineAdapter,
    BacktestMetrics,
    BacktestReport,
    DrawdownPoint,
    EquityPoint,
    Panel,
    ProgressFn,
    RankICFn,
    RebalancePoint,
)


def _f(x: object, nd: int = 6) -> float | None:
    try:
        v = float(x)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return round(v, nd) if math.isfinite(v) else None


def _period_key(d: date, freq: str) -> tuple[int, int]:
    if freq == "W":
        iso = d.isocalendar()
        return (iso[0], iso[1])
    if freq == "Q":
        return (d.year, (d.month - 1) // 3)
    return (d.year, d.month)  # "M"


def _downsample(dates: list[date], freq: str) -> list[date]:
    """Keep the last decision date in each W/M/Q period (ascending in → ascending out)."""
    by_period: dict[tuple[int, int], date] = {}
    for d in dates:
        by_period[_period_key(d, freq)] = d  # last write per period wins
    return sorted(by_period.values())


def _native_fwd(panels, cross, t, exit_date):
    """IC inputs priced on each symbol's OWN native bars (no ffill): entry = first
    native bar STRICTLY after T, exit = last native bar on/before the holding-span end.
    Mirrors the signal grader's strictly-after-T rule, so the Rank-IC has no one-bar
    weekend/holiday leak the ffilled master matrix would introduce on mixed calendars."""
    scores: list[float] = []
    fwds: list[float] = []
    for sym, score in cross.items():
        p = panels.get(sym)
        if not p:
            continue
        sdates, scloses = p
        ent = bisect.bisect_right(sdates, t)  # first native bar strictly after T
        if ent >= len(sdates):
            continue
        ex = bisect.bisect_right(sdates, exit_date) - 1  # last native bar ≤ span end
        if ex <= ent:
            continue
        pe, px = float(scloses[ent]), float(scloses[ex])
        if pe > 0 and px > 0:
            scores.append(float(score))
            fwds.append(px / pe - 1.0)
    return np.array(scores), np.array(fwds)


class VectorizedEngine(BacktestEngineAdapter):
    name = "vectorized"

    def run(
        self,
        config: BacktestConfig,
        panels: dict[str, Panel],
        signal_rows: list[tuple[date, str, float]],
        benchmark_panel: Panel | None,
        settle_days: int,
        rank_ic: RankICFn,
        progress: ProgressFn | None = None,
    ) -> BacktestReport:
        warnings: list[str] = []
        if progress:
            progress(0.55, "ranking cross-sections")

        # ── decision grid: the signal's own as_of_dates in-window, downsampled ──
        by_date: dict[date, dict[str, float]] = {}
        for d, sym, val in signal_rows:
            if config.start <= d <= config.end and sym in panels and math.isfinite(val):
                by_date.setdefault(d, {})[sym] = val  # last value per (date,symbol)
        decision_dates = _downsample(sorted(by_date), config.rebalance)
        if len(decision_dates) < 2:
            raise ValueError("not enough rebalance dates with price coverage in the window")

        # ── master trading calendar: union of every panel's bar dates, RESTRICTED to
        # weekdays. The book mixes equities (5-day) with 24/7 crypto; an unfiltered
        # union puts Sat/Sun in the calendar, which would (a) annualise a ~365-day
        # series with a trading-day factor and (b) ffill equities across weekends.
        # Dropping weekends folds crypto's weekend move into the next weekday's
        # close-to-close return (the risk-lens convention). Weekday holidays where only
        # crypto trades are handled by the native-bar IC below; the equity curve is
        # return-relative, so a held name's ffilled holiday bar contributes 0 and
        # realises its move on the next native bar.
        master = [d for d in sorted({d for ds, _ in panels.values() for d in ds}) if d.weekday() < 5]
        n = len(master)
        if n < 3:
            raise ValueError("insufficient price history for the universe")
        last_settled = n - 1 - max(0, settle_days)  # provisional-tail exclusion
        if last_settled < 2:
            raise ValueError("window too short once the unsettled tail is excluded")

        names = sorted(panels)
        price = pd.DataFrame(index=pd.Index(master), columns=names, dtype=float)
        for name, (ds, cl) in panels.items():
            price[name] = pd.Series(np.asarray(cl, dtype=float), index=ds).reindex(master)
        price = price.ffill()  # carry the last close across non-trading days
        R = price.pct_change(fill_method=None).to_numpy()  # daily simple returns; NaN pre-first-bar
        P = price.to_numpy()
        col = {name: i for i, name in enumerate(names)}

        q = config.quantile
        port = np.zeros(n)  # portfolio daily return on the master calendar
        bench_ret = np.zeros(n)
        bench_desc = self._benchmark(config, benchmark_panel, master, R, names, warnings)
        if bench_desc[0] is not None:
            bench_ret = bench_desc[0]

        rebs: list[RebalancePoint] = []
        ics: list[float] = []
        long_counts: list[int] = []
        wprev = np.zeros(len(names))
        turnovers_one_way: list[float] = []
        first_active: int | None = None

        # entries for every decision date (the lookahead-clean rule)
        entries: list[tuple[date, int]] = []
        for t in decision_dates:
            ei = bisect.bisect_right(master, t)  # first bar STRICTLY after T
            entries.append((t, ei))

        for k, (t, ei) in enumerate(entries):
            cross = by_date[t]
            # next entry bounds this holding span; clip to the settled tail
            next_ei = entries[k + 1][1] if k + 1 < len(entries) else n
            span_end = min(next_ei, last_settled + 1)
            settled = ei <= last_settled and span_end > ei

            wvec, longs, shorts = self._weights(config, cross, col, P, ei, names)
            n_long = int((wvec > 0).sum())
            n_short = int((wvec < 0).sum())

            if not settled or n_long == 0:
                rebs.append(RebalancePoint(
                    date=t.isoformat(),
                    entry_date=master[ei].isoformat() if ei < n else None,
                    n_long=n_long, n_short=n_short, turnover=0.0, ic=None, status="pending",
                ))
                continue

            # daily portfolio return over the (settled) holding span, fixed weights
            for d in range(ei, span_end):
                rd = R[d]
                port[d] = float(np.nansum(np.where(np.isnan(rd), 0.0, rd) * wvec))
            # transaction cost on two-way turnover, charged on the entry bar
            two_way = float(np.abs(wvec - wprev).sum())
            one_way = 0.5 * two_way
            port[ei] -= config.costs_bps / 1e4 * two_way
            turnovers_one_way.append(one_way)
            wprev = wvec
            if first_active is None:
                first_active = ei
            long_counts.append(n_long)

            # IC over the FULL scored cross-section, priced on each name's OWN native
            # bars (entry = first native bar strictly after T; exit = last native bar
            # within the holding span) — never the ffilled master row, so the Rank-IC
            # has no one-bar weekend/holiday leak (mirrors the signal grader).
            exit_date = master[span_end - 1]
            scores_arr, fwd_arr = _native_fwd(panels, cross, t, exit_date)
            ic = rank_ic(scores_arr, fwd_arr) if scores_arr.size >= 3 else None
            if ic is not None:
                ics.append(ic)
            rebs.append(RebalancePoint(
                date=t.isoformat(), entry_date=master[ei].isoformat(),
                n_long=n_long, n_short=n_short, turnover=_f(one_way, 4) or 0.0,
                ic=_f(ic, 4), status="matured",
            ))
            if progress and len(entries) > 1:
                progress(0.55 + 0.3 * (k + 1) / len(entries), "compounding")

        n_matured = sum(1 for r in rebs if r.status == "matured")
        n_pending = sum(1 for r in rebs if r.status == "pending")
        if first_active is None:
            raise ValueError("no settled rebalance produced a tradeable basket")

        # ── equity curve from the day before first entry to the settled tail ──
        lo = first_active - 1 if first_active >= 1 else first_active
        idxs = list(range(lo, last_settled + 1))
        eq = [1.0]
        for d in idxs[1:]:
            eq.append(eq[-1] * (1.0 + port[d]))
        eq_arr = np.array(eq)
        peak = np.maximum.accumulate(eq_arr)
        dd = eq_arr / peak - 1.0
        equity = [EquityPoint(date=master[idxs[i]].isoformat(), value=_f(eq_arr[i], 6) or 0.0)
                  for i in range(len(idxs))]
        drawdown = [DrawdownPoint(date=master[idxs[i]].isoformat(), value=_f(dd[i] * 100, 4) or 0.0)
                    for i in range(len(idxs))]

        bench_equity = None
        if bench_desc[0] is not None:
            be = [1.0]
            for d in idxs[1:]:
                be.append(be[-1] * (1.0 + bench_ret[d]))
            bench_equity = [EquityPoint(date=master[idxs[i]].isoformat(), value=_f(be[i], 6) or 0.0)
                            for i in range(len(idxs))]

        if progress:
            progress(0.9, "metrics")
        # Annualise off the ACTUAL elapsed calendar span (the master is weekday, mixed
        # equity/crypto), not a hardcoded trading-day count — derive periods-per-year
        # empirically so vol/Sharpe/IR and the geometric annual return are correct.
        span_days = (master[idxs[-1]] - master[idxs[0]]).days
        years = span_days / 365.25 if span_days > 0 else (len(idxs) - 1) / 252.0
        metrics = self._metrics(
            config, port, bench_ret, eq_arr, dd, idxs, turnovers_one_way, ics, long_counts,
            n_matured, n_pending, bench_desc[1], years,
        )

        # ── honesty caveats (computed, surfaced — never buried) ──
        if metrics.n_names_avg is not None and metrics.n_names_avg < max(3.0, q * 0.8):
            warnings.append(f"thin cross-section ({metrics.n_names_avg:.1f} names/rebalance avg) — metrics are descriptive, not research-grade")
        max_w = self._max_avg_weight(rebs, config, by_date, panels)
        if max_w is not None and max_w > 0.5:
            warnings.append(f"single-name dominated (one name ≈ {max_w*100:.0f}% of the long basket) — diversification is illusory")
        if n_pending:
            warnings.append(f"{n_pending} final/unsettled rebalance period(s) excluded from the curve (provisional-close settle buffer)")
        if config.long_short:
            warnings.append("short leg: borrow cost / financing / short-availability NOT modelled — long/short returns are optimistic")
        if config.signal_id.startswith("xs_"):
            warnings.append("demo universe = today's Wealthfolio book applied to past dates (survivorship/selection lookahead at the universe level; per-name pricing is lookahead-clean)")

        if progress:
            progress(0.97, "finalising")
        return BacktestReport(
            signal_id=config.signal_id, engine=self.name, name=config.name,
            window_start=master[idxs[0]].isoformat(), window_end=master[idxs[-1]].isoformat(),
            equity=equity, drawdown=drawdown, benchmark_equity=bench_equity,
            rebalances=rebs, metrics=metrics, warnings=warnings,
        )

    # ── basket construction for one rebalance ──
    def _weights(self, config, cross, col, P, ei, names):
        # restrict to scored names with a (real or carried) price at the entry bar; the
        # curve is return-relative so a held name realises its move from its next native
        # bar. (The IC is priced separately on native bars — see _native_fwd.)
        usable = [(s, v) for s, v in cross.items() if s in col and not math.isnan(P[ei, col[s]])]
        wvec = np.zeros(len(names))
        if not usable:
            return wvec, [], []
        usable.sort(key=lambda kv: kv[1], reverse=True)  # high score = bullish
        m = len(usable)
        n_sel = max(1, m // config.quantile)
        longs = usable[:n_sel]
        shorts = usable[-n_sel:] if config.long_short and m >= 2 * n_sel else []

        def _lw(items, sign):
            if not items:
                return
            if config.weighting == "score":
                base = np.array([v for _, v in items], dtype=float)
                raw = np.maximum(base - base.min(), 0.0) + 1e-9
                w = raw / raw.sum()
            else:
                w = np.full(len(items), 1.0 / len(items))
            for (s, _), wi in zip(items, w):
                wvec[col[s]] = sign * float(wi)

        _lw(longs, 1.0)
        _lw(shorts, -1.0)
        return wvec, longs, shorts

    def _benchmark(self, config, benchmark_panel, master, R, names, warnings):
        """Return (daily_returns | None, description)."""
        n = len(master)
        if benchmark_panel is not None:
            ds, cl = benchmark_panel
            s = pd.Series(np.asarray(cl, dtype=float), index=ds).reindex(master).ffill()
            br = s.pct_change(fill_method=None).to_numpy()
            br = np.where(np.isnan(br), 0.0, br)
            return br, (config.benchmark_symbol or "benchmark")
        # synthetic: equal-weight buy-and-hold of the FULL signal universe
        br = np.zeros(n)
        for d in range(n):
            row = R[d]
            valid = ~np.isnan(row)
            br[d] = float(row[valid].mean()) if valid.any() else 0.0
        if config.benchmark_symbol:
            # A benchmark WAS requested but couldn't be priced — say so honestly rather
            # than the misleading "no benchmark_symbol set".
            warnings.append(f"benchmark '{config.benchmark_symbol}' could not be priced — info ratio falls back to a SYNTHETIC equal-weight-universe benchmark")
            return br, f"synthetic (— '{config.benchmark_symbol}' unpriceable)"
        warnings.append("info ratio vs a SYNTHETIC equal-weight-universe benchmark (no benchmark_symbol set) — interpret with care")
        return br, "synthetic equal-weight universe"

    def _metrics(self, config, port, bench_ret, eq_arr, dd, idxs, turnovers, ics, long_counts,
                 n_matured, n_pending, bench_desc, years) -> BacktestMetrics:
        active_days = idxs[1:]
        daily = np.array([port[d] for d in active_days])
        bdaily = np.array([bench_ret[d] for d in active_days])
        n_days = len(daily)
        # periods-per-year derived from the real elapsed span (not a hardcoded 252).
        ppy = (n_days / years) if years > 0 else 252.0
        rt = math.sqrt(ppy)
        cum = float(eq_arr[-1] - 1.0)
        ann_ret = None
        if years > 0 and eq_arr[-1] > 0:
            ann_ret = float(eq_arr[-1] ** (1.0 / years) - 1.0)  # geometric, over elapsed years
        sd = float(np.std(daily, ddof=1)) if n_days >= 2 else 0.0
        ann_vol = sd * rt if sd > 0 else None
        rf_period = config.rf_annual / ppy
        sharpe = None
        if sd > 0:
            sharpe = float((daily.mean() - rf_period) / sd * rt)
        info_ratio = None
        if n_days >= 2:
            active = daily - bdaily
            asd = float(np.std(active, ddof=1))
            if asd > 0:
                info_ratio = float(active.mean() / asd * rt)
        turnover_ann = float(sum(turnovers) / years) if years > 0 and turnovers else None
        return BacktestMetrics(
            cum_return_pct=_f(cum * 100, 3), ann_return_pct=_f((ann_ret or 0) * 100, 3) if ann_ret is not None else None,
            ann_vol_pct=_f((ann_vol or 0) * 100, 3) if ann_vol is not None else None,
            sharpe=_f(sharpe, 3), info_ratio=_f(info_ratio, 3),
            max_drawdown_pct=_f(float(dd.min()) * 100, 3) if dd.size else None,
            turnover_annualised=_f(turnover_ann, 3), mean_ic=_f(float(np.mean(ics)), 4) if ics else None,
            n_rebalances=n_matured + n_pending, n_matured_periods=n_matured, n_pending_periods=n_pending,
            n_names_avg=_f(float(np.mean(long_counts)), 2) if long_counts else None, n_days=n_days,
            benchmark=bench_desc,
        )

    def _max_avg_weight(self, rebs, config, by_date, panels) -> float | None:
        """Largest single-name share of the long basket across matured rebalances."""
        if config.weighting != "equal":
            return None
        sizes = [r.n_long for r in rebs if r.status == "matured" and r.n_long > 0]
        if not sizes:
            return None
        return 1.0 / min(sizes)  # equal weight → biggest share is 1/smallest basket
