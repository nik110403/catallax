"""Shadow verdict engine — pure per-tool mapping thresholds,
verdict-change event collapsing, and the graded-scoreboard statistics.
No DB/Redis: the class methods that need I/O are exercised via monkeypatched
seams (`_load_history` / `_upsert` / `_fetch_return`), matching this
codebase's convention (`app/services/advisor_report.py`'s module-level async
def split, `test_signals_scoring.py`'s pure-function panel tests).
"""
from __future__ import annotations

from datetime import datetime

import pytest

from app.errors import RateLimitExceeded, SymbolNotFound
from app.services.shadow import (
    NATURE,
    ShadowEngine,
    _collapse_events,
    _score_tool,
    _tercile_verdicts,
    _verdict_forensic,
    _verdict_patterns,
    _verdict_technicals,
    _wilson_lb,
    _week_start,
)


# ── technicals edge thresholds ──────────────────────────────────────────
def test_technicals_golden_cross_add_watch():
    v, d, r = _verdict_technicals({"sma50": 110, "sma200": 100, "rsi_14": 50}, 70.0, 30.0)
    assert (v, d) == ("add_watch", 1)


def test_technicals_golden_cross_muted_at_overbought_boundary():
    # RSI exactly at the overbought threshold mutes the buy signal.
    v, d, r = _verdict_technicals({"sma50": 110, "sma200": 100, "rsi_14": 70.0}, 70.0, 30.0)
    assert (v, d) == ("hold", None)
    v2, d2, _ = _verdict_technicals({"sma50": 110, "sma200": 100, "rsi_14": 69.9}, 70.0, 30.0)
    assert (v2, d2) == ("add_watch", 1)


def test_technicals_death_cross_trim_watch():
    v, d, _ = _verdict_technicals({"sma50": 90, "sma200": 100, "rsi_14": 50}, 70.0, 30.0)
    assert (v, d) == ("trim_watch", -1)


def test_technicals_death_cross_muted_at_oversold_boundary():
    v, d, _ = _verdict_technicals({"sma50": 90, "sma200": 100, "rsi_14": 30.0}, 70.0, 30.0)
    assert (v, d) == ("hold", None)
    v2, d2, _ = _verdict_technicals({"sma50": 90, "sma200": 100, "rsi_14": 30.1}, 70.0, 30.0)
    assert (v2, d2) == ("trim_watch", -1)


def test_technicals_no_cross_and_missing_data_hold():
    assert _verdict_technicals({"sma50": 100, "sma200": 100, "rsi_14": None}, 70.0, 30.0)[:2] == ("hold", None)
    assert _verdict_technicals({"sma50": None, "sma200": 100, "rsi_14": None}, 70.0, 30.0)[:2] == ("hold", None)


# ── patterns edge thresholds ─────────────────────────────────────────────
def test_patterns_directional_hits_and_no_hit():
    assert _verdict_patterns({"score": 2})[:2] == ("add_watch", 1)
    assert _verdict_patterns({"score": -1})[:2] == ("trim_watch", -1)
    assert _verdict_patterns({"score": 0})[:2] == ("hold", None)
    assert _verdict_patterns({})[:2] == ("hold", None)


# ── forensic: direction always NULL, priority order ──────────────────────
def test_forensic_distress_flags_regardless_of_piotroski():
    v, d, r = _verdict_forensic(
        {"altman_verdict": "Distress", "beneish_verdict": None, "piotroski_score": 8}, 3.0, 7.0
    )
    assert v == "flag" and d is None


def test_forensic_beneish_flags():
    v, d, _ = _verdict_forensic(
        {"altman_verdict": "Safe", "beneish_verdict": "Elevated manipulation risk", "piotroski_score": None},
        3.0, 7.0,
    )
    assert v == "flag" and d is None


def test_forensic_piotroski_boundaries():
    weak, _, _ = _verdict_forensic({"piotroski_score": 3}, 3.0, 7.0)
    mid, dmid, _ = _verdict_forensic({"piotroski_score": 5}, 3.0, 7.0)
    strong, dstrong, _ = _verdict_forensic({"piotroski_score": 7}, 3.0, 7.0)
    assert weak == "trim_watch" and mid == "hold" and strong == "add_watch"
    assert dmid is None and dstrong is None  # direction NULL always


