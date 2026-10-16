"""Pure-function tests for the vectorized backtest engine (Phase 9, engine).

The cardinal test is no-lookahead: entry MUST be the first bar STRICTLY after the
decision date T (bisect_right), the same rule the proven signal grader uses. The
rest pin settle-tail exclusion, cost drag, monotone-signal IC, and pending counts.
No DB/Redis — the engine is pure; we hand it synthetic panels + signal rows.
"""
from __future__ import annotations

import bisect
from datetime import date, timedelta

import numpy as np

from app.adapters.backtest.base import BacktestConfig
from app.adapters.backtest.vectorized import VectorizedEngine, _downsample
from app.services.signals import _rank_ic

ENGINE = VectorizedEngine()


def _bdays(start: date, n: int) -> list[date]:
    out: list[date] = []
    d = start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _panel(dates: list[date], daily_factor: float, base: float = 100.0):
    closes = base * np.cumprod(np.full(len(dates), daily_factor))
    return dates, closes


def _fixture(n_names: int = 10, n_days: int = 170, monotone: bool = True):
    """n_names symbols; symbol i rises at rate proportional to i (monotone => higher
    score should predict higher forward return). Monthly decision dates."""
    dates = _bdays(date(2024, 1, 1), n_days)
    panels = {}
    for i in range(n_names):
        rate = 1.0 + (i * 0.0006 if monotone else 0.0004)  # higher i = faster rise
        panels[f"S{i}"] = _panel(dates, rate)
    # monthly decision dates = last business day of each month present in `dates`
    by_month: dict[tuple[int, int], date] = {}
    for d in dates[: n_days - 20]:  # leave tail room so windows mature
        by_month[(d.year, d.month)] = d
    decision_dates = sorted(by_month.values())
    signal_rows = []
    for t in decision_dates:
        for i in range(n_names):
            signal_rows.append((t, f"S{i}", float(i)))  # score = i (sym i more bullish)
    return panels, signal_rows, dates, decision_dates


def _cfg(**kw) -> BacktestConfig:
    base = dict(name="t", signal_id="xs_test", start=date(2024, 1, 1), end=date(2024, 12, 31))
    base.update(kw)
    return BacktestConfig(**base)


def _run(panels, signal_rows, settle=2, **cfgkw):
    return ENGINE.run(_cfg(**cfgkw), panels, signal_rows, None, settle, _rank_ic)


def test_no_lookahead_entry_strictly_after_T():
    panels, signal_rows, dates, decision_dates = _fixture()
    rep = _run(panels, signal_rows)
    master = sorted({d for ds, _ in panels.values() for d in ds})
    matured = [r for r in rep.rebalances if r.status == "matured"]
    assert matured, "expected at least one matured rebalance"
    for r in matured:
        t = date.fromisoformat(r.date)
        expected = master[bisect.bisect_right(master, t)]
        assert r.entry_date == expected.isoformat()
        assert date.fromisoformat(r.entry_date) > t  # STRICTLY after — the cardinal rule


def test_settle_buffer_excludes_unsettled_tail():
    panels, signal_rows, dates, _ = _fixture()
    master = sorted({d for ds, _ in panels.values() for d in ds})
    settle = 5
    rep = _run(panels, signal_rows, settle=settle)
    last_settled = master[len(master) - 1 - settle]
    assert date.fromisoformat(rep.window_end) <= last_settled


def test_monotone_signal_positive_ic_and_return():
    panels, signal_rows, *_ = _fixture(monotone=True)
    rep = _run(panels, signal_rows)
    # perfect monotone cross-section → Rank-IC ≈ 1 each rebalance
    assert rep.metrics.mean_ic is not None and rep.metrics.mean_ic > 0.9
    # long the top quantile of a rising book → positive cumulative return
    assert rep.metrics.cum_return_pct is not None and rep.metrics.cum_return_pct > 0


def test_costs_drag_reduces_return():
    panels, signal_rows, *_ = _fixture()
    free = _run(panels, signal_rows, costs_bps=0.0)
    costly = _run(panels, signal_rows, costs_bps=50.0)
    assert costly.metrics.cum_return_pct < free.metrics.cum_return_pct


