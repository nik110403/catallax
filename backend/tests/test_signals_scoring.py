"""Phase 9 signal self-scoring core — the numerical heart, tested on synthetic data.

The one defect that would rot a backtester is LOOKAHEAD, so these assert the exact
maturation rule (entry strictly after T, exit H bars later, settled) and the
cross-sectional Rank-IC on a panel whose answer is known by construction. Pure
functions only — no DB, no network.
"""
from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from app.services.signals import (
    _build_reference_signals,
    _compute_grade,
    _forward,
    _rank_ic,
    _two_sided_p,
    _wilson_lb,
)

_DATES = [(date(2026, 1, 5) + timedelta(days=i)) for i in range(16)]
_ISO = [d.isoformat() for d in _DATES]


def _series(overrides: dict[int, float]) -> tuple[list[str], list[float], bool]:
    closes = [100.0] * 16
    for i, c in overrides.items():
        closes[i] = c
    return _ISO, closes, False


# ── the lookahead / maturation rule ────────────────────────────
def test_entry_is_strictly_after_decision_date():
    closes = np.array([100.0] * 16)
    # T = a bar date → entry must be the NEXT bar (index+1), never the same bar.
    status, entry, exit_i, _ = _forward(_DATES, closes, _DATES[2], h=2, settle=1)
    assert entry == 3  # strictly after _DATES[2]
    assert exit_i == 5  # entry + H


def test_decision_date_in_a_bar_gap_takes_next_bar():
    # Sparse calendar (weekend/holiday gap): Jan 7-8 have no bar.
    sparse = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 9), date(2026, 1, 10)]
    closes = np.array([100.0] * 4)
    # Decision on Jan 7 (no bar) → entry = first bar strictly after = Jan 9 (index 2).
    status, entry, _exit, _ = _forward(sparse, closes, date(2026, 1, 7), h=1, settle=0)
    assert entry == 2


def test_pending_when_exit_not_settled():
    closes = np.array([100.0] * 16)
    # exit lands on the very last bar → not trailed by `settle` bars → pending.
    status, _e, _x, fwd = _forward(_DATES, closes, _DATES[12], h=3, settle=1)  # exit idx 16 > 15
    assert status == "pending" and fwd is None
    # exit at the tail (idx 15) with settle=1 also pending (no trailing settle bar).
    status2, _e2, _x2, _ = _forward(_DATES, closes, _DATES[11], h=3, settle=1)  # entry 12, exit 15
    assert status2 == "pending"


def test_matured_when_settled_and_forward_return_correct():
    closes = np.array([100.0] * 16)
    closes[3] = 100.0
    closes[5] = 110.0
    status, entry, exit_i, fwd = _forward(_DATES, closes, _DATES[2], h=2, settle=1)
    assert status == "matured"
    assert fwd == pytest.approx(0.10)  # 110/100 - 1


def test_uncovered_when_no_bar_after_decision():
    closes = np.array([100.0] * 16)
    status, *_ = _forward(_DATES, closes, _DATES[15], h=1, settle=0)
    assert status == "uncovered"


# ── Rank-IC + small-sample honesty ─────────────────────────────
def test_rank_ic_monotonic():
    v = np.array([5.0, 4, 3, 2, 1])
    assert _rank_ic(v, np.array([10.0, 8, 6, 4, 2])) == pytest.approx(1.0)
    assert _rank_ic(v, np.array([2.0, 4, 6, 8, 10])) == pytest.approx(-1.0)


def test_wilson_lb_below_point_estimate():
    lb = _wilson_lb(3, 4)  # 75% on 4 trials → LB well under 0.75
    assert lb is not None and lb < 0.5


def test_two_sided_p():
    assert _two_sided_p(0.0) == pytest.approx(1.0)
    assert _two_sided_p(5.0) < 0.001


# ── full grade on a known panel ────────────────────────────────
def _panel():
    """5 names, 3 decision dates. Higher value ⇒ higher forward return at every date,
    so per-date Rank-IC = +1 by construction. T1/T2 mature, T3 is still pending."""
    fwd_by_val = {2.0: 0.04, 1.0: 0.02, 0.0: 0.0, -1.0: -0.02, -2.0: -0.04}
    syms = {"A": 2.0, "B": 1.0, "C": 0.0, "D": -1.0, "E": -2.0}
    # T1 entry=3 exit=5 ; T2 entry=9 exit=11 ; T3 entry=15 exit=17 (pending)
    series = {}
    rows = []
    for sym, val in syms.items():
        f = fwd_by_val[val]
        ov = {3: 100.0, 5: 100.0 * (1 + f), 9: 100.0, 11: 100.0 * (1 + f)}
        series[(sym, "yfinance")] = _series(ov)
        for t_idx in (2, 8, 14):
            rows.append((_DATES[t_idx].isoformat(), sym, "yfinance", val))
    return rows, series


def test_compute_grade_known_panel():
    rows, series = _panel()
    grade, transient = _compute_grade(
        "xs_test", rows, series, h=2, q=5, settle=1, min_names=3, min_dates=1,
        today=date(2026, 6, 24),
    )
    assert not transient
    assert grade.methodology == "cross-sectional"
    assert grade.coverage.n_pairs_matured == 10  # 5 names × (T1, T2)
    assert grade.coverage.n_pairs_pending == 5  # T3 horizon not elapsed
    assert grade.coverage.n_pairs_uncovered == 0
    assert grade.n_dates_graded == 2
    assert grade.mean_rank_ic == pytest.approx(1.0)
    assert all(p.rank_ic == pytest.approx(1.0) for p in grade.per_date if p.rank_ic is not None)
    # hit-rate: 4 non-median names per date hit both ×2 dates = 8/8.
    assert grade.hit_rate == pytest.approx(1.0)
    assert grade.n_hit_pairs == 8
    # demeaned L/S spread = top(+4%) − bottom(−4%) = 8% per date.
    assert grade.ls_spread_mean_pct == pytest.approx(8.0)


def test_no_lookahead_leak_pending_excluded():
    """A pending (unmatured) window must never enter the graded cross-section."""
    rows, series = _panel()
    grade, _ = _compute_grade(
        "xs_test", rows, series, h=2, q=5, settle=1, min_names=3, min_dates=1,
        today=date(2026, 6, 24),
    )
    graded_dates = {p.date for p in grade.per_date if p.rank_ic is not None}
    assert _DATES[14].isoformat() not in graded_dates  # T3 never graded


# ── reference generator is sign-normalised + lookahead-clean ────
def test_reference_signals_zscored_per_date():
    # Long flat-then-trending closes so momentum is well-defined for all symbols.
    n = 400
    iso = [(date(2025, 1, 1) + timedelta(days=i)).isoformat() for i in range(n)]
    resolved = [(s, "yfinance") for s in ("A", "B", "C", "D", "E", "F")]
    series = {}
    for k, (sym, _) in enumerate(resolved):
        closes = [100.0 * (1.0 + 0.001 * k) ** i for i in range(n)]  # distinct drifts
        series[(sym, "yfinance")] = (iso, closes, False)
    decision = [date(2025, 1, 1) + timedelta(days=380)]
    rows = _build_reference_signals(
        resolved, series, decision, mom_lb=252, mom_skip=21, lowvol_lb=63, min_names=5,
    )
    mom = [r["value"] for r in rows if r["signal_id"] == "xs_mom_252_21"]
    assert len(mom) == 6
    assert sum(mom) == pytest.approx(0.0, abs=1e-9)  # z-scores sum to zero
    assert np.std(mom) == pytest.approx(1.0, abs=1e-6)  # population z-score