# ── tercile ranking (factor / signals) ────────────────────────────────────
def test_tercile_insufficient_breadth_all_hold():
    out = _tercile_verdicts({"A": 1.0, "B": 2.0})
    assert all(v == ("hold", None, out[k][2]) for k, v in out.items() if k in ("A", "B"))
    assert all(out[k][0] == "hold" and out[k][1] is None for k in out)


def test_tercile_exact_split_of_three():
    out = _tercile_verdicts({"A": 1.0, "B": 2.0, "C": 3.0})
    assert out["A"][:2] == ("trim_watch", -1)
    assert out["B"][:2] == ("hold", None)
    assert out["C"][:2] == ("add_watch", 1)


def test_tercile_six_symbols_splits_evenly():
    values = {c: float(i) for i, c in enumerate("ABCDEF")}
    out = _tercile_verdicts(values)
    trims = [k for k, v in out.items() if v[0] == "trim_watch"]
    adds = [k for k, v in out.items() if v[0] == "add_watch"]
    holds = [k for k, v in out.items() if v[0] == "hold"]
    assert set(trims) == {"A", "B"} and set(adds) == {"E", "F"} and set(holds) == {"C", "D"}


# ── wilson lower bound sanity (reimplemented formula) ─────────────────────
def test_wilson_lb_below_point_estimate_and_none_for_zero_n():
    lb = _wilson_lb(3, 4)
    assert lb is not None and lb < 0.75
    assert _wilson_lb(0, 0) is None


# ── week-start anchor ──────────────────────────────────────────────────────
def test_week_start_is_monday_of_iso_week():
    d = _week_start("2026-W01")
    assert d.isoweekday() == 1
    assert d.isocalendar()[:2] == (2026, 1)


# ── verdict-change event collapsing: a persistent flag emitted N weeks in a
#    row is exactly ONE event ( statistical condition, acceptance
#    criterion) ─────────────────────────────────────────────────────────────
def test_persistent_flag_across_six_weeks_is_one_event():
    rows = [
        {"report_week": f"2026-W{i:02d}", "symbol": "BTC", "tool": "technicals",
         "verdict": "add_watch", "direction": 1, "reading": {}}
        for i in range(1, 7)
    ]
    events = _collapse_events(rows)
    assert len(events["technicals"]) == 1
    assert events["technicals"][0]["report_week"] == "2026-W01"


def test_verdict_change_to_a_different_non_hold_verdict_yields_two_events():
    rows = [
        {"report_week": "2026-W01", "symbol": "BTC", "tool": "technicals",
         "verdict": "add_watch", "direction": 1, "reading": {}},
        {"report_week": "2026-W02", "symbol": "BTC", "tool": "technicals",
         "verdict": "add_watch", "direction": 1, "reading": {}},
        {"report_week": "2026-W03", "symbol": "BTC", "tool": "technicals",
         "verdict": "trim_watch", "direction": -1, "reading": {}},
    ]
    events = _collapse_events(rows)
    assert [e["verdict"] for e in events["technicals"]] == ["add_watch", "trim_watch"]
    assert events["technicals"][1]["report_week"] == "2026-W03"


def test_hold_gap_does_not_split_or_reopen_an_event():
    # Fix round 2 (flap inflation): A, hold, A must stay ONE event — "hold"
    # is a transparent gap, not a boundary.
    rows = [
        {"report_week": "2026-W01", "symbol": "BTC", "tool": "technicals",
         "verdict": "add_watch", "direction": 1, "reading": {}},
        {"report_week": "2026-W02", "symbol": "BTC", "tool": "technicals",
         "verdict": "hold", "direction": None, "reading": {}},
        {"report_week": "2026-W03", "symbol": "BTC", "tool": "technicals",
         "verdict": "add_watch", "direction": 1, "reading": {}},
    ]
    events = _collapse_events(rows)
    assert len(events["technicals"]) == 1
    assert events["technicals"][0]["report_week"] == "2026-W01"


def test_flapping_rsi_threshold_collapses_to_one_graded_event():
    # 20 weeks alternating add_watch/hold (RSI hovering at 69.9/70.1) must
    # collapse to exactly 1 graded event, not one per flap ( 
    # acceptance criterion, fix round 2).
    rows = []
    for i in range(1, 21):
        verdict = "add_watch" if i % 2 else "hold"
        direction = 1 if verdict == "add_watch" else None
        rows.append({
            "report_week": f"2026-W{i:02d}", "symbol": "BTC", "tool": "technicals",
            "verdict": verdict, "direction": direction, "reading": {},
        })
    events = _collapse_events(rows)
    assert len(events["technicals"]) == 1
    assert events["technicals"][0]["verdict"] == "add_watch"
    assert events["technicals"][0]["report_week"] == "2026-W01"