def test_pending_tail_is_counted_not_dropped_silently():
    # end the window so the final decision date's horizon can't settle
    panels, signal_rows, dates, decision_dates = _fixture()
    rep = _run(panels, signal_rows, settle=10)
    total = rep.metrics.n_matured_periods + rep.metrics.n_pending_periods
    assert total == rep.metrics.n_rebalances
    # the curve only spans matured periods; pending are still reported
    assert all(r.status in ("matured", "pending") for r in rep.rebalances)


def test_long_short_warns_borrow_not_modelled():
    panels, signal_rows, *_ = _fixture()
    rep = _run(panels, signal_rows, long_short=True)
    assert any("borrow" in w.lower() for w in rep.warnings)


def test_synthetic_benchmark_warns():
    panels, signal_rows, *_ = _fixture()
    rep = _run(panels, signal_rows)  # no benchmark_symbol → synthetic
    assert rep.metrics.benchmark.startswith("synthetic")
    assert any("synthetic" in w.lower() for w in rep.warnings)


def test_downsample_monthly_keeps_last_per_month():
    days = _bdays(date(2024, 1, 1), 90)
    kept = _downsample(days, "M")
    # one per calendar month, each the last business day of that month present
    months = {(d.year, d.month) for d in days}
    assert len(kept) == len(months)
    for k in kept:
        same_month = [d for d in days if (d.year, d.month) == (k.year, k.month)]
        assert k == max(same_month)


def test_master_calendar_drops_weekends():
    # a 24/7 crypto panel injects weekend dates; the master calendar must keep only
    # weekdays (else a 365-day series would be annualised with a trading-day factor and
    # equities ffilled across weekends).
    cal = []
    d = date(2024, 1, 1)
    while len(cal) < 200:  # every calendar day (crypto-style)
        cal.append(d)
        d += timedelta(days=1)
    panels = {"BTC-USD": _panel(cal, 1.0003), "S0": _panel(_bdays(date(2024, 1, 1), 140), 1.0006)}
    by_m: dict[tuple[int, int], date] = {}
    for dd in _bdays(date(2024, 1, 1), 130):
        by_m[(dd.year, dd.month)] = dd
    decs = sorted(by_m.values())
    rows = [(t, s, v) for t in decs for s, v in (("S0", 1.0), ("BTC-USD", 0.5))]
    rep = ENGINE.run(_cfg(quantile=2), panels, rows, None, 2, _rank_ic)
    assert rep.equity, "expected a non-empty equity curve"
    assert all(date.fromisoformat(p.date).weekday() < 5 for p in rep.equity)


def test_native_fwd_entry_is_first_native_bar_after_T():
    # the IC must price each name on its OWN native bars (entry strictly after T), not
    # the ffilled master row — the no-weekend/holiday-leak guarantee.
    from app.adapters.backtest.vectorized import _native_fwd

    dates = _bdays(date(2024, 1, 1), 20)
    closes = np.array([100.0 + i for i in range(20)])
    t = dates[4]
    exit_date = dates[9]
    scores, fwds = _native_fwd({"X": (dates, closes)}, {"X": 1.0}, t, exit_date)
    ent = bisect.bisect_right(dates, t)
    assert dates[ent] > t  # strictly after T
    assert scores.size == 1 and abs(fwds[0] - (closes[9] / closes[ent] - 1.0)) < 1e-9


def test_equity_curve_monotone_when_all_rising():
    # every name strictly rising → long-only equity should never draw down materially
    dates = _bdays(date(2024, 1, 1), 170)
    panels = {f"S{i}": _panel(dates, 1.0005) for i in range(8)}
    by_month: dict[tuple[int, int], date] = {}
    for d in dates[:150]:
        by_month[(d.year, d.month)] = d
    decision_dates = sorted(by_month.values())
    rows = [(t, f"S{i}", float(i)) for t in decision_dates for i in range(8)]
    rep = ENGINE.run(_cfg(), panels, rows, None, 2, _rank_ic)
    assert rep.metrics.max_drawdown_pct is not None
    assert rep.metrics.max_drawdown_pct >= -1.0  # essentially flat-to-up, tiny dd at most
    assert rep.equity[0].value == 1.0 and rep.equity[-1].value > 1.0