# ── pure grading arithmetic (`_score_tool`) ───────────────────────────────
def test_score_tool_hit_rate_benchmark_and_wilson():
    obs = [
        {"symbol": "A", "report_week": "2026-W01", "direction": 1, "forward_return": 0.05, "exited": False},
        {"symbol": "B", "report_week": "2026-W05", "direction": -1, "forward_return": 0.02, "exited": False},
        {"symbol": "C", "report_week": "2026-W09", "direction": 1, "forward_return": -0.01, "exited": True},
    ]
    out = _score_tool("technicals", n_events=3, n_ungraded_null=0, n_pending=0, n_uncovered=0,
                       observations=obs, full_cross_section=[],
                       weeks_of_history=10, min_weeks=4, horizon_days=28)
    # hits: A (dir+1, ret+) hit; B (dir-1, ret+) miss; C (dir+1, ret-) miss -> 1/3
    assert out["n_graded"] == 3
    assert out["pushes"] == 0
    assert out["effective_n"] is None  # not a cross-sectional tool
    assert out["hit_rate"] == pytest.approx(1 / 3, abs=1e-3)
    assert out["benchmark_hit_rate"] == pytest.approx(2 / 3, abs=1e-3)  # A,B positive returns
    assert out["hit_rate_wilson_lb"] is not None
    assert out["exited_count"] == 1
    assert out["overlapping_windows"] is False  # weeks 4 apart == horizon, not >


def test_score_tool_overlapping_windows_flagged():
    obs = [
        {"symbol": "A", "report_week": "2026-W01", "direction": 1, "forward_return": 0.05, "exited": False},
        {"symbol": "A", "report_week": "2026-W02", "direction": 1, "forward_return": 0.03, "exited": False},
    ]
    out = _score_tool("technicals", n_events=2, n_ungraded_null=0, n_pending=0, n_uncovered=0,
                       observations=obs, full_cross_section=[],
                       weeks_of_history=10, min_weeks=4, horizon_days=28)
    assert out["overlapping_windows"] is True
    assert any("overlapping" in w for w in out["warnings"])


def test_score_tool_one_week_blindness_multiple_names_same_week_flagged():
    # Fix round 2: multiple graded names sharing ONE market week is just as
    # non-independent as literally overlapping windows — the gap-based check
    # alone never even runs when there's only one distinct week.
    obs = [
        {"symbol": "A", "report_week": "2026-W01", "direction": 1, "forward_return": 0.05, "exited": False},
        {"symbol": "B", "report_week": "2026-W01", "direction": -1, "forward_return": -0.02, "exited": False},
    ]
    out = _score_tool("technicals", n_events=2, n_ungraded_null=0, n_pending=0, n_uncovered=0,
                       observations=obs, full_cross_section=[],
                       weeks_of_history=10, min_weeks=4, horizon_days=28)
    assert out["overlapping_windows"] is True
    assert any("one market week" in w for w in out["warnings"])


def test_score_tool_cross_sectional_grades_vs_full_cross_section_median():
    # 3-name book: only A (top) and B (bottom) are graded picks; the median
    # base must be the FULL covered cross-section (A, B, C), not just {A, B}.
    full_cross = [
        {"symbol": "A", "report_week": "2026-W01", "forward_return": 0.10},
        {"symbol": "B", "report_week": "2026-W01", "forward_return": 0.02},
        {"symbol": "C", "report_week": "2026-W01", "forward_return": -0.02},
    ]
    obs = [
        {"symbol": "A", "report_week": "2026-W01", "direction": 1, "forward_return": 0.10, "exited": False},
        {"symbol": "B", "report_week": "2026-W01", "direction": -1, "forward_return": 0.02, "exited": False},
    ]
    # full-cross-section median = 0.02 -> A excess +0.08 (hit, dir+1); B excess
    # 0 (lands exactly on the median) -> a PUSH, excluded from hits/misses.
    out = _score_tool("factor", n_events=3, n_ungraded_null=1, n_pending=0, n_uncovered=0,
                       observations=obs, full_cross_section=full_cross,
                       weeks_of_history=10, min_weeks=4, horizon_days=28)
    assert out["cross_sectional"] is True
    assert out["n_graded"] == 2
    assert out["pushes"] == 1
    assert out["effective_n"] == 1  # floor(2 graded / 2) — one independent comparison
    assert out["hit_rate"] == pytest.approx(1.0)  # only A is "decided", and it hits


def test_score_tool_cross_sectional_excess_uses_full_set_not_graded_subset():
    # Regression for the SEV2 benchmark-base bug: computing the median from
    # only the graded (top/bottom) picks instead of the full covered
    # cross-section flips this exact case from a miss to a false "hit".
    full_cross = [
        {"symbol": "A", "report_week": "2026-W01", "forward_return": 0.20},
        {"symbol": "B", "report_week": "2026-W01", "forward_return": -0.20},
        {"symbol": "C", "report_week": "2026-W01", "forward_return": -0.20},
        {"symbol": "D", "report_week": "2026-W01", "forward_return": -0.20},
        {"symbol": "E", "report_week": "2026-W01", "forward_return": 0.02},
    ]
    obs = [
        {"symbol": "A", "report_week": "2026-W01", "direction": 1, "forward_return": 0.20, "exited": False},
        {"symbol": "E", "report_week": "2026-W01", "direction": -1, "forward_return": 0.02, "exited": False},
    ]
    # full-cross-section median = -0.20 (three untouched mid names).
    # The old (buggy) graded-subset-only median would be median(0.20, 0.02) =
    # 0.11, which flips E's excess sign (miss -> false hit). Correctly:
    # A excess = 0.40 (hit, dir+1); E excess = 0.22 (>0, but dir=-1 -> miss).
    out = _score_tool("factor", n_events=2, n_ungraded_null=3, n_pending=0, n_uncovered=0,
                       observations=obs, full_cross_section=full_cross,
                       weeks_of_history=10, min_weeks=4, horizon_days=28)
    assert out["hit_rate"] == pytest.approx(0.5)


def test_score_tool_cross_sectional_wilson_uses_effective_n():
    # Two weeks, each an add/trim tercile pair from the same ranking -> 4
    # graded rows but only 2 independent comparisons. The Wilson LB must be
    # computed on effective_n=2, giving a materially tighter (lower) bound
    # than a naive 4-trial Wilson at the same 100% hit-rate.
    full_cross = [
        {"symbol": "A", "report_week": "2026-W01", "forward_return": 0.10},
        {"symbol": "B", "report_week": "2026-W01", "forward_return": -0.10},
        {"symbol": "C", "report_week": "2026-W05", "forward_return": 0.10},
        {"symbol": "D", "report_week": "2026-W05", "forward_return": -0.10},
    ]
    obs = [
        {"symbol": "A", "report_week": "2026-W01", "direction": 1, "forward_return": 0.10, "exited": False},
        {"symbol": "B", "report_week": "2026-W01", "direction": -1, "forward_return": -0.10, "exited": False},
        {"symbol": "C", "report_week": "2026-W05", "direction": 1, "forward_return": 0.10, "exited": False},
        {"symbol": "D", "report_week": "2026-W05", "direction": -1, "forward_return": -0.10, "exited": False},
    ]
    out = _score_tool("factor", n_events=4, n_ungraded_null=0, n_pending=0, n_uncovered=0,
                       observations=obs, full_cross_section=full_cross,
                       weeks_of_history=10, min_weeks=4, horizon_days=28)
    assert out["n_graded"] == 4
    assert out["effective_n"] == 2
    assert out["hit_rate"] == pytest.approx(1.0)
    naive_four_trial = _wilson_lb(4, 4)
    assert out["hit_rate_wilson_lb"] == pytest.approx(_wilson_lb(2, 2), abs=1e-3)
    assert out["hit_rate_wilson_lb"] < naive_four_trial


def test_score_tool_low_confidence_gates_on_calendar_weeks_not_min_weeks_arg():
    # weeks_of_history below min_weeks -> low_confidence, regardless of observations.
    out = _score_tool("patterns", n_events=2, n_ungraded_null=0, n_pending=0, n_uncovered=0,
                       observations=[], full_cross_section=[],
                       weeks_of_history=2, min_weeks=4, horizon_days=28)
    assert out["low_confidence"] is True
    assert out["hit_rate"] is None  # no observations -> ungraded, not a miss
    assert any("week(s) of shadow_verdicts history" in w for w in out["warnings"])


def test_score_tool_low_confidence_gates_on_weeks_not_event_count():
    # Fix-round regression: a persistent flag is 1 EVENT but can span many
    # calendar WEEKS — low_confidence must track the latter, not n_events.
    out_long_history = _score_tool(
        "technicals", n_events=1, n_ungraded_null=0, n_pending=0, n_uncovered=0,
        observations=[], full_cross_section=[], weeks_of_history=20, min_weeks=4, horizon_days=28,
    )
    assert out_long_history["low_confidence"] is False
    out_short_history = _score_tool(
        "technicals", n_events=1, n_ungraded_null=0, n_pending=0, n_uncovered=0,
        observations=[], full_cross_section=[], weeks_of_history=2, min_weeks=4, horizon_days=28,
    )
    assert out_short_history["low_confidence"] is True


def test_score_tool_small_sample_warning_independent_of_low_confidence():
    # Fix round 2 (low-confidence vacuity): ample calendar history but a tiny
    # graded sample must still warn — the two conditions are independent.
    obs = [
        {"symbol": "A", "report_week": "2026-W01", "direction": 1, "forward_return": 0.05, "exited": False},
    ]
    out = _score_tool("technicals", n_events=1, n_ungraded_null=0, n_pending=0, n_uncovered=0,
                       observations=obs, full_cross_section=[],
                       weeks_of_history=52, min_weeks=4, horizon_days=28)
    assert out["low_confidence"] is False
    assert any("small sample" in w for w in out["warnings"])


# ── compute_verdicts: deterministic, idempotent upsert seam ───────────────
class _FakeSettings:
    report_pack_shadow_rsi_overbought = 70.0
    report_pack_shadow_rsi_oversold = 30.0
    report_pack_shadow_piotroski_weak = 3.0
    report_pack_shadow_piotroski_strong = 7.0
    report_pack_shadow_grade_min_weeks = 4
    price_source_default = "yfinance"
    advisor_cash_equivalent_symbols_set = {"USDC", "USDT", "DAI", "EURC"}


@pytest.mark.asyncio
async def test_compute_verdicts_is_deterministic_and_upserts():
    engine = ShadowEngine(redis=None, settings=_FakeSettings())
    upserted: list[list[dict]] = []

    async def fake_upsert(rows):
        upserted.append(rows)

    engine._upsert = fake_upsert  # monkeypatch the persistence seam

    readings = {
        "technicals": {"BTC": {"sma50": 110, "sma200": 100, "rsi_14": 50}},
        "patterns": {"BTC": {"score": 2}},
        "forensic": {"BTC": {"altman_verdict": "Safe", "beneish_verdict": "Low manipulation risk",
                              "piotroski_score": 8}},
        "factor": {"A": {"value": 1.0}, "B": {"value": 2.0}, "C": {"value": 3.0}},
    }
    rows1 = await engine.compute_verdicts("2026-W20", readings)
    rows2 = await engine.compute_verdicts("2026-W20", readings)
    assert rows1 == rows2  # byte-identical rows for identical input
    assert len(upserted) == 2  # each call upserts (idempotent on the unique key)

    by_tool_symbol = {(r["tool"], r["symbol"]): r for r in rows1}
    assert by_tool_symbol[("technicals", "BTC")]["verdict"] == "add_watch"
    assert by_tool_symbol[("forensic", "BTC")]["direction"] is None  # forensic never gradeable
    assert by_tool_symbol[("factor", "C")]["verdict"] == "add_watch"


@pytest.mark.asyncio
async def test_compute_verdicts_excludes_cash_equivalents_from_every_tool():
    """Mirrors advisor.py: a stablecoin (USDC) is cash, not a gradeable
    position — no tool emits a shadow verdict for it, even when a reading
    dict hands one in."""
    engine = ShadowEngine(redis=None, settings=_FakeSettings())

    async def fake_upsert(rows):
        pass

    engine._upsert = fake_upsert

    readings = {
        "technicals": {"BTC": {"sma50": 110, "sma200": 100, "rsi_14": 50},
                        "USDC": {"sma50": 1.0, "sma200": 1.0, "rsi_14": 50}},
        "factor": {"A": {"value": 1.0}, "USDC": {"value": 2.0}, "C": {"value": 3.0}},
    }
    rows = await engine.compute_verdicts("2026-W20", readings)

    symbols_by_tool = {}
    for r in rows:
        symbols_by_tool.setdefault(r["tool"], set()).add(r["symbol"])
    assert "USDC" not in symbols_by_tool.get("technicals", set())
    assert "USDC" not in symbols_by_tool.get("factor", set())
    assert symbols_by_tool["technicals"] == {"BTC"}
    assert symbols_by_tool["factor"] == {"A", "C"}


@pytest.mark.asyncio
async def test_compute_verdicts_drops_a_tool_entirely_when_only_cash_equivalents_present():
    engine = ShadowEngine(redis=None, settings=_FakeSettings())

    async def fake_upsert(rows):
        pass

    engine._upsert = fake_upsert

    rows = await engine.compute_verdicts("2026-W20", {
        "patterns": {"USDC": {"score": 2}},
    })
    assert rows == []


# ── scoreboard: forensic excluded from k, benchmark+wilson+k present ──────
@pytest.mark.asyncio
async def test_scoreboard_excludes_forensic_and_reports_k_and_nature():
    engine = ShadowEngine(redis=None, settings=_FakeSettings())

    history = [
        {"report_week": f"2026-W{i:02d}", "symbol": "BTC", "tool": "technicals",
         "verdict": "add_watch", "direction": 1, "reading": {}}
        for i in range(1, 5)
    ] + [
        {"report_week": "2026-W01", "symbol": "BTC", "tool": "forensic",
         "verdict": "flag", "direction": None, "reading": {}},
        {"report_week": "2026-W01", "symbol": "BTC", "tool": "patterns",
         "verdict": "add_watch", "direction": 1, "reading": {}},
    ]

    async def fake_load_history(report_week):
        return history

    async def fake_fetch_return(symbol, source, decision, horizon_days):
        return "matured", 0.03  # every graded verdict "hits"

    engine._load_history = fake_load_history
    engine._fetch_return = fake_fetch_return

    board = await engine.scoreboard("2026-W20")
    assert board["nature"] == NATURE
    assert board["tools"]["forensic"]["graded"] is False
    assert "reason" in board["tools"]["forensic"]
    assert board["tools"]["technicals"]["hit_rate"] == pytest.approx(1.0)
    assert board["tools"]["technicals"]["benchmark_hit_rate"] is not None
    assert board["tools"]["technicals"]["hit_rate_wilson_lb"] is not None
    assert board["k"] >= 1
    assert board["deflation_note"] is not None  # k > 1 (technicals + patterns both graded)


# ── pricing-failure resilience: a per-event pricing error must not abort
#    the whole pack (root-cause fix — bug traced to `_grade_tool` having no
#    per-event error handling) ──────────────────────────────────────────────
@pytest.mark.asyncio
async def test_grade_tool_symbol_not_found_counts_as_uncovered_and_does_not_abort():
    engine = ShadowEngine(redis=None, settings=_FakeSettings())
    history = [
        {"report_week": "2026-W01", "symbol": "DEAD", "tool": "technicals",
         "verdict": "add_watch", "direction": 1, "reading": {}},
        {"report_week": "2026-W01", "symbol": "LIVE", "tool": "technicals",
         "verdict": "add_watch", "direction": 1, "reading": {}},
    ]

    async def fake_load_history(report_week):
        return history

    calls: list[str] = []

    async def fake_fetch_return(symbol, source, decision, horizon_days):
        calls.append(symbol)
        if symbol == "DEAD":
            raise SymbolNotFound(f"No history for symbol {symbol}")
        return "matured", 0.03

    engine._load_history = fake_load_history
    engine._fetch_return = fake_fetch_return

    board = await engine.scoreboard("2026-W20")
    tech = board["tools"]["technicals"]
    assert tech["hit_rate"] == pytest.approx(1.0)  # only LIVE graded
    assert tech["n_uncovered"] == 1  # DEAD counted like status == "uncovered"
    assert calls.count("DEAD") == 1


@pytest.mark.asyncio
async def test_grade_tool_dead_symbol_memo_fetches_at_most_once_per_scoreboard_run():
    engine = ShadowEngine(redis=None, settings=_FakeSettings())
    # DEAD flips verdict twice -> two distinct events for the same symbol.
    history = [
        {"report_week": "2026-W01", "symbol": "DEAD", "tool": "technicals",
         "verdict": "add_watch", "direction": 1, "reading": {}},
        {"report_week": "2026-W02", "symbol": "DEAD", "tool": "technicals",
         "verdict": "trim_watch", "direction": -1, "reading": {}},
    ]

    async def fake_load_history(report_week):
        return history

    calls: list[str] = []

    async def fake_fetch_return(symbol, source, decision, horizon_days):
        calls.append(symbol)
        raise SymbolNotFound(f"No history for symbol {symbol}")

    engine._load_history = fake_load_history
    engine._fetch_return = fake_fetch_return

    board = await engine.scoreboard("2026-W20")
    assert board["tools"]["technicals"]["n_uncovered"] == 2  # both events still counted
    assert calls == ["DEAD"]  # but the actual fetch only happened once


@pytest.mark.asyncio
async def test_grade_tool_rate_limit_exceeded_counts_as_uncovered_and_does_not_abort():
    engine = ShadowEngine(redis=None, settings=_FakeSettings())
    history = [
        {"report_week": "2026-W01", "symbol": "BTC", "tool": "technicals",
         "verdict": "add_watch", "direction": 1, "reading": {}},
    ]

    async def fake_load_history(report_week):
        return history

    async def fake_fetch_return(symbol, source, decision, horizon_days):
        raise RateLimitExceeded("yfinance: rate limit of 30/min exceeded")

    engine._load_history = fake_load_history
    engine._fetch_return = fake_fetch_return

    board = await engine.scoreboard("2026-W20")
    tech = board["tools"]["technicals"]
    assert tech["n_uncovered"] == 1
    assert tech["hit_rate"] is None  # no graded observations, not an abort


# ── fix round 1: low_confidence gates on CALENDAR WEEKS of shadow_verdicts
#    history (config.py's own definition), not on n_events. A persistent
#    flag collapses to 1 event regardless of how many weeks it spans, so the
#    gate must be wired off the earliest row's report_week, not the event
#    count. ────────────────────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_scoreboard_persistent_flag_20_weeks_is_not_low_confidence():
    engine = ShadowEngine(redis=None, settings=_FakeSettings())
    history = [
        {"report_week": f"2026-W{i:02d}", "symbol": "BTC", "tool": "technicals",
         "verdict": "add_watch", "direction": 1, "reading": {}}
        for i in range(1, 21)  # 20 calendar weeks, one unbroken verdict
    ]

    async def fake_load_history(report_week):
        return history

    async def fake_fetch_return(symbol, source, decision, horizon_days):
        return "matured", 0.03

    engine._load_history = fake_load_history
    engine._fetch_return = fake_fetch_return

    board = await engine.scoreboard("2026-W20")
    tech = board["tools"]["technicals"]
    assert tech["n_events"] == 1  # persistent flag -> exactly one verdict-change event
    assert tech["low_confidence"] is False  # but 20 weeks of history is enough


@pytest.mark.asyncio
async def test_scoreboard_two_weeks_of_history_is_low_confidence():
    engine = ShadowEngine(redis=None, settings=_FakeSettings())
    history = [
        {"report_week": f"2026-W{i:02d}", "symbol": "BTC", "tool": "technicals",
         "verdict": "add_watch", "direction": 1, "reading": {}}
        for i in range(1, 3)  # only 2 calendar weeks of history
    ]

    async def fake_load_history(report_week):
        return history

    async def fake_fetch_return(symbol, source, decision, horizon_days):
        return "pending", None

    engine._load_history = fake_load_history
    engine._fetch_return = fake_fetch_return

    board = await engine.scoreboard("2026-W02")
    tech = board["tools"]["technicals"]
    assert tech["n_events"] == 1
    assert tech["low_confidence"] is True


# ── fix round 2: horizon decoupled from the maturity gate ─────────────────
@pytest.mark.asyncio
async def test_grading_horizon_is_fixed_four_weeks_independent_of_grade_min_weeks():
    class _SettingsLongMinWeeks(_FakeSettings):
        report_pack_shadow_grade_min_weeks = 10  # maturity gate, NOT the horizon

    engine = ShadowEngine(redis=None, settings=_SettingsLongMinWeeks())
    history = [
        {"report_week": "2026-W01", "symbol": "BTC", "tool": "technicals",
         "verdict": "add_watch", "direction": 1, "reading": {}},
    ]
    captured: dict[str, int] = {}

    async def fake_load_history(report_week):
        return history

    async def fake_fetch_return(symbol, source, decision, horizon_days):
        captured["horizon_days"] = horizon_days
        return "pending", None

    engine._load_history = fake_load_history
    engine._fetch_return = fake_fetch_return

    await engine.scoreboard("2026-W01")
    assert captured["horizon_days"] == 28  # 4 weeks, NOT 10 * 7 = 70


# ── fix round 2: exited inferred at grading time (no producer sets it) ────
@pytest.mark.asyncio
async def test_exited_inferred_when_symbol_absent_from_latest_verdict_week():
    engine = ShadowEngine(redis=None, settings=_FakeSettings())
    history = [
        {"report_week": "2026-W01", "symbol": "BTC", "tool": "technicals",
         "verdict": "add_watch", "direction": 1, "reading": {}},
        {"report_week": "2026-W01", "symbol": "ETH", "tool": "technicals",
         "verdict": "add_watch", "direction": 1, "reading": {}},
        {"report_week": "2026-W05", "symbol": "ETH", "tool": "technicals",
         "verdict": "add_watch", "direction": 1, "reading": {}},
        # BTC never reappears -> absent from the latest (W05) verdict week -> exited
    ]

    async def fake_load_history(report_week):
        return history

    async def fake_fetch_return(symbol, source, decision, horizon_days):
        return "matured", 0.03

    engine._load_history = fake_load_history
    engine._fetch_return = fake_fetch_return

    board = await engine.scoreboard("2026-W10")
    assert board["tools"]["technicals"]["exited_count"] == 1  # BTC only


@pytest.mark.asyncio
async def test_exited_reading_override_wins_over_inference():
    engine = ShadowEngine(redis=None, settings=_FakeSettings())
    history = [
        {"report_week": "2026-W01", "symbol": "BTC", "tool": "technicals",
         "verdict": "add_watch", "direction": 1, "reading": {"exited": True}},
        {"report_week": "2026-W05", "symbol": "BTC", "tool": "technicals",
         "verdict": "add_watch", "direction": 1, "reading": {}},
        # BTC IS present at the latest (W05) verdict week, so grading-time
        # INFERENCE alone would say not-exited — the opening event's explicit
        # reading["exited"]=True must still win over that inference.
    ]

    async def fake_load_history(report_week):
        return history

    async def fake_fetch_return(symbol, source, decision, horizon_days):
        return "matured", 0.03

    engine._load_history = fake_load_history
    engine._fetch_return = fake_fetch_return
    board = await engine.scoreboard("2026-W05")
    # Single event (persistent add_watch) opened at W01 with reading exited=True
    # overrides the inference (which would say False, since BTC is present at
    # the latest verdict week).
    assert board["tools"]["technicals"]["exited_count"] == 1


# ── fix round 2: priced_as_of stamp ────────────────────────────────────────
@pytest.mark.asyncio
async def test_scoreboard_includes_priced_as_of_timestamp():
    engine = ShadowEngine(redis=None, settings=_FakeSettings())

    async def fake_load_history(report_week):
        return []

    engine._load_history = fake_load_history
    board = await engine.scoreboard("2026-W01")
    assert "priced_as_of" in board
    datetime.fromisoformat(board["priced_as_of"])  # parses without error


# ── fix round 2: how_to_read describes what's actually computed ───────────
@pytest.mark.asyncio
async def test_how_to_read_describes_full_cross_section_and_effective_n():
    engine = ShadowEngine(redis=None, settings=_FakeSettings())

    async def fake_load_history(report_week):
        return []

    engine._load_history = fake_load_history
    chapter = await engine.chapter_shadow("2026-W01")
    text = chapter["how_to_read"]
    assert "full covered cross-section" in text
    assert "effective_n" in text
    assert "book's own unconditional up-rate" not in text  # old, inaccurate wording


@pytest.mark.asyncio
async def test_chapter_shadow_shape():
    engine = ShadowEngine(redis=None, settings=_FakeSettings())

    async def fake_load_history(report_week):
        return []

    engine._load_history = fake_load_history
    chapter = await engine.chapter_shadow("2026-W20")
    assert set(("engine", "how_to_read", "nature", "tools")) <= chapter.keys()
    assert chapter["nature"] == NATURE
