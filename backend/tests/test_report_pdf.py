"""Tests for the PDF pipeline's non-Playwright surface (Phase 17):
containment (`resolve_pdf_path`) and the Jinja2 template render
(`_render_html`) — autoescape correctness in particular, since `sections`
carries raw web-sourced strings. The Playwright half (`render_pack_pdf`'s
actual browser launch) is proven separately via a one-off `docker run`
against the built `Dockerfile.report` image — it
needs a real Chromium, which this host venv does not have.

Fix round: present-but-wrong-shaped known structured
fields (a string where a list/dict was expected) must render without
raising, with the affected chapter+field surfaced in the rendered PDF's
"Render Warnings" appendix — see `_normalize_sections` in report_pdf.py.

Fix round 2: the same guard extends to numeric LEAVES fed through a
`|pct`/`|num`/`|eur` filter, not just containers.

Fix round 3 (shape alignment): `_fixture_sections()` below and every
wrong-shape/numeric-leaf case now mirror the REAL `report_pack.py`, `shadow.py` and
`report_worker.py` output shapes, not this module's
original invented contract — see `report_pdf.py`'s module docstring for the
real shapes, cited chapter-by-chapter. `test_render_html_populates_real_values_
from_actual_producer_shapes` builds its sections dict by calling the REAL
`ReportPackEngine.build_chapters()` (monkeypatched engines, mirroring
`test_report_pack.py`'s own fixtures) rather than hand-rolling one, per the
controller's proof requirement — a fixture that drifts from the producers
is exactly how the original mismatch happened.

Fix round 4: the
shadow table now shows `n_graded` (the real denominator) alongside
`n_events`, plus per-tool `low_confidence`/`warnings[]`/optional
`effective_n`/`pushes` rendered visibly under the table; `quant.factor.
warnings[]`/`quant.scoreboard.warnings[]` (previously guarded but never
rendered) now print verbatim; `exec_summary.narration` renders as escaped
text (no `|safe`) with a small+muted `narration_absent_reason` fallback.
`_fixture_sections()` extended with real (non-empty) warnings arrays and a
narration field so these paths are exercised by default, not just by
override tests.

Fix round 5 (editorial redesign, renderer-side only): `_fixture_sections()`
now mirrors the REAL `var_cvar`/`coverage`/`scorecard`/`market` leaf shapes
(the prior fixture's `var_cvar={"var_95_eur": ...}` and bare `market={
"regime": {"state": ...}}` were never real producer output — the old
template dumped both generically, so the mismatch was invisible). New
tests cover the redesign's editorial rules directly: no Python dict/list
repr anywhere in the rendered HTML, the positions-table materiality
threshold + tail collapse, the scorecard's one-sentence insufficient-history
state, the "In plain terms" panel's value-gated sentences, the market
correlation top/bottom-pairs reader (never the raw matrix), the econ
calendar's dedupe+cap+column-drop, holdings' material-tear-sheet-vs-roster
split, and the "N further field(s) recorded" residual note replacing the
old generic key/value fallback.
"""
from __future__ import annotations

import asyncio
import contextlib
import re
from typing import Any

import pytest

import app.services.report_pack as report_pack_module
from app.config import Settings
from app.services.report_pack import ReportPackEngine
from app.services.report_pdf import (
    _CALIBRATION_STAT_LABELS,
    _CHAPTER_ORDER,
    _calibration_groups_view,
    _display_issuer,
    _environment,
    _is_crypto,
    _normalize_sections,
    _plain_terms_forecasts,
    _render_html,
    _shadow_definition_sentence,
    resolve_pdf_path,
)


# ── containment ──────────────────────────────────────────────────────────
def test_resolve_pdf_path_accepts_a_path_under_base(tmp_path) -> None:
    base = tmp_path / "reports"
    base.mkdir()
    target = base / "catallax-weekly-2026-W33.pdf"
    resolved = resolve_pdf_path(str(target), str(base))
    assert resolved.startswith(str(base.resolve()))


def test_resolve_pdf_path_rejects_traversal_escape(tmp_path) -> None:
    base = tmp_path / "reports"
    base.mkdir()
    escaping = base / ".." / "secrets" / "leak.pdf"
    with pytest.raises(ValueError):
        resolve_pdf_path(str(escaping), str(base))


def test_resolve_pdf_path_rejects_unrelated_absolute_path(tmp_path) -> None:
    base = tmp_path / "reports"
    base.mkdir()
    with pytest.raises(ValueError):
        resolve_pdf_path("/etc/passwd", str(base))


def test_resolve_pdf_path_rejects_symlink_escape(tmp_path) -> None:
    base = tmp_path / "reports"
    base.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = base / "escape.pdf"
    link.symlink_to(outside / "leak.pdf")
    with pytest.raises(ValueError):
        resolve_pdf_path(str(link), str(base))


# ── template render — fixture mirrors the REAL producer shapes ───────────
def _fixture_sections() -> dict:
    return {
        "exec_summary": {
            "engine": "report-worker-exec-summary-v1", "how_to_read": "NAV/WoW/regime roll-up.",
            "nav_eur": 48512.30, "wow_pct": 1.42, "regime": {"state": "risk-on", "score": 0.42},
            "chapters_present": {"portfolio": True}, "n_chapters_present": 5, "shadow_counts": {},
            "headline": "NAV up on the week.", "highlights": ["BTC led gains"],
            "narration": "NAV rose 1.42% this week, led by BTC.", "narration_absent_reason": None,
        },
        "portfolio": {
            "engine": "risk.RiskEngine + services.scorecard.compute_scorecard",
            "how_to_read": "Positions as of Friday close.",
            "nav_eur": 48512.30, "cash_eur": 5000.0,
            "positions": [
                {
                    "symbol": "BTC", "qty": 0.5, "value_eur": 20000.0, "weight_pct": 41.2,
                    "wow_weight_delta_pct": 2.1, "wow_value_delta_eur": 800.0,
                },
                {
                    "symbol": "ETH", "qty": 4.0, "value_eur": 1500.0, "weight_pct": 3.0,
                    "wow_weight_delta_pct": None, "wow_value_delta_eur": None,
                },
            ],
            # Real `services/risk.py` `_var_block`/bundle["var"] leaf names —
            # `coverage` is NESTED inside `var_cvar` (real production shape;
            # a top-level `portfolio.coverage` field does not exist upstream).
            "var_cvar": {
                "var_eur": -1200.0, "var_pct": -2.47, "cvar_eur": -1800.0, "cvar_pct": -3.71,
                "var99_eur": -2400.0, "cvar99_eur": -3000.0, "gaussian_var_eur": -1100.0,
                "fat_tail_ratio": 1.5, "hist_vs_gaussian": 1.09, "worst_day": "2026-08-05",
                "worst_day_pct": -4.2, "confidence": 0.95, "low_confidence": False, "n_scenarios": 340,
                "coverage": {
                    "n_assets_total": 2, "n_covered": 2, "n_skipped": 0, "covered_weight_pct": 100.0,
                    "n_observations": 335, "window_start": "2025-03-31", "window_end": "2026-08-14",
                    "cash_weight_pct": 10.3,
                    "assumption": "Daily EUR P&L reconstructed from per-asset local-currency return series.",
                },
            },
            "scorecard": {
                "convention": "Time-weighted return (TWR), chain-linked over weekly sub-periods.",
                "empty": False, "depth_weeks": 6, "n_effective": 3, "p_floor_two_sided": 0.25,
                "actual_twr": 0.014, "do_nothing_twr": 0.009, "arms_coincide": False, "note": None,
                "meta": {"n_actual": 5, "n_donothing": 5, "base_composition_assumption": "nav as-is"},
            },
        },
        "market": {
            "engine": "market_research.MarketResearchEngine", "how_to_read": "Cross-asset context.",
            "regime": {
                "as_of": "2026-08-14", "state": "risk-on", "score": 0.42,
                "components": [
                    {"key": "trend", "label": "Trend", "value": 0.6, "score": 0.7, "weight": 0.3, "contribution": 0.21},
                    {"key": "breadth", "label": "Breadth", "value": 0.5, "score": 0.4, "weight": 0.2, "contribution": 0.08},
                ],
                "history": [
                    {"date": "2026-08-10", "score": 0.38}, {"date": "2026-08-11", "score": 0.40},
                    {"date": "2026-08-14", "score": 0.42},
                ],
                "recession_flag": False, "sahm": 0.1,
            },
            "cross_asset": {
                "as_of": "2026-08-14", "window_days": 90, "symbols": ["SPY", "TLT", "GLD"],
                "snapshot": [
                    {"symbol": "SPY", "label": "S&P 500", "last": 560.2, "chg_1d_pct": 0.3, "chg_1w_pct": 1.1, "chg_1m_pct": 2.4, "chg_ytd_pct": 14.2},
                    {"symbol": "TLT", "label": "20Y Treasury", "last": 92.1, "chg_1d_pct": -0.1, "chg_1w_pct": -0.4, "chg_1m_pct": 0.9, "chg_ytd_pct": -3.1},
                    {"symbol": "GLD", "label": "Gold", "last": 210.5, "chg_1d_pct": 0.5, "chg_1w_pct": 0.8, "chg_1m_pct": 3.2, "chg_ytd_pct": 22.1},
                ],
                "matrix": [[1.0, -0.3, 0.1], [-0.3, 1.0, 0.05], [0.1, 0.05, 1.0]],
                "avg_pairwise_corr": -0.05, "avg_corr_history": [], "ratios": [],
            },
            # Not surfaced by the redesigned pack (rule 7 scope) — must fold
            # into the "further fields recorded" residual note, never dump.
            "rotation": {"as_of": "2026-08-14", "benchmark": "SPY", "rows": []},
            "rrg": {"as_of": "2026-08-14", "benchmark": "SPY", "tail_weeks": 8, "series": []},
            "volatility": {
                "as_of": "2026-08-14", "vix": 14.2, "vix_percentile": 32.1, "vix3m": 15.8,
                "term_ratio": 1.11, "term_state": "contango", "realized_vol_pct": 11.4,
                "implied_minus_realized": 2.8, "move": 88.0, "vix_history": [],
            },
            "breadth": {
                "as_of": "2026-08-14", "session_date": "2026-08-14", "primary_market": "NYSE",
                "markets": [{
                    "market": "NYSE", "issues": 3000, "advances": 1800, "declines": 1100, "unchanged": 100,
                    "breadth_pct": 62.1, "ad_ratio": 1.64, "new_highs": 120, "new_lows": 30,
                    "net_highs": 90, "updown_vol_ratio": 1.2, "trin": 0.9,
                }],
                "history_days": 45, "warming_up": False, "ad_line": [],
                "mcclellan_osc": 34.5, "mcclellan_summation": 512.0, "mcclellan_history": [],
            },
            "sentiment": {
                "as_of": "2026-08-14", "fg_score": 62.0, "fg_rating": "Greed", "fg_prev_close": 60.0,
                "fg_prev_week": 55.0, "fg_prev_month": 48.0, "fg_prev_year": 50.0,
                "fg_components": [], "fg_history": [], "cot_as_of": "2026-08-08",
                "cot": [{
                    "code": "NQ", "label": "Nasdaq spec", "net": 13200.0, "oi": 400000.0, "pct_oi": 3.3,
                    "z_1y": 0.8, "percentile": 70.0, "change_1w": 500.0, "inverse": False,
                    "report_date": "2026-08-08",
                }],
            },
            "valuation": {
                "as_of": "2026-08-14",
                "metrics": [
                    {"key": "cape", "label": "CAPE", "value": 42.56, "percentile": 99.4, "note": "Shiller CAPE", "needs_source": False},
                    {"key": "erp", "label": "ERP", "value": None, "percentile": None, "note": "needs source", "needs_source": True},
                ],
            },
            "econ_calendar_week_ahead": {
                "data": {
                    "events": [
                        {"date": "2026-08-18", "country": "US", "event": "CPI", "actual": None, "previous": None, "consensus": None},
                        {"date": "2026-08-19", "country": "US", "event": "Retail Sales", "actual": None, "previous": None, "consensus": None},
                        {"date": "2026-08-18", "country": "US", "event": "CPI", "actual": None, "previous": None, "consensus": None},
                    ],
                    "needs_key": False,
                },
                "source": "fred", "stale": False,
            },
        },
        "holdings": {
            "engine": "research.ResearchEngine + signals.ScannerEngine + intel_service.IntelService",
            "how_to_read": "Per-symbol deep dive.",
            "symbols": {
                "BTC": {
                    "fundamentals": {"symbol": "BTC", "sector": "Crypto", "currency": "USD"},
                    "valuation": {"symbol": "BTC", "price": 44000.0, "market_cap": 900_000_000_000.0},
                    "forensic": {
                        "cards": [{"key": "piotroski", "label": "Piotroski F-Score", "score": 7.0, "verdict": "Strong"}],
                    },
                    "technicals": {
                        "trend": "uptrend", "rsi_14": 61.4,
                        "series": [{"date": "2026-08-10", "close": 41000.0}, {"date": "2026-08-11", "close": 44000.0}],
                    },
                    "scan": {"n_hits": 1},
                    # malicious, web-sourced news title — must come out escaped, not executable
                    "news": [{"title": "<script>alert('xss')</script> BTC rallies"}],
                },
            },
            "symbols_failed": {}, "n_symbols_failed": 0,
        },
        "quant": {
            "engine": "signals.SignalEngine", "how_to_read": "Within-book ordering, not a market factor reading.",
            "factor": {
                "factor": "momentum", "horizon_days": 21, "n_symbols": 6, "n_periods": 60,
                "ic_mean": 0.061, "ic_ir": 0.29, "long_short_spread": 0.036,
                "ic_series": [{"date": "2026-08-10", "ic": 0.11}],
                "quantile_returns": [{"quantile": 1, "mean_fwd_ret_pct": -1.2, "n_avg": 1.0}],
                "warnings": ["factor exposures are z-scored on the latest decision date only"],
            },
            "scoreboard": {
                "rows": [{
                    "signal_id": "mom_63d", "mean_rank_ic": 0.081, "hit_rate": 0.58,
                    "significant": True, "methodology": "cross-sectional",
                }],
                "warnings": ["today's holdings applied to past decision dates — not research-grade edge"],
            },
            "cross_section": {},
        },
        "shadow": {
            "engine": "shadow-verdict-engine-v1", "how_to_read": "Descriptive, evidence-only.",
            "nature": "descriptive, evidence-only, never a proposal", "k": 1, "grade_min_weeks": 4,
            "deflation_note": None, "priced_as_of": "2026-08-16T14:32:07.123456+00:00",
            "tools": {
                "technicals": {
                    "n_events": 3, "n_graded": 3, "effective_n": 2, "pushes": 1,
                    "hit_rate": 0.6, "benchmark_hit_rate": 0.55, "hit_rate_wilson_lb": 0.31,
                    "low_confidence": False, "warnings": [], "graded": True,
                },
                "forensic": {"n_events": 1, "graded": False, "reason": "horizon exceeds grading window"},
            },
        },
        "methodology": {
            "engine": "static", "how_to_read": "Static per-tool explainer.",
            "version": "phase17-s2-v1",
            "tools": [{
                "tool": "risk.RiskEngine", "measures": "VaR/CVaR", "endpoint": "/api/v1/risk/*",
                "limitations": "daily-close only", "grading_horizon": "n/a",
            }],
        },
    }


def _fixture_with_tail() -> dict:
    """Base fixture + 12 dust positions (weight < 1%) so the top-12-by-weight
    threshold (rule 4) is exercised for real: BTC/ETH stay material (weight
    >= 1%), and with 14 positions total the top-12 cap bites, pushing the two
    smallest dust rows into the tail/roster. Each dust symbol also gets a
    minimal `holdings.symbols` entry so `_holdings_view`'s material/roster
    split has real per-symbol data on both sides of the threshold."""
    sections = _fixture_sections()
    dust_positions = [
        {
            "symbol": f"DUST{i}", "qty": 1.0, "value_eur": 10.0 - i * 0.1,
            "weight_pct": 0.05 - i * 0.001, "wow_weight_delta_pct": None, "wow_value_delta_eur": None,
        }
        for i in range(12)
    ]
    sections["portfolio"]["positions"].extend(dust_positions)
    # Real holdings enumeration covers every held symbol, not just the one
    # the base fixture happened to hand-build (BTC) — ETH needs an entry too
    # so the material set (BTC + ETH + 10 dust) has a card for every member.
    sections["holdings"]["symbols"]["ETH"] = {
        "fundamentals": {"symbol": "ETH", "sector": "Crypto", "currency": "USD"},
        "valuation": {"symbol": "ETH", "price": 2500.0, "market_cap": 300_000_000_000.0},
        "forensic": {"cards": []},
        "technicals": {"trend": "flat", "series": []},
        "scan": {"n_hits": 0},
        "news": [],
    }
    for i in range(12):
        sections["holdings"]["symbols"][f"DUST{i}"] = {
            "fundamentals": {"symbol": f"DUST{i}", "sector": "Crypto", "currency": "USD"},
            "valuation": {"symbol": f"DUST{i}", "price": 0.01, "market_cap": 1000.0},
            "forensic": {"cards": []},
            "technicals": {"trend": "flat", "series": []},
            "scan": {"n_hits": 0},
            "news": [],
        }
    return sections


def test_environment_autoescape_is_enabled() -> None:
    assert _environment().autoescape is True


def test_render_html_escapes_malicious_news_title() -> None:
    html = _render_html(_fixture_sections(), "2026-W33")
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


def test_render_html_includes_all_seven_chapter_headings() -> None:
    html = _render_html(_fixture_sections(), "2026-W33")
    for title in (
        "Executive Summary", "Portfolio Review", "Market Context",
        "Holdings Deep-Dive", "Quant", "Shadow-Mode Tool Grading", "Methodology Appendix",
    ):
        assert title in html


def test_render_html_includes_eur_glyph_and_cover_fields() -> None:
    html = _render_html(_fixture_sections(), "2026-W33")
    assert "€" in html
    assert "2026-W33" in html
    assert "risk-on" in html  # cover.regime extracted from the real nested {"state": ...} dict


def test_render_html_absent_chapter_renders_reason_not_a_crash() -> None:
    sections = _fixture_sections()
    sections["market"] = {"absent": True, "reason": "MarketResearchEngine timed out"}
    html = _render_html(sections, "2026-W33")
    assert "MarketResearchEngine timed out" in html


def test_render_html_sets_render_done_flag_script_and_chart_data() -> None:
    html = _render_html(_fixture_sections(), "2026-W33")
    assert "data-render-done" in html
    assert "report-chart-data" in html
    assert "allocation_donut" in html
    assert "nav_line" in html
    assert "regime_history_line" in html
    assert "factor_ic_bars" in html
    assert "holding_spark_btc" in html


def test_render_warnings_absent_when_all_shapes_correct() -> None:
    html = _render_html(_fixture_sections(), "2026-W33")
    assert "Render Warnings" not in html


# ── redesign: no repr, ever ────────────────────────────────────────────
def test_render_html_never_contains_python_dict_repr() -> None:
    """The core ask of the redesign brief: it must be IMPOSSIBLE for a
    dict/list to print as a Python repr anywhere in the rendered PDF HTML —
    covers the old raw-dump culprits (market's daily history/correlation
    matrix/econ calendar, portfolio's coverage/scorecard dicts)."""
    html = _render_html(_fixture_sections(), "2026-W33")
    assert "{'" not in html
    assert "None," not in html
    assert "'date':" not in html


def test_render_html_never_contains_repr_even_with_tail_and_market_data() -> None:
    html = _render_html(_fixture_with_tail(), "2026-W33")
    assert "{'" not in html
    assert "'date':" not in html


# ── redesign: positions materiality threshold + tail collapse (rule 4) ───
def test_positions_table_collapses_tail_when_over_threshold() -> None:
    html = _render_html(_fixture_with_tail(), "2026-W33")
    assert "Tail: 2 positions" in html  # pluralized
    # material dust rows (top-12) still show individually; the two smallest
    # dust rows do not get their own positions-table row.
    assert "DUST0" in html
    assert "DUST11" not in html or "Tail:" in html  # DUST11 is the smallest, always tail-only


def test_positions_table_no_tail_row_under_threshold() -> None:
    html = _render_html(_fixture_sections(), "2026-W33")
    assert "Tail:" not in html


# ── redesign: holdings materiality (rule 8) ───────────────────────────────
def test_holdings_material_threshold_splits_cards_and_roster() -> None:
    html = _render_html(_fixture_with_tail(), "2026-W33")
    assert "Other holdings" in html
    assert 'class="holding-card"' in html
    # exactly the material set (BTC + ETH + 10 material dust rows) gets a card
    assert html.count('class="holding-card"') == 12


def test_holdings_roster_table_has_no_cards_for_non_material_symbols() -> None:
    html = _render_html(_fixture_with_tail(), "2026-W33")
    roster_section = html[html.index("Other holdings"):]
    assert "DUST11" in roster_section or "DUST10" in roster_section


# ── P0: materiality gate must never silently ────
# drop a material position with no tear-sheet data at all.
def test_material_position_with_no_holdings_entry_renders_absent_slot() -> None:
    """Regression: several material positions (>=1% weight, in the positions
    table) could have NO entry at
    all in `holdings.symbols` — not even a per-symbol `{"absent": True}`
    placeholder — and were silently dropped from the whole Holdings chapter
    while a €0.00 dust card still rendered. A material position with zero
    tear-sheet data must now render as part of the grouped absent block
    instead (fix round 7: no longer one full card per symbol — see
    `test_holdings_absent_symbols_collapse_into_one_grouped_block`)."""
    sections = _fixture_sections()
    sections["portfolio"]["positions"].append({
        "symbol": "ZZZ", "qty": 10.0, "value_eur": 1280.0, "weight_pct": 12.0,
        "wow_weight_delta_pct": None, "wow_value_delta_eur": None,
    })
    # deliberately NOT added to holdings.symbols at all — the no-entry shape
    html = _render_html(sections, "2026-W33")
    assert "ZZZ" in html
    assert "ZZZ (12.0%)" in html  # weight still shown, grouped-sentence style
    # ETH (base fixture, also material, no holdings.symbols entry at all)
    # shares the exact same reason — grouped into the SAME sentence, not a
    # second near-identical line (the fix this test now proves).
    assert "Tear sheet unavailable — no data recorded this week: ZZZ (12.0%), ETH (3.0%)." in html
    assert '<div class="holding-card">' not in html[html.index("ZZZ (12.0%)") - 200:html.index("ZZZ (12.0%)")]


def test_material_position_with_real_absent_reason_still_shows_it() -> None:
    """A genuine per-symbol failure (`{"absent": True, "reason": ...}`) is a
    DIFFERENT case from "no entry recorded at all" — the real reason must
    still print, never overwritten by the generic no-data-recorded message,
    and never merged into a group it doesn't belong to (fix round 7:
    distinct reasons get distinct groups)."""
    sections = _fixture_sections()
    sections["holdings"]["symbols"]["BTC"] = {"absent": True, "reason": "delisted ticker"}
    html = _render_html(sections, "2026-W33")
    assert "Tear sheet unavailable — delisted ticker: BTC (41.2%)." in html
    # ETH (also material, no fixture entry at all) legitimately DOES get the
    # generic no-data-recorded message in this same fixture — but as its OWN
    # group, never folded into BTC's unrelated "delisted ticker" line.
    assert "Tear sheet unavailable — no data recorded this week: ETH (3.0%)." in html
    delisted_line_start = html.index("delisted ticker")
    delisted_line = html[delisted_line_start:delisted_line_start + 40]
    assert "ETH" not in delisted_line


def test_holdings_absent_symbols_collapse_into_one_grouped_block() -> None:
    """Many material positions with no tear-sheet entry must not produce
    near-identical cards, a page of one repeated sentence. They must collapse into ONE block
    after the real cards, not one card per symbol."""
    sections = _fixture_sections()
    for i, sym in enumerate(["AAA", "BBB", "CCC"]):
        sections["portfolio"]["positions"].append({
            "symbol": sym, "qty": 1.0, "value_eur": 100.0, "weight_pct": 9.0 - i,
            "wow_weight_delta_pct": None, "wow_value_delta_eur": None,
        })
    html = _render_html(sections, "2026-W33")
    assert html.count("Tear sheet unavailable — no data recorded this week:") == 1
    assert "AAA (9.0%)" in html and "BBB (8.0%)" in html and "CCC (7.0%)" in html
    # only the ONE real card (BTC) uses the boxed .holding-card wrapper —
    # the absent group is a distinct, unboxed block.
    assert html.count('<div class="holding-card">') == 1
    assert 'class="holding-absent-group"' in html


# ── redesign: forensic n/a-for-crypto + pattern-scan-zero suppression ────
def test_forensic_blank_crypto_note_when_all_scores_blank() -> None:
    sections = _fixture_sections()
    sections["holdings"]["symbols"]["BTC"]["forensic"]["cards"] = [
        {"key": "piotroski", "label": "Piotroski F-Score", "score": None, "verdict": None},
    ]
    html = _render_html(sections, "2026-W33")
    assert "Forensic scores n/a for crypto." in html


def test_pattern_scan_zero_hits_not_printed() -> None:
    sections = _fixture_sections()
    sections["holdings"]["symbols"]["BTC"]["scan"]["n_hits"] = 0
    html = _render_html(sections, "2026-W33")
    assert "Pattern scan" not in html


def test_pattern_scan_nonzero_hits_still_printed() -> None:
    html = _render_html(_fixture_sections(), "2026-W33")
    assert "Pattern scan: 1 hit." in html  # pluralized


# ── redesign: scorecard insufficient-history one-liner (rule 6) ──────────
def test_scorecard_insufficient_history_renders_one_sentence() -> None:
    sections = _fixture_sections()
    sections["portfolio"]["scorecard"] = {
        "convention": "TWR...", "empty": False, "depth_weeks": 1, "n_effective": 0,
        "p_floor_two_sided": None, "actual_twr": None, "do_nothing_twr": None,
        "arms_coincide": None, "note": "insufficient history for a return calculation",
    }
    html = _render_html(sections, "2026-W33")
    assert "Return scorecard: insufficient history — needs ≥2 snapshot weeks." in html
    assert 'data-chart="nav_line"' not in html


def test_scorecard_sufficient_history_renders_chart_and_figures() -> None:
    html = _render_html(_fixture_sections(), "2026-W33")
    assert 'data-chart="nav_line"' in html
    assert "Return scorecard: insufficient history" not in html


# ── redesign: shadow roll-up reads cleanly when nothing has a hit rate ───
def test_shadow_rollup_all_graded_but_no_hit_rate_reads_cleanly() -> None:
    """A tool can be `graded: true` (the scoreboard attempted it) yet still
    have `hit_rate: None` (too few events to score) — the roll-up must not
    read as "toolname ungraded, toolname ungraded ... hit rate this week"."""
    sections = _fixture_sections()
    sections["shadow"]["tools"]["technicals"]["hit_rate"] = None
    sections["shadow"]["tools"]["forensic"] = {"n_events": 2, "graded": True, "hit_rate": None}
    html = _render_html(sections, "2026-W33")
    assert "ungraded hit rate this week" not in html
    assert "none with enough events yet for a hit rate" in html


# ── redesign: "In plain terms" panel (rule 3) ─────────────────────────────
def test_plain_terms_panel_present_with_real_values() -> None:
    html = _render_html(_fixture_sections(), "2026-W33")
    assert "In plain terms" in html
    assert "The portfolio is worth €48,512.30." in html
    assert "one-day loss should stay under" in html
    assert "evidence-only scoring tool" in html


def test_plain_terms_omits_var_sentence_when_var_missing() -> None:
    sections = _fixture_sections()
    sections["portfolio"]["var_cvar"]["var_eur"] = None
    sections["portfolio"]["var_cvar"]["cvar_eur"] = None
    html = _render_html(sections, "2026-W33")
    assert "one-day loss should stay under" not in html
    # other value-gated sentences still render
    assert "The portfolio is worth €48,512.30." in html


def test_plain_terms_omits_cash_sentence_when_cash_missing() -> None:
    sections = _fixture_sections()
    sections["portfolio"]["cash_eur"] = None
    html = _render_html(sections, "2026-W33")
    assert "Cash is" not in html


# ── redesign: market — correlation pairs, never the raw matrix ───────────
def test_market_correlation_pairs_rendered_not_raw_matrix() -> None:
    html = _render_html(_fixture_sections(), "2026-W33")
    assert "Highest correlated pairs" in html
    assert "Lowest correlated pairs" in html
    # autoescape turns "&" into "&amp;" ("S&P 500" -> "S&amp;P 500") — assert
    # around the escaped form, not through it.
    assert "S&amp;P 500 / 20Y Treasury" in html or "S&amp;P 500 / Gold" in html
    assert "[[1.0" not in html
    assert "[1.0, -0.3" not in html


def test_market_regime_components_table_and_history_chart() -> None:
    html = _render_html(_fixture_sections(), "2026-W33")
    assert "Trend" in html
    assert 'data-chart="regime_history_line"' in html


def test_market_regime_missing_components_render_as_footnote_not_dash_rows() -> None:
    """Rule 7: a component with no score this week (e.g. a warming-up data
    source) drops into ONE muted footnote line — never a table row of
    em-dash score/contribution cells sitting next to real ones."""
    sections = _fixture_sections()
    sections["market"]["regime"]["components"].append(
        {"key": "credit", "label": "Credit", "value": None, "score": None, "weight": 0.2, "contribution": None},
    )
    html = _render_html(sections, "2026-W33")
    assert "Not available this week: Credit." in html
    # the real components still get a full row
    assert "<td>Trend</td>" in html


def test_market_leftover_note_removed_per_design_fix_round() -> None:
    """The prior pass's "N further field(s) recorded" residual note (for
    market fields with no bespoke reader — rotation/rrg) sat directly under
    the calendar table with no visual separation and read as if it were
    ABOUT the calendar's hidden rows rather than unrelated rotation/rrg data
    — removed per the design review fix round (P1.9), not replaced. Base
    fixture still carries `rotation`/`rrg` (unused, real producer shape) to
    prove their presence alone no longer prints anything."""
    sections = _fixture_sections()
    assert "rotation" in sections["market"] and "rrg" in sections["market"]
    html = _render_html(sections, "2026-W33")
    assert "field(s) recorded" not in html


def test_market_valuation_headline_stat_with_percentile() -> None:
    html = _render_html(_fixture_sections(), "2026-W33")
    assert "CAPE" in html
    assert "42.56" in html
    assert "99.4th percentile" in html
    assert "Sources needed: ERP" in html


# ── redesign: econ calendar — dedupe, cap, column-drop (rule 7) ──────────
def test_econ_calendar_dedupes_events() -> None:
    html = _render_html(_fixture_sections(), "2026-W33")
    assert html.count("<td>CPI</td>") + html.count(">CPI<") <= 1 or html.count("CPI") <= 2


def test_econ_calendar_drops_actual_columns_when_all_none() -> None:
    html = _render_html(_fixture_sections(), "2026-W33")
    # the fixture's events are all-None actual/previous/consensus (real FRED
    # calendar shape) — those columns must not appear at all.
    assert "<th>Actual</th>" not in html
    assert "<th>Previous</th>" not in html
    assert "<th>Consensus</th>" not in html


# ── redesign: calendar keyword-tier ranking ─────────
def test_econ_calendar_ranks_by_keyword_tier_and_caps_at_eight() -> None:
    sections = _fixture_sections()
    tier1 = [
        {"date": f"2026-08-{18 + i:02d}", "country": "US", "event": name, "actual": None, "previous": None, "consensus": None}
        for i, name in enumerate([
            "FOMC Press Release", "CPI Report", "Nonfarm Payrolls", "GDP Growth Rate",
            "Employment Situation", "HICP Flash Estimate", "Core Inflation Rate", "FOMC Minutes",
            "Payrolls Revision", "GDP Second Estimate",
        ])
    ]
    tier3_noise = [
        {"date": "2026-08-17", "country": "US", "event": f"H.15 Interest Rates {i}", "actual": None, "previous": None, "consensus": None}
        for i in range(5)
    ]
    sections["market"]["econ_calendar_week_ahead"]["data"]["events"] = tier1 + tier3_noise
    html = _render_html(sections, "2026-W33")
    assert "FOMC Press Release" in html  # tier 1, always shown
    assert "H.15 Interest Rates" not in html  # tier 3, dropped entirely
    # at most 8 tier-1/2 rows even though 10 tier-1 events were offered
    assert html.count("Payrolls") <= 2  # "Nonfarm Payrolls" + "Payrolls Revision" — sanity, not the cap itself


# ── fix round 7: dedupe by normalized title, ──────
# BEFORE the tier cap — a real render showed 4 BLS state variants of the
# same release filling rows 5-8 of the 8-row cap.
def test_econ_calendar_dedupes_state_variants_by_normalized_title_before_cap() -> None:
    sections = _fixture_sections()
    sections["market"]["econ_calendar_week_ahead"]["data"]["events"] = [
        {"date": "2026-08-18", "country": "US", "event": "Employment Situation", "actual": None, "previous": None, "consensus": None},
        {"date": "2026-08-19", "country": "US", "event": "Employment Situation - California", "actual": None, "previous": None, "consensus": None},
        {"date": "2026-08-19", "country": "US", "event": "Employment Situation - Texas", "actual": None, "previous": None, "consensus": None},
        {"date": "2026-08-19", "country": "US", "event": "Employment Situation - New York", "actual": None, "previous": None, "consensus": None},
        {"date": "2026-08-20", "country": "US", "event": "CPI Report", "actual": None, "previous": None, "consensus": None},
        {"date": "2026-08-21", "country": "US", "event": "GDP Growth Rate", "actual": None, "previous": None, "consensus": None},
    ]
    html = _render_html(sections, "2026-W33")
    # one normalized row for the whole state-variant cohort, not four
    assert html.count("Employment Situation") == 1
    assert "California" not in html
    assert "Texas" not in html
    # genuinely distinct tier-1 releases still get their own rows
    assert "CPI Report" in html
    assert "GDP Growth Rate" in html


def test_econ_calendar_drops_tier3_only_events_entirely() -> None:
    sections = _fixture_sections()
    sections["market"]["econ_calendar_week_ahead"]["data"]["events"] = [
        {"date": "2026-08-18", "country": "US", "event": f"Event {i}", "actual": None, "previous": None, "consensus": None}
        for i in range(30)
    ]
    html = _render_html(sections, "2026-W33")  # must not raise
    assert "Event 0" not in html
    # every offered event is tier-3 noise — the section has nothing to show
    # and prints nothing (no heading, no empty table), same collapse
    # discipline as every other "nothing this week" chapter.
    assert "Week-ahead calendar" not in html


# ── redesign: cover masthead / KPI band / TOC (rule 1) ────────────────────
def test_cover_kpi_band_and_toc_present() -> None:
    html = _render_html(_fixture_sections(), "2026-W33")
    assert "CATALLAX" in html.upper()
    assert "VaR 95%" in html
    assert "Cash" in html
    assert "Contents" in html
    assert "Executive Summary" in html


# ── redesign: methodology conventions as prose (rule 5) ──────────────────
def test_methodology_conventions_render_as_prose_not_dict() -> None:
    html = _render_html(_fixture_sections(), "2026-W33")
    # autoescape turns "&" into "&amp;" — assert around it, not through it.
    assert "Daily EUR P&amp;L reconstructed from per-asset local-currency return series." in html
    assert "Time-weighted return (TWR), chain-linked over weekly sub-periods." in html


# ── design review fix round: P1/P2 items ───────────────────────────────────
def test_quantile_returns_collapses_when_all_forward_returns_null() -> None:
    sections = _fixture_sections()
    sections["quant"]["factor"]["quantile_returns"] = [
        {"quantile": i, "mean_fwd_ret_pct": None, "n_avg": 1.280851} for i in range(1, 6)
    ]
    html = _render_html(sections, "2026-W33")
    assert "Quantile returns unavailable — universe too small." in html
    assert "1.280851" not in html  # never >4 sig figs, and no degenerate table at all
    assert "<h3>Quantile returns</h3>" not in html


def test_quantile_returns_table_renders_when_real_and_n_avg_rounded() -> None:
    html = _render_html(_fixture_sections(), "2026-W33")  # base fixture has a real -1.2% return
    assert "<h3>Quantile returns</h3>" in html
    assert "1.00" in html  # n_avg=1.0 rounded to 2dp via |num(2), not a raw repr


def test_shadow_warnings_collapse_to_one_banner_for_shared_low_confidence() -> None:
    sections = _fixture_sections()
    shared_warnings = ["only 1 week(s) of shadow_verdicts history (< 4) — indicative only"]
    sections["shadow"]["tools"] = {
        "factor": {"n_events": 4, "n_graded": 0, "graded": True, "low_confidence": True, "warnings": shared_warnings},
        "signals": {"n_events": 2, "n_graded": 0, "graded": True, "low_confidence": True, "warnings": shared_warnings},
        "patterns": {"n_events": 2, "n_graded": 0, "graded": True, "low_confidence": True, "warnings": shared_warnings},
        "technicals": {"n_events": 8, "n_graded": 0, "graded": True, "low_confidence": True, "warnings": shared_warnings},
        "forensic": {"n_events": 1, "graded": False, "reason": "horizon exceeds grading window"},
    }
    html = _render_html(sections, "2026-W33")
    assert "All 4 graded tools: 0 effective events, fewer than 4 weeks of history" in html
    assert "hit rates indicative only" in html
    # the old one-line-per-tool repetition must be gone — the shared boilerplate
    # warning string appears zero times (it's summarized, not concatenated),
    # and the per-tool "low confidence" line is gone entirely (folded into the banner).
    assert html.count("low confidence (insufficient shadow_verdicts history)") == 0
    assert html.count("only 1 week(s) of shadow_verdicts history") == 0


def test_shadow_warnings_genuine_exception_still_shown_per_tool() -> None:
    sections = _fixture_sections()
    sections["shadow"]["tools"]["technicals"]["warnings"] = [
        "overlap veto: technicals and forensic both fired on BTC this week",
    ]
    sections["shadow"]["tools"]["technicals"]["low_confidence"] = False
    html = _render_html(sections, "2026-W33")
    assert "technicals — overlap veto: technicals and forensic both fired on BTC this week" in html


def test_allocation_donut_other_slice_always_neutral_grey() -> None:
    html = _render_html(_fixture_with_tail(), "2026-W33")  # 14 positions -> triggers "Other"
    idx = html.index('<span class="lbl">Other</span>')
    li_start = html.rindex("<li>", 0, idx)
    assert 'background:#999999;' in html[li_start:idx]


def test_allocation_donut_no_repeated_color_across_eight_real_slices() -> None:
    html = _render_html(_fixture_with_tail(), "2026-W33")
    legend_block = html[html.index('<ul class="legend">'):html.index("</ul>")]
    colors = re.findall(r"background:(#[0-9a-fA-F]{6});", legend_block)
    real_colors = colors[:-1] if "Other" in legend_block else colors  # drop the fixed grey "Other" swatch
    assert len(real_colors) == len(set(real_colors))  # every real position gets a distinct hue


def test_holding_news_headline_has_substring_match_suffix() -> None:
    html = _render_html(_fixture_sections(), "2026-W33")
    assert "· substring match" in html


def test_holding_card_fixed_skeleton_shows_em_dash_for_missing_fields() -> None:
    """P2.16: every material card renders the SAME five slots (score,
    sector, price, trend, RSI) regardless of what data is actually
    available — missing ones show an em-dash row, never a shorter card."""
    sections = _fixture_sections()
    sections["holdings"]["symbols"]["BTC"]["fundamentals"] = {}  # no sector
    sections["holdings"]["symbols"]["BTC"]["technicals"] = {"series": []}  # no trend/RSI
    html = _render_html(sections, "2026-W33")
    card = html[html.index('<div class="holding-card">'):html.index("Other holdings") if "Other holdings" in html else len(html)]
    assert '<span class="k">Sector</span><span>—</span>' in card
    assert '<span class="k">Trend</span><span>—</span>' in card
    assert '<span class="k">RSI 14</span><span>—</span>' in card


def test_positions_table_drops_wow_columns_when_all_null() -> None:
    sections = _fixture_sections()
    for p in sections["portfolio"]["positions"]:
        p["wow_weight_delta_pct"] = None
        p["wow_value_delta_eur"] = None
    html = _render_html(sections, "2026-W33")
    assert "<th>WoW %pt</th>" not in html
    assert "<th>WoW €</th>" not in html
    assert "Week-over-week deltas unavailable — first snapshot week." in html


def test_positions_table_keeps_wow_columns_when_any_row_has_data() -> None:
    html = _render_html(_fixture_sections(), "2026-W33")  # base fixture: BTC has real WoW figures
    assert "<th>WoW %pt</th>" in html
    assert "Week-over-week deltas unavailable" not in html


def test_regime_history_chart_carries_date_labels_for_axis() -> None:
    html = _render_html(_fixture_sections(), "2026-W33")
    assert '"x_labels": ["2026-08-10", "2026-08-11", "2026-08-14"]' in html


# ── fix round: present-but-wrong-shaped known fields must never raise ────
# Real top-level structured fields per the (now-aligned) normalization tables.
_WRONG_SHAPE_CASES = [
    ("methodology", "tools"),
    ("portfolio", "positions"),
    ("portfolio", "var_cvar"),
    ("quant", "factor"),
    ("quant", "scoreboard"),
    ("holdings", "symbols"),
    ("shadow", "tools"),
]


@pytest.mark.parametrize("chapter_key,field", _WRONG_SHAPE_CASES)
def test_normalize_sections_drops_wrong_shaped_known_field_and_warns(chapter_key, field) -> None:
    sections = _fixture_sections()
    sections[chapter_key][field] = "oops"
    normalized, warnings = _normalize_sections(sections)
    dropped = normalized[chapter_key][field]
    assert not isinstance(dropped, str)  # "oops" replaced by a safe list/dict
    # dict-guarded fields (factor/scoreboard/symbols/tools/var_cvar) may come back
    # as a dict re-populated with empty nested lists (the bespoke per-chapter
    # guards still run over it) rather than a bare `{}` — either is "no data",
    # never the original string.
    assert dropped == [] or (isinstance(dropped, dict) and not any(
        v not in ([], {}, None) for v in dropped.values()
    ))
    assert any(w.startswith(f"{chapter_key}.{field}:") for w in warnings)


@pytest.mark.parametrize("chapter_key,field", _WRONG_SHAPE_CASES)
def test_render_html_never_raises_on_wrong_shape_and_surfaces_warning(chapter_key, field) -> None:
    sections = _fixture_sections()
    sections[chapter_key][field] = "oops"
    html = _render_html(sections, "2026-W33")  # must not raise
    assert "Render Warnings" in html
    assert f"{chapter_key}.{field}:" in html


def test_normalize_sections_handles_all_wrong_shape_cases_simultaneously() -> None:
    sections = _fixture_sections()
    for chapter_key, field in _WRONG_SHAPE_CASES:
        sections[chapter_key][field] = "oops"
    normalized, warnings = _normalize_sections(sections)
    assert len(warnings) == len(_WRONG_SHAPE_CASES)
    html = _render_html(sections, "2026-W33")
    for chapter_key, field in _WRONG_SHAPE_CASES:
        assert f"{chapter_key}.{field}:" in html


def test_normalize_sections_chapter_itself_wrong_type_renders_as_empty() -> None:
    """Not a top-level-field case, but the same bug class one level up — a
    whole chapter value (not just a known field within it) can be
    wrong-shaped too."""
    sections = _fixture_sections()
    sections["market"] = "oops"
    normalized, warnings = _normalize_sections(sections)
    assert normalized["market"] == {}
    assert any(w.startswith("market.<chapter>:") for w in warnings)
    html = _render_html(sections, "2026-W33")  # must not raise
    assert "Market Context" in html


def test_normalize_sections_holdings_per_symbol_wrong_shaped_subfields() -> None:
    sections = _fixture_sections()
    sections["holdings"]["symbols"]["BTC"]["technicals"] = "oops"
    sections["holdings"]["symbols"]["BTC"]["fundamentals"] = "oops"
    normalized, warnings = _normalize_sections(sections)
    btc = normalized["holdings"]["symbols"]["BTC"]
    # technicals gets bespoke re-processing (adds an empty `series` back) —
    # still "no data", never the original "oops" string; fundamentals has no
    # bespoke sub-processing and stays a bare `{}`.
    assert btc["technicals"] == {"series": []}
    assert btc["fundamentals"] == {}
    assert any("symbols[BTC].technicals:" in w for w in warnings)
    assert any("symbols[BTC].fundamentals:" in w for w in warnings)
    html = _render_html(sections, "2026-W33")  # must not raise
    assert "Render Warnings" in html


# ── fix round 2/3/5: numeric leaves fed through a |pct/|num/|eur filter ──
def _mutate_nav_eur(s, v): s["exec_summary"]["nav_eur"] = v
def _get_nav_eur(s): return s["exec_summary"]["nav_eur"]


def _mutate_wow_pct(s, v): s["exec_summary"]["wow_pct"] = v
def _get_wow_pct(s): return s["exec_summary"]["wow_pct"]


def _mutate_position_value_eur(s, v): s["portfolio"]["positions"][0]["value_eur"] = v
def _get_position_value_eur(s): return s["portfolio"]["positions"][0]["value_eur"]


def _mutate_position_weight_pct(s, v): s["portfolio"]["positions"][0]["weight_pct"] = v
def _get_position_weight_pct(s): return s["portfolio"]["positions"][0]["weight_pct"]


def _mutate_position_wow_weight_delta_pct(s, v): s["portfolio"]["positions"][0]["wow_weight_delta_pct"] = v
def _get_position_wow_weight_delta_pct(s): return s["portfolio"]["positions"][0]["wow_weight_delta_pct"]


def _mutate_position_wow_value_delta_eur(s, v): s["portfolio"]["positions"][0]["wow_value_delta_eur"] = v
def _get_position_wow_value_delta_eur(s): return s["portfolio"]["positions"][0]["wow_value_delta_eur"]


def _mutate_holding_rsi(s, v): s["holdings"]["symbols"]["BTC"]["technicals"]["rsi_14"] = v
def _get_holding_rsi(s): return s["holdings"]["symbols"]["BTC"]["technicals"]["rsi_14"]


def _mutate_holding_price(s, v): s["holdings"]["symbols"]["BTC"]["valuation"]["price"] = v
def _get_holding_price(s): return s["holdings"]["symbols"]["BTC"]["valuation"]["price"]


def _mutate_factor_ic_mean(s, v): s["quant"]["factor"]["ic_mean"] = v
def _get_factor_ic_mean(s): return s["quant"]["factor"]["ic_mean"]


def _mutate_scoreboard_mean_rank_ic(s, v): s["quant"]["scoreboard"]["rows"][0]["mean_rank_ic"] = v
def _get_scoreboard_mean_rank_ic(s): return s["quant"]["scoreboard"]["rows"][0]["mean_rank_ic"]


def _mutate_shadow_hit_rate(s, v): s["shadow"]["tools"]["technicals"]["hit_rate"] = v
def _get_shadow_hit_rate(s): return s["shadow"]["tools"]["technicals"]["hit_rate"]


def _mutate_shadow_benchmark_hit_rate(s, v): s["shadow"]["tools"]["technicals"]["benchmark_hit_rate"] = v
def _get_shadow_benchmark_hit_rate(s): return s["shadow"]["tools"]["technicals"]["benchmark_hit_rate"]


def _mutate_shadow_wilson_lb(s, v): s["shadow"]["tools"]["technicals"]["hit_rate_wilson_lb"] = v
def _get_shadow_wilson_lb(s): return s["shadow"]["tools"]["technicals"]["hit_rate_wilson_lb"]


def _mutate_var_cvar_var_eur(s, v): s["portfolio"]["var_cvar"]["var_eur"] = v
def _get_var_cvar_var_eur(s): return s["portfolio"]["var_cvar"]["var_eur"]


def _mutate_var_cvar_coverage_n_covered(s, v): s["portfolio"]["var_cvar"]["coverage"]["n_covered"] = v
def _get_var_cvar_coverage_n_covered(s): return s["portfolio"]["var_cvar"]["coverage"]["n_covered"]


def _mutate_scorecard_actual_twr(s, v): s["portfolio"]["scorecard"]["actual_twr"] = v
def _get_scorecard_actual_twr(s): return s["portfolio"]["scorecard"]["actual_twr"]


_NUMERIC_LEAF_CASES = [
    (_mutate_nav_eur, _get_nav_eur, "exec_summary.nav_eur:"),
    (_mutate_wow_pct, _get_wow_pct, "exec_summary.wow_pct:"),
    (_mutate_position_value_eur, _get_position_value_eur, "portfolio.positions[BTC].value_eur:"),
    (_mutate_position_weight_pct, _get_position_weight_pct, "portfolio.positions[BTC].weight_pct:"),
    (_mutate_position_wow_weight_delta_pct, _get_position_wow_weight_delta_pct, "portfolio.positions[BTC].wow_weight_delta_pct:"),
    (_mutate_position_wow_value_delta_eur, _get_position_wow_value_delta_eur, "portfolio.positions[BTC].wow_value_delta_eur:"),
    (_mutate_holding_rsi, _get_holding_rsi, "holdings.symbols[BTC].technicals.rsi_14:"),
    (_mutate_holding_price, _get_holding_price, "holdings.symbols[BTC].valuation.price:"),
    (_mutate_factor_ic_mean, _get_factor_ic_mean, "quant.factor.ic_mean:"),
    (_mutate_scoreboard_mean_rank_ic, _get_scoreboard_mean_rank_ic, "quant.scoreboard.rows[mom_63d].mean_rank_ic:"),
    (_mutate_shadow_hit_rate, _get_shadow_hit_rate, "shadow.tools[technicals].hit_rate:"),
    (_mutate_shadow_benchmark_hit_rate, _get_shadow_benchmark_hit_rate, "shadow.tools[technicals].benchmark_hit_rate:"),
    (_mutate_shadow_wilson_lb, _get_shadow_wilson_lb, "shadow.tools[technicals].hit_rate_wilson_lb:"),
    (_mutate_var_cvar_var_eur, _get_var_cvar_var_eur, "portfolio.var_cvar.var_eur:"),
    (_mutate_var_cvar_coverage_n_covered, _get_var_cvar_coverage_n_covered, "portfolio.var_cvar.coverage.n_covered:"),
    (_mutate_scorecard_actual_twr, _get_scorecard_actual_twr, "portfolio.scorecard.actual_twr:"),
]


@pytest.mark.parametrize("mutate,get,warning_prefix", _NUMERIC_LEAF_CASES)
def test_render_html_never_raises_on_wrong_shaped_numeric_leaf(mutate, get, warning_prefix) -> None:
    sections = _fixture_sections()
    mutate(sections, "oops")
    html = _render_html(sections, "2026-W33")  # must not raise
    assert "Render Warnings" in html
    assert warning_prefix in html


@pytest.mark.parametrize("mutate,get,warning_prefix", _NUMERIC_LEAF_CASES)
def test_normalize_sections_drops_wrong_shaped_numeric_leaf_and_warns(mutate, get, warning_prefix) -> None:
    sections = _fixture_sections()
    mutate(sections, "oops")
    normalized, warnings = _normalize_sections(sections)
    assert get(normalized) is None
    assert any(w.startswith(warning_prefix) for w in warnings)


@pytest.mark.parametrize("mutate,get,warning_prefix", _NUMERIC_LEAF_CASES)
def test_normalize_sections_coerces_numeric_string_cleanly_without_warning(mutate, get, warning_prefix) -> None:
    sections = _fixture_sections()
    mutate(sections, "42.5")  # numeric string — must coerce, not warn
    normalized, warnings = _normalize_sections(sections)
    assert get(normalized) == pytest.approx(42.5)
    assert not any(w.startswith(warning_prefix) for w in warnings)


def test_normalize_sections_handles_all_numeric_leaves_wrong_simultaneously() -> None:
    sections = _fixture_sections()
    for mutate, _get, _prefix in _NUMERIC_LEAF_CASES:
        mutate(sections, "oops")
    normalized, warnings = _normalize_sections(sections)
    assert len(warnings) == len(_NUMERIC_LEAF_CASES)
    html = _render_html(sections, "2026-W33")  # must not raise
    for _mutate, _get, prefix in _NUMERIC_LEAF_CASES:
        assert prefix in html


# ── fix round 4: PDF honesty ──────────
def test_render_html_narration_renders_as_escaped_text() -> None:
    html = _render_html(_fixture_sections(), "2026-W33")
    assert "NAV rose 1.42% this week, led by BTC." in html
    assert "Not narrated" not in html


def test_render_html_narration_absent_reason_renders_small_muted_when_no_narration() -> None:
    sections = _fixture_sections()
    sections["exec_summary"]["narration"] = None
    sections["exec_summary"]["narration_absent_reason"] = "not configured in this deployment"
    html = _render_html(sections, "2026-W33")
    assert "Not narrated: not configured in this deployment" in html
    # rendered via the muted `.how-to-read` style, not the reddish `.absent` one
    assert '<p class="how-to-read">Not narrated:' in html


def test_render_html_narration_malicious_script_renders_escaped() -> None:
    sections = _fixture_sections()
    sections["exec_summary"]["narration"] = "NAV up. <script>alert('xss')</script> Also BTC rallied."
    html = _render_html(sections, "2026-W33")
    assert "<script>alert" not in html
    assert "&lt;script&gt;" in html


@pytest.mark.parametrize("field", ["narration", "narration_absent_reason"])
def test_normalize_sections_drops_wrong_shaped_narration_string_and_warns(field) -> None:
    sections = _fixture_sections()
    sections["exec_summary"][field] = {"not": "a string"}
    normalized, warnings = _normalize_sections(sections)
    assert normalized["exec_summary"][field] is None
    assert any(w.startswith(f"exec_summary.{field}:") for w in warnings)
    html = _render_html(sections, "2026-W33")  # must not raise
    assert "Render Warnings" in html
    assert f"exec_summary.{field}:" in html


# ── feature: per-chapter "In plain terms" boxes ───────────────────────────
# Every analytical chapter (Portfolio Review / Market Context / Holdings
# Deep-Dive / Quant / Shadow-Mode Tool Grading) gets its own box, rendered
# via the shared `plain_terms_box` Jinja macro; Methodology gets none.
def _plain_terms_box_divs(html: str) -> list[str]:
    """Extracts the six (or fewer) `<div class="plain-terms">...</div>`
    blocks in document order — exec, portfolio, market, holdings, quant,
    shadow — so tests can assert against exactly one box's content without
    relying on brittle chapter-id string slicing that can accidentally
    capture neighbouring chapters' text."""
    return re.findall(r'<div class="plain-terms">.*?</div>', html, re.S)


def test_six_plain_terms_boxes_present_methodology_has_none() -> None:
    html = _render_html(_fixture_sections(), "2026-W33")
    # P3a: the label is a styled `<p>`, not an `<h4>` (a real heading here
    # would skip levels in the document outline — the box is an aside
    # interpreting the chapter, not a new section of it).
    assert html.count('<p class="plain-terms-label">In plain terms</p>') == 6
    assert "<h4>In plain terms</h4>" not in html


def test_plain_terms_boxes_em_dash_budget() -> None:
    """P1e: box prose must read as split clauses
    (periods), not the "statement — gloss" em-dash construction. Budget: at
    most 2 em dashes across all boxes combined.

    Fix-first #1: the calibration chapter's box was 3 em
    dashes all on its own, invisible to this test because the base fixture
    never carries a `calibration` key — adding it here is what actually
    covers the calibration box's prose, not just the six earlier boxes."""
    sections = _fixture_sections()
    sections["calibration"] = _fixture_calibration()
    html = _render_html(sections, "2026-W33")
    boxes = _plain_terms_box_divs(html)
    assert len(boxes) == 7
    total_em_dashes = sum(b.count("—") for b in boxes)
    assert total_em_dashes <= 2, f"{total_em_dashes} em dash(es) across boxes: {boxes}"


def test_plain_terms_portfolio_box_has_largest_holding_sentence() -> None:
    """BTC's 41.2% weight clears the 25% concentration threshold (P1a)."""
    html = _render_html(_fixture_sections(), "2026-W33")
    assert "BTC alone is 41.2% of the portfolio. Concentration is the dominant risk factor here." in html


def test_plain_terms_portfolio_box_below_threshold_omits_concentration_clause() -> None:
    """P1a: below 25% weight, only the factual "largest holding" clause
    prints — the "dominant risk factor" framing is gated off."""
    sections = _fixture_sections()
    sections["portfolio"]["positions"][0]["weight_pct"] = 20.0  # BTC, was 41.2
    html = _render_html(sections, "2026-W33")
    assert "BTC is the largest holding at 20.0%." in html
    assert "dominant risk factor" not in html


def test_plain_terms_portfolio_box_at_threshold_prints_concentration_clause() -> None:
    """P1a boundary: exactly 25.0% still counts as "at or above" the floor."""
    sections = _fixture_sections()
    sections["portfolio"]["positions"][0]["weight_pct"] = 25.0
    html = _render_html(sections, "2026-W33")
    assert "BTC alone is 25.0% of the portfolio. Concentration is the dominant risk factor here." in html


def test_plain_terms_portfolio_box_has_cash_and_var_and_scorecard_sentences() -> None:
    html = _render_html(_fixture_sections(), "2026-W33")
    assert "Cash, including stablecoins, is" in html
    # base fixture: actual_twr 0.014 > do_nothing_twr 0.009, depth_weeks 6 >= 4
    assert "actively managing the portfolio has done better than simply leaving it alone." in html


def test_plain_terms_portfolio_box_omits_cash_sentence_when_cash_missing() -> None:
    sections = _fixture_sections()
    sections["portfolio"]["cash_eur"] = None
    html = _render_html(sections, "2026-W33")
    assert "Cash, including stablecoins, is" not in html
    # other value-gated sentences in the same box still render
    assert "BTC alone is 41.2% of the portfolio" in html


def test_plain_terms_portfolio_box_insufficient_history_state() -> None:
    sections = _fixture_sections()
    sections["portfolio"]["scorecard"]["depth_weeks"] = 1
    html = _render_html(sections, "2026-W33")
    # autoescape turns the apostrophe into `&#39;` — assert around it, not through it
    assert (
        "There isn&#39;t enough weekly history yet to judge whether active decisions are helping or "
        "hurting returns." in html
    )
    assert "actively managing the portfolio has" not in html


def test_plain_terms_portfolio_box_matches_shadow_four_week_floor() -> None:
    """P2c: the scorecard box now reads the SAME
    `grade_min_weeks` the shadow layer uses (default 4, was 2) before it
    will characterize active management as better/worse than doing
    nothing. depth_weeks=3 used to be "sufficient" (old floor 2); it is not
    anymore (new floor 4)."""
    sections = _fixture_sections()
    sections["portfolio"]["scorecard"]["depth_weeks"] = 3
    html = _render_html(sections, "2026-W33")
    assert "There isn&#39;t enough weekly history yet" in html
    assert "actively managing the portfolio has" not in html


def test_plain_terms_portfolio_box_reads_real_shadow_grade_min_weeks() -> None:
    """The floor is read from `shadow.grade_min_weeks`, not hardcoded — a
    depth of 5 clears the default floor (4) but not a stricter real one (6)."""
    sections = _fixture_sections()
    sections["shadow"]["grade_min_weeks"] = 6
    sections["portfolio"]["scorecard"]["depth_weeks"] = 5
    html = _render_html(sections, "2026-W33")
    assert "There isn&#39;t enough weekly history yet" in html
    assert "actively managing the portfolio has" not in html


def test_plain_terms_market_box_regime_mover_and_cape_sentences() -> None:
    html = _render_html(_fixture_sections(), "2026-W33")
    assert "Most market indicators favour risk-taking this week." in html
    # autoescape turns "&" into "&amp;" — assert around it, not through it
    assert "S&amp;P 500 moved +1.1% this week. That was the largest move across major assets." in html
    assert "US equities are priced richer than ~99% of history on the Shiller measure." in html


def test_plain_terms_market_box_omits_cape_sentence_when_needs_source() -> None:
    sections = _fixture_sections()
    sections["market"]["valuation"]["metrics"][0]["needs_source"] = True
    html = _render_html(sections, "2026-W33")
    assert "on the Shiller measure" not in html
    # other value-gated sentences in the same box still render
    assert "Most market indicators favour risk-taking this week." in html


def test_plain_terms_market_box_risk_off_regime_wording() -> None:
    sections = _fixture_sections()
    sections["market"]["regime"]["state"] = "risk-off"
    html = _render_html(sections, "2026-W33")
    assert "Most market indicators favour caution this week." in html


# ── P1d: widened crypto gate (fires on the REAL stored-pack shape) ───────
def test_is_crypto_widened_gate_direct() -> None:
    """Direct unit coverage of the four branches (P1d): explicit sector,
    missing sector + known ticker + no scores (the real BTC/ETH shape),
    missing sector + UNKNOWN ticker (must not false-positive), and missing
    sector + a real score present (a genuine equity, never miscast)."""
    assert _is_crypto(
        {"fundamentals": {"sector": "Crypto"}, "forensic": {"cards": []}}, "ACME",
    ) is True
    assert _is_crypto(
        {"fundamentals": {"sector": "Technology"}, "forensic": {"cards": []}}, "BTC",
    ) is False  # a real sector on file is authoritative even if the ticker collides
    assert _is_crypto(
        {"fundamentals": {"sector": None}, "forensic": {"cards": [{"key": "piotroski", "score": None}]}}, "BTC",
    ) is True  # the real production shape: no sector, no scores, known ticker
    assert _is_crypto(
        {"fundamentals": {"sector": None}, "forensic": {"cards": [{"key": "piotroski", "score": None}]}}, "ACME",
    ) is False  # missing sector but NOT a known coin — no false positive
    assert _is_crypto(
        {"fundamentals": {"sector": None}, "forensic": {"cards": [{"key": "piotroski", "score": 7.0}]}}, "BTC",
    ) is False  # a real score on file means this is a covered equity, not a coin


def test_plain_terms_holdings_box_crypto_gate_fires_on_real_pack_shape() -> None:
    """P1d: real stored packs render BTC/ETH with `fundamentals.sector`
    missing entirely (yfinance has no sector for a coin) rather than the
    old fixture's explicit `sector: "Crypto"`. The box must still classify
    it as crypto via the known-ticker fallback and print the sentence."""
    sections = _fixture_sections()
    sections["holdings"]["symbols"]["BTC"]["fundamentals"] = {"symbol": "BTC", "sector": None, "currency": "USD"}
    sections["holdings"]["symbols"]["BTC"]["forensic"] = {"cards": [
        {"key": "piotroski", "label": "Piotroski F-Score", "score": None, "verdict": None},
        {"key": "altman", "label": "Altman Z-Score", "score": None, "verdict": None},
        {"key": "beneish", "label": "Beneish M-Score", "score": None, "verdict": None},
    ]}
    html = _render_html(sections, "2026-W33")
    assert "Crypto holdings don&#39;t get forensic accounting scores." in html


def test_plain_terms_holdings_box_crypto_sentence() -> None:
    """BTC (the only holding in the base fixture) is crypto — the box must
    state that forensic scores are expected to be absent, not present a
    strongest/weakest or manipulation-flag sentence with nothing behind it."""
    html = _render_html(_fixture_sections(), "2026-W33")
    holdings_box = _plain_terms_box_divs(html)[3]
    # autoescape turns apostrophes into `&#39;` — assert around them, not through them
    assert (
        "Crypto holdings don&#39;t get forensic accounting scores. Those scores need reported "
        "financial statements, which crypto assets don&#39;t file. That&#39;s expected, not "
        "missing data." in holdings_box
    )
    assert "has the strongest Piotroski score" not in holdings_box
    assert "distress zone" not in holdings_box
    # BTC is the ONLY holding and it's crypto — no real Beneish score exists
    # to gate a manipulation-flag sentence off of, so neither variant prints.
    assert "elevated earnings-manipulation risk on the Beneish measure" not in holdings_box
    assert "No manipulation flags across covered names on the Beneish measure." not in holdings_box


def test_plain_terms_holdings_box_definition_sentences_always_present() -> None:
    html = _render_html(_fixture_sections(), "2026-W33")
    # autoescape turns apostrophes into `&#39;` — assert around them, not through them
    assert "Piotroski&#39;s F-Score (0–9) is a rough read on business quality." in html
    assert "Altman&#39;s Z-Score estimates a company&#39;s distance from bankruptcy." in html
    assert "Beneish&#39;s M-Score flags accounting patterns associated with earnings manipulation." in html


# ── P1c: restricted to tear-sheeted (material) names only ─────────────────
def _add_material_holding(sections: dict, symbol: str, weight_pct: float, cards: list[dict]) -> None:
    """Adds BOTH a `portfolio.positions[]` row (what makes a symbol
    "material") AND a matching `holdings.symbols[]` tear-sheet entry — the
    two ingredients a name needs to legitimately appear in the holdings
    plain-terms box under the P1c rule."""
    sections["portfolio"]["positions"].append({
        "symbol": symbol, "qty": 1.0, "value_eur": 100.0, "weight_pct": weight_pct,
        "wow_weight_delta_pct": None, "wow_value_delta_eur": None,
    })
    sections["holdings"]["symbols"][symbol] = {
        "fundamentals": {"symbol": symbol, "sector": "Technology", "currency": "USD"},
        "valuation": {"symbol": symbol, "price": 100.0, "market_cap": 1_000_000_000.0},
        "forensic": {"cards": cards},
        "technicals": {"trend": "flat", "series": []},
        "scan": {"n_hits": 0}, "news": [],
    }


def test_plain_terms_holdings_box_material_forensic_findings() -> None:
    sections = _fixture_sections()
    _add_material_holding(sections, "AAPL", 5.0, [
        {"key": "piotroski", "label": "Piotroski F-Score", "score": 8.0, "verdict": "Strong"},
        {"key": "altman", "label": "Altman Z-Score", "score": 1.2, "verdict": "Distress"},
        {"key": "beneish", "label": "Beneish M-Score", "score": -1.5, "verdict": "Elevated manipulation risk"},
    ])
    _add_material_holding(sections, "MSFT", 4.0, [
        {"key": "piotroski", "label": "Piotroski F-Score", "score": 3.0, "verdict": "Weak"},
        {"key": "altman", "label": "Altman Z-Score", "score": 4.0, "verdict": "Safe"},
        {"key": "beneish", "label": "Beneish M-Score", "score": -3.0, "verdict": "Low manipulation risk"},
    ])
    html = _render_html(sections, "2026-W33")
    assert "AAPL has the strongest Piotroski score this week (8/9). MSFT has the weakest (3/9)." in html
    assert "AAPL sits in Altman&#39;s distress zone this week." in html
    assert "AAPL flags elevated earnings-manipulation risk on the Beneish measure." in html


def test_plain_terms_holdings_box_single_material_piotroski_name() -> None:
    """A pack where exactly one material, non-crypto,
    tear-sheeted name carries a Piotroski score this week (the others are
    either crypto or below the materiality floor) — the box must still
    name it rather than silently requiring a second name to compare against."""
    sections = _fixture_sections()
    _add_material_holding(sections, "ZZY", 1.5, [
        {"key": "piotroski", "label": "Piotroski F-Score", "score": 6.0, "verdict": "Moderate"},
        {"key": "altman", "label": "Altman Z-Score", "score": None, "verdict": "Incomplete data"},
        {"key": "beneish", "label": "Beneish M-Score", "score": None, "verdict": "Incomplete data"},
    ])
    html = _render_html(sections, "2026-W33")
    assert "ZZY is the only material holding with a Piotroski score this week (6/9)." in html


def test_plain_terms_holdings_box_excludes_non_material_names_even_with_real_scores() -> None:
    """P1c core regression: a symbol with a real, printable forensic score
    but NO position (so no tear-sheet card anywhere in the chapter) must
    never be cited in the box — the reader could never find the figure."""
    sections = _fixture_sections()
    sections["holdings"]["symbols"]["AAPL"] = {
        "fundamentals": {"symbol": "AAPL", "sector": "Technology", "currency": "USD"},
        "valuation": {"symbol": "AAPL", "price": 190.0, "market_cap": 3_000_000_000_000.0},
        "forensic": {"cards": [
            {"key": "piotroski", "label": "Piotroski F-Score", "score": 8.0, "verdict": "Strong"},
        ]},
        "technicals": {"trend": "uptrend", "rsi_14": 55.0, "series": []},
        "scan": {"n_hits": 0}, "news": [],
    }  # deliberately NOT added to portfolio.positions — not material
    html = _render_html(sections, "2026-W33")
    holdings_box = _plain_terms_box_divs(html)[3]
    assert "AAPL" not in holdings_box
    assert "8/9" not in holdings_box


def test_plain_terms_holdings_box_no_manipulation_flags_sentence() -> None:
    sections = _fixture_sections()
    _add_material_holding(sections, "MSFT", 4.0, [
        {"key": "beneish", "label": "Beneish M-Score", "score": -3.0, "verdict": "Low manipulation risk"},
    ])
    html = _render_html(sections, "2026-W33")
    assert "No manipulation flags across covered names on the Beneish measure." in html


def test_plain_terms_quant_box_ic_sentences() -> None:
    html = _render_html(_fixture_sections(), "2026-W33")
    # autoescape turns the apostrophe into `&#39;` — assert around it, not through it
    assert (
        "A factor IC (information coefficient) tests whether ranking names by a signal, such as "
        "momentum." in html
    )
    assert "This week&#39;s momentum IC was +0.06, a weak positive signal in this small universe." in html


def test_plain_terms_quant_box_no_edge_wording_below_threshold() -> None:
    sections = _fixture_sections()
    sections["quant"]["factor"]["ic_mean"] = -0.02
    html = _render_html(sections, "2026-W33")
    assert "This week&#39;s momentum IC was -0.02, effectively no edge in this small universe." in html


def test_plain_terms_quant_box_absent_when_factor_absent() -> None:
    sections = _fixture_sections()
    sections["quant"]["factor"] = {"absent": True, "reason": "insufficient history"}
    html = _render_html(sections, "2026-W33")
    quant_box = html[html.rindex('id="chapter-quant"'):html.index('id="chapter-shadow"')]
    assert "In plain terms" not in quant_box


def test_plain_terms_quant_box_drops_duplicate_within_book_caveat() -> None:
    """P2b: the existing how-to-read caption above
    the factor summary already states the small-universe/within-book
    caveat ("... a book this size is a within-book ordering ..."); the box
    used to ALSO state it, a third repetition on one page. The chapter's own
    `how_to_read` field text and the static factor caption both already say
    "within-book" independent of this feature (real, pre-existing sections
    data — out of scope here) — the box itself is the one place this
    feature controls, so that is what gets checked directly."""
    html = _render_html(_fixture_sections(), "2026-W33")
    quant_box = _plain_terms_box_divs(html)[4]
    assert "within-book" not in quant_box.lower()


def test_plain_terms_shadow_box_status_sentence_with_a_real_hit_rate() -> None:
    """Base fixture: `technicals` is graded AND has a real hit_rate (0.6) —
    the box must report the real figure, not a flat tool count."""
    html = _render_html(_fixture_sections(), "2026-W33")
    shadow_box = _plain_terms_box_divs(html)[5]
    assert _shadow_definition_sentence() in shadow_box
    assert "technicals 60% had a real hit rate this week." in shadow_box
    assert "tool graded this week" not in shadow_box


def test_plain_terms_shadow_box_status_sentence_matches_scoreboard_when_ungraded() -> None:
    """P1b: the box previously said "N tools graded
    this week" while the scoreboard directly above it showed N graded 0 /
    "ungraded" for those same tools — a real contradiction on one page. When
    every graded tool still has no real hit rate, the box must say so
    explicitly instead."""
    sections = _fixture_sections()
    sections["shadow"]["tools"] = {
        "factor": {"n_events": 4, "n_graded": 0, "graded": True, "hit_rate": None},
        "signals": {"n_events": 2, "n_graded": 0, "graded": True, "hit_rate": None},
    }
    html = _render_html(sections, "2026-W33")
    assert "2 tools were graded this attempt. None had enough events yet for a real hit rate." in html
    assert "2 tools graded this week." not in html


def test_plain_terms_shadow_box_honest_state_when_all_graded_low_confidence() -> None:
    sections = _fixture_sections()
    sections["shadow"]["tools"]["technicals"]["low_confidence"] = True
    html = _render_html(sections, "2026-W33")
    assert (
        "None of the tools has enough graded history yet for a track record. Treat all tool "
        "opinions as unproven." in html
    )


def test_plain_terms_shadow_box_omits_honest_state_when_a_tool_has_confidence() -> None:
    """Base fixture's one graded tool (`technicals`) is NOT low-confidence —
    the blanket "no track record yet" claim would be false, so it must not
    print (never invent a stronger claim than the data actually supports)."""
    html = _render_html(_fixture_sections(), "2026-W33")
    assert "None of the tools has enough graded history yet for a track record" not in html


def test_plain_terms_methodology_chapter_has_no_box() -> None:
    html = _render_html(_fixture_sections(), "2026-W33")
    methodology_box = html[html.rindex('id="chapter-methodology"'):]
    assert "In plain terms" not in methodology_box


def test_var95_sentence_identical_in_exec_and_portfolio_boxes() -> None:
    """`_var95_sentence` is shared verbatim between the two boxes (rule 3's
    single-source-of-truth requirement) — the sentence must appear twice."""
    html = _render_html(_fixture_sections(), "2026-W33")
    assert html.count("On 19 trading days out of 20, a one-day loss should stay under") == 2


def test_render_html_quant_factor_warnings_rendered_verbatim() -> None:
    html = _render_html(_fixture_sections(), "2026-W33")
    assert "factor exposures are z-scored on the latest decision date only" in html


def test_render_html_quant_scoreboard_warnings_rendered_verbatim() -> None:
    """SEV1: scoreboard.warnings (e.g. "today's holdings applied to past decision
    dates ... not research-grade edge") must appear next to the significance
    line, not be silently dropped."""
    html = _render_html(_fixture_sections(), "2026-W33")
    # autoescape turns the apostrophe into `&#39;` — assert around it, not through it
    assert "holdings applied to past decision dates" in html
    assert "not research-grade edge" in html


def test_render_html_quant_scoreboard_warnings_rendered_even_with_zero_graded_rows() -> None:
    """Regression (caught by the container PDF proof this round): the warnings
    render was originally nested INSIDE the `if scoreboard.rows` block, so a
    week with zero graded rows silently dropped the caveat too — a
    methodology warning is still true even when nothing graded this week."""
    sections = _fixture_sections()
    sections["quant"]["scoreboard"]["rows"] = []
    html = _render_html(sections, "2026-W33")
    assert "Signal scoreboard" not in html  # no table when there are no rows
    assert "not research-grade edge" in html  # but the warning still shows


def test_render_html_shadow_table_includes_n_graded_denominator() -> None:
    """SEV1: n_events alone invites reading it as the hit-rate's denominator —
    n_graded (the real denominator) must be visible in the table."""
    html = _render_html(_fixture_sections(), "2026-W33")
    assert "N graded" in html  # column header present


def test_render_html_shadow_priced_as_of_caption_rendered_trimmed() -> None:
    html = _render_html(_fixture_sections(), "2026-W33")
    assert "priced as of 2026-08-16 14:32:07" in html


def test_render_html_shadow_effective_n_and_pushes_are_scoreboard_columns() -> None:
    """P1.8: effective_n/pushes moved from a per-tool prose
    line into scoreboard TABLE columns — no more "effective N: 2. pushes:
    1." sentence at all."""
    html = _render_html(_fixture_sections(), "2026-W33")
    assert "<th>Effective N</th>" in html
    assert "<th>Pushes</th>" in html
    assert "effective N:" not in html
    assert "pushes:" not in html
    scoreboard_start = html.index("SCOREBOARD")
    scoreboard = html[scoreboard_start:html.index("</table>", scoreboard_start)]
    assert "<td>2</td>" in scoreboard  # technicals' effective_n
    assert "<td>1</td>" in scoreboard  # technicals' pushes


def test_render_html_shadow_low_confidence_and_warnings_visible_under_table() -> None:
    sections = _fixture_sections()
    sections["shadow"]["tools"]["technicals"]["low_confidence"] = True
    sections["shadow"]["tools"]["technicals"]["warnings"] = [
        "overlapping grading windows (horizon > event spacing) — indicative only",
    ]
    html = _render_html(sections, "2026-W33")
    assert "low confidence" in html.lower()
    assert "overlapping grading windows" in html
    # visible body text, not a footnote glyph/title attribute
    assert 'title="overlapping grading windows' not in html


def test_render_html_shadow_omits_optional_effective_n_pushes_when_absent() -> None:
    """`forensic` tool carries neither field — must not print "effective N:
    None" or similar garbage."""
    html = _render_html(_fixture_sections(), "2026-W33")
    assert "effective N: None" not in html
    assert "pushes: None" not in html


# ── PROOF: sections dict built by calling the REAL producers ─────────────
# Mirrors test_report_pack.py's own fixture style (module-level engine
# monkeypatches) — a fixture that drifts from the producers is exactly how
# fix rounds 1/2's invented contract happened.
class _MD:
    def __init__(self, d: dict[str, Any]) -> None:
        self._d = d

    def model_dump(self) -> dict[str, Any]:
        return self._d


class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self) -> "_FakeResult":
        return self

    def all(self) -> list:
        return self._rows


class _FakeDB:
    def __init__(self, results: list) -> None:
        self._results = list(results)

    async def execute(self, stmt: object) -> _FakeResult:
        return self._results.pop(0)


def _wire_session_scope_sequence(monkeypatch, module: object, results_sequence: list[list]) -> None:
    """One entry per `session_scope()` CALL (not per query) — chapter_portfolio
    makes one call (2 queries); chapter_holdings' `_forensic_deltas` makes one
    call PER symbol (1 query each), in symbol-sorted order."""
    calls = {"n": 0}

    @contextlib.asynccontextmanager
    async def _fake():
        results = results_sequence[calls["n"]]
        calls["n"] += 1
        yield _FakeDB(results)

    monkeypatch.setattr(module, "session_scope", _fake)


class _FakeSnapRow:
    def __init__(self, report_week: str, symbol: str, qty: float, value_eur: float, nav: float, cash: float) -> None:
        self.report_week, self.symbol = report_week, symbol
        self.qty, self.value_eur, self.nav, self.cash = qty, value_eur, nav, cash


class _FakeAsset:
    def __init__(self, symbol: str) -> None:
        self.symbol = symbol


class _FakeHoldings:
    def __init__(self, symbols: list[str]) -> None:
        self.by_asset = [_FakeAsset(s) for s in symbols]


class _FakeWF:
    def __init__(self, symbols: list[str]) -> None:
        self._symbols = symbols

    async def holdings(self) -> _FakeHoldings:
        return _FakeHoldings(self._symbols)


class _FakeRiskEngine:
    """Real `services/risk.py` leaf names — `coverage` NESTED inside `var`
    (production's actual shape; there is no top-level `bundle["coverage"]`
    upstream, only `bundle["var"]["coverage"]`/`bundle["xray"]["coverage"]`/
    `bundle["fx_drag"]["coverage"]`, all the SAME dict)."""

    def __init__(self, redis: object, settings: object) -> None:
        pass

    async def bundle(self) -> dict[str, Any]:
        return {
            "var": {
                "var_eur": -1200.0, "var_pct": -2.47, "cvar_eur": -1800.0, "cvar_pct": -3.71,
                "var99_eur": -2400.0, "cvar99_eur": -3000.0, "fat_tail_ratio": 1.5,
                "worst_day": "2026-08-05", "worst_day_pct": -4.2, "confidence": 0.95,
                "low_confidence": False, "n_scenarios": 340,
                "coverage": {
                    "n_assets_total": 2, "n_covered": 2, "covered_weight_pct": 100.0,
                    "n_observations": 335, "window_start": "2025-03-31",
                    "assumption": "Daily EUR P&L reconstructed from per-asset price series.",
                },
            },
        }


class _FakeMarketResearchEngine:
    def __init__(self, redis: object, settings: object) -> None:
        pass

    async def bundle(self) -> dict[str, Any]:
        return {
            "regime": {"state": "risk-on", "score": 0.42}, "cross_asset": {}, "rotation": {},
            "rrg": {}, "volatility": {}, "breadth": {}, "sentiment": {}, "valuation": {},
        }


class _FakeCalendarEnvelope:
    def model_dump(self, mode: str | None = None) -> dict[str, Any]:
        return {"data": {"events": []}, "source": "fred", "stale": False}


class _FakeMacroAdapter:
    def __init__(self, redis: object, settings: object) -> None:
        pass

    async def calendar(self, days: int = 14) -> _FakeCalendarEnvelope:
        return _FakeCalendarEnvelope()


_TECH_SERIES = [
    {"date": "2026-08-10", "close": 41000.0, "sma50": 40000.0, "sma200": 38000.0},
    {"date": "2026-08-11", "close": 42500.0, "sma50": 40200.0, "sma200": 38100.0},
    {"date": "2026-08-12", "close": 44000.0, "sma50": 40500.0, "sma200": 38200.0},
]


class _FakeResearchEngine:
    def __init__(self, redis: object, settings: object) -> None:
        pass

    async def fundamentals(self, symbol: str, *, asset_class: str | None = None):
        return _MD({"symbol": symbol, "sector": "Crypto", "currency": "USD"}), {}

    async def valuation(self, symbol: str, *, asset_class: str | None = None):
        return _MD({"symbol": symbol, "price": 44000.0, "market_cap": 900_000_000_000.0}), {}

    async def scores(self, symbol: str, *, asset_class: str | None = None):
        return _MD({
            "symbol": symbol, "name": symbol, "fiscal_years": ["2025"],
            "cards": [{
                "key": "piotroski", "label": "Piotroski F-Score", "score": 7.0,
                "scale": "0-9", "verdict": "Strong", "components": [],
            }],
            "warnings": [],
        }), {}

    async def technicals(self, symbol: str, *, asset_class: str | None = None):
        return _MD({
            "symbol": symbol, "last": 44000.0, "sma50": 40500.0, "sma200": 38200.0,
            "rsi_14": 61.4, "trend": "uptrend", "series": _TECH_SERIES,
        }), {}


class _FakeScannerEngine:
    def __init__(self, redis: object, settings: object) -> None:
        pass

    async def scan(self, patterns: object, universe: list[str], emit: bool = False):
        return _MD({"n_hits": 1, "n_symbols": len(universe), "rows": []})


class _FakeIntelService:
    def __init__(self, redis: object, settings: object) -> None:
        pass

    async def news(self, limit: int = 60) -> list[dict[str, Any]]:
        return [{"title": "BTC rallies on ETF inflows", "summary": "", "topics": []}]


class _FakeCatalogEntry:
    def __init__(self, signal_id: str, last_date: str | None) -> None:
        self.signal_id, self.last_date = signal_id, last_date


class _FakeCatalog:
    def __init__(self, entries: list) -> None:
        self.signals = entries


class _FakeCrossSectionRow:
    def __init__(self, symbol: str, value: float) -> None:
        self.symbol, self.value = symbol, value

    def model_dump(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "value": self.value}


class _FakeCrossSection:
    def __init__(self, as_of_date: str, rows: list) -> None:
        self.as_of_date, self.rows = as_of_date, rows


class _FakeSignalEngine:
    def __init__(self, redis: object, settings: object) -> None:
        pass

    async def scoreboard(self):
        return _MD({
            "as_of": "2026-08-14", "horizon_bars": 5, "quantiles": 5,
            "rows": [{
                "signal_id": "mom_63d", "horizon_bars": 5, "methodology": "cross-sectional",
                "n_dates_graded": 40, "mean_rank_ic": 0.081, "icir": 0.4, "t_stat": 2.1,
                "significant": True, "low_confidence": False, "hit_rate": 0.58,
                "hit_rate_wilson_lb": 0.44, "n_pairs_matured": 40, "n_pairs_pending": 2,
            }],
            "warnings": [],
        })

    async def catalog(self):
        return _FakeCatalog([_FakeCatalogEntry("mom_63d", "2026-08-14")])

    async def cross_section(self, signal_id: str, as_of: str):
        return _FakeCrossSection(as_of, [_FakeCrossSectionRow("BTC", 1.2)])


_FACTOR_PAYLOAD = {
    "name": "report-pack-momentum", "factor": "momentum", "horizon_days": 21,
    "window_start": "2026-05-01", "window_end": "2026-08-10",
    "n_periods": 60, "n_symbols": 6, "n_obs": 340,
    "ic_mean": 0.061, "ic_std": 0.21, "ic_ir": 0.29, "ic_tstat": 2.3, "ic_hit_rate": 0.58,
    "ic_series": [
        {"date": "2026-08-08", "ic": 0.08}, {"date": "2026-08-09", "ic": -0.02}, {"date": "2026-08-10", "ic": 0.11},
    ],
    "quantile_returns": [
        {"quantile": 1, "mean_fwd_ret_pct": -1.2, "n_avg": 1.0},
        {"quantile": 5, "mean_fwd_ret_pct": 2.4, "n_avg": 1.0},
    ],
    "long_short_spread": 0.036,
    "warnings": [],
}


def test_render_html_populates_real_values_from_actual_producer_shapes(monkeypatch) -> None:
    """Builds `sections` by calling the REAL `ReportPackEngine.build_chapters`
    (portfolio/market/holdings/quant/methodology) over monkeypatched engine
    fakes, plus a hand-built exec_summary/shadow in their real documented
    shape (assembled outside `ReportPackEngine`). Asserts every chapter and
    all chart-data families show REAL populated values, not em-dashes."""
    weeks = ["2026-W33", "2026-W32"]
    snap_rows = [
        _FakeSnapRow("2026-W32", "BTC", 0.4, 16000.0, 40000.0, 5000.0),
        _FakeSnapRow("2026-W33", "BTC", 0.5, 20000.0, 48512.30, 5000.0),
        _FakeSnapRow("2026-W33", "ETH", 4.0, 9000.0, 48512.30, 5000.0),
    ]
    _wire_session_scope_sequence(monkeypatch, report_pack_module, [
        [_FakeResult(weeks), _FakeResult(snap_rows)],  # chapter_portfolio
        [_FakeResult([])],  # chapter_holdings forensic delta: BTC
        [_FakeResult([])],  # chapter_holdings forensic delta: ETH
    ])
    monkeypatch.setattr(report_pack_module, "RiskEngine", _FakeRiskEngine)
    monkeypatch.setattr(report_pack_module, "MarketResearchEngine", _FakeMarketResearchEngine)
    monkeypatch.setattr(report_pack_module, "MacroAdapter", _FakeMacroAdapter)
    monkeypatch.setattr(report_pack_module, "make_wealthfolio", lambda redis, settings: _FakeWF(["BTC", "ETH"]))
    monkeypatch.setattr(report_pack_module, "ResearchEngine", _FakeResearchEngine)
    monkeypatch.setattr(report_pack_module, "ScannerEngine", _FakeScannerEngine)
    monkeypatch.setattr(report_pack_module, "IntelService", _FakeIntelService)
    monkeypatch.setattr(report_pack_module, "SignalEngine", _FakeSignalEngine)

    settings = Settings(postgres_user="u", postgres_password="p", postgres_db="d", redis_password="r", demo_mode=False)
    engine = ReportPackEngine(object(), settings)
    sections, _sources = asyncio.run(engine.build_chapters("2026-W33", factor=_FACTOR_PAYLOAD))

    # exec_summary/shadow are assembled OUTSIDE ReportPackEngine —
    # hand-built here in their real documented shape, per the controller's
    # explicit allowance ("plus a hand-built exec_summary/shadow").
    sections["exec_summary"] = {
        "engine": "report-worker-exec-summary-v1", "how_to_read": "roll-up",
        "nav_eur": sections["portfolio"]["nav_eur"], "wow_pct": 1.42,
        "regime": sections["market"]["regime"],
        "chapters_present": {k: True for k in sections}, "n_chapters_present": len(sections),
        "shadow_counts": {}, "headline": "NAV up on the week.", "highlights": ["portfolio: present"],
        "narration": "NAV rose on the week, led by BTC.", "narration_absent_reason": None,
    }
    sections["shadow"] = {
        "engine": "shadow-verdict-engine-v1", "how_to_read": "evidence-only",
        "report_week": "2026-W33", "nature": "descriptive, evidence-only, never a proposal",
        "grade_min_weeks": 4, "k": 1, "deflation_note": None,
        "priced_as_of": "2026-08-16T14:32:07+00:00",
        "tools": {
            "technicals": {
                "n_events": 3, "n_graded": 3, "hit_rate": 0.6, "benchmark_hit_rate": 0.5,
                "hit_rate_wilson_lb": 0.31, "graded": True,
            },
            "forensic": {"n_events": 1, "graded": False, "reason": "horizon exceeds grading window"},
        },
    }

    html = _render_html(sections, "2026-W33")

    assert "Render Warnings" not in html  # every field well-shaped -> no degradation
    assert "{'" not in html  # no raw dict repr anywhere, including market's bare fixture shape

    # cover / exec_summary
    assert "€48,512.30" in html
    assert "risk-on" in html
    assert "NAV rose on the week, led by BTC." in html  # narration (fix round 4)

    # portfolio: real positions, both symbols, real weight
    assert "BTC" in html and "ETH" in html
    assert "41.2%" in html

    # risk: real coverage sentence, not a dict dump
    assert "Covers 2 of 2 assets" in html

    # holdings: real per-symbol fields populated, not dashes
    assert "Crypto" in html  # fundamentals.sector
    assert "uptrend" in html  # technicals.trend
    assert "Piotroski" in html  # forensic card label
    assert "BTC rallies on ETF inflows" in html  # news

    # quant: real factor stats + real scoreboard row
    assert "momentum" in html
    assert "mom_63d" in html

    # shadow: real per-tool row
    assert "technicals" in html
    assert "60.0%" in html  # hit_rate formatted

    # methodology: real field names (tool/measures), not the old name/what_it_measures
    assert "risk.RiskEngine" in html

    # chart data: all chart families bound to real fields, present in the payload
    assert "allocation_donut" in html
    assert "nav_line" in html
    assert "factor_ic_bars" in html
    assert "holding_spark_btc" in html


# ── forecasts + calibration chapters ────────────────────────
# Real shapes from `report_worker.py::_issue_forecasts` / `shadow.py::calibration`
# (see report_pdf.py's module docstring, "Phase 18" paragraph) — not
# invented here, same discipline fix round 3 established for every other
# chapter's fixture.
def _fixture_forecasts() -> dict:
    return {
        "engine": "forecast-ledger-v1",
        "how_to_read": "One binary forecast per gradeable shadow-tool call, issued rolling and non-overlapping.",
        "issued": 5, "duplicates": 2,
        "skipped": {"covered": 1, "ungradeable": 3},
        "issuance_errors": [
            {"issued_by": "shadow:technicals", "subject": "BTC", "reason": "probability 1.2 out of [0, 1]"},
        ],
        "pending_total": 9, "next_due": "2026-09-14",
    }


def _fixture_calibration() -> dict:
    return {
        "engine": "calibration-v1",
        "how_to_read": "Each row grades one issuer's forecasts at one horizon via the Brier score.",
        "groups": [
            {
                "issued_by": "human", "horizon_days": 28,
                "summary": {
                    "n": 12, "effective_n": 12, "brier_mean": 0.18, "brier_ref": 0.24,
                    "brier_coin": 0.25, "benchmark_brier": 0.22, "brier_skill": 0.25,
                    "log_score_mean": -0.5, "hit_rate": 0.6, "benchmark_hit_rate": 0.5,
                    "wilson_lb": 0.35, "pushes": 1, "abstained": 0, "abstention_rate": 0.0,
                },
                "bins": [
                    {"bin": "0.0–0.2", "lo": 0.0, "hi": 0.2, "n": 0, "mean_p": None, "hit_rate": None, "wilson_lb": None},
                    {"bin": "0.2–0.4", "lo": 0.2, "hi": 0.4, "n": 4, "mean_p": 0.3, "hit_rate": 0.5, "wilson_lb": 0.2},
                    {"bin": "0.4–0.6", "lo": 0.4, "hi": 0.6, "n": 8, "mean_p": 0.5, "hit_rate": 0.625, "wilson_lb": 0.3},
                ],
                "bins_note": None, "pending": 2, "priced_as_of": "2026-08-30T00:00:00+00:00",
                "low_confidence": False, "deflation_note": None,
            },
            {
                "issued_by": "shadow:technicals", "horizon_days": 28,
                "summary": {
                    "n": 0, "effective_n": 0, "brier_mean": None, "brier_ref": None,
                    "brier_coin": 0.25, "benchmark_brier": None, "brier_skill": None,
                    "log_score_mean": None, "hit_rate": None, "benchmark_hit_rate": None,
                    "wilson_lb": None, "pushes": 0, "abstained": 0, "abstention_rate": None,
                },
                "bins": None,
                "bins_note": (
                    "issuer probability is a single track-record prior (constant per week) — "
                    "reliability bins would be tautological; read discrimination (hit_rate vs "
                    "benchmark_hit_rate) and brier_skill instead"
                ),
                "pending": 3, "priced_as_of": "2026-08-30T00:00:00+00:00",
                "low_confidence": True, "deflation_note": None,
            },
        ],
    }


def test_forecasts_and_calibration_chapters_render_with_real_shapes() -> None:
    sections = _fixture_sections()
    sections["forecasts"] = _fixture_forecasts()
    sections["calibration"] = _fixture_calibration()
    html = _render_html(sections, "2026-W33")

    assert "Forecasts Issued" in html
    assert "Calibration" in html
    assert "Brier" in html
    assert "Render Warnings" not in html

    # forecasts chapter body
    assert "already covered: 1" in html
    assert "probability 1.2 out of [0, 1]" in html
    assert "2026-09-14" in html  # next_due

    # calibration: both issuer groups present, headings stripped/title-cased
    # — "human" -> "Human", "shadow:technicals" -> "Technicals".
    # ("shadow:technicals" still appears verbatim once, from the forecasts
    # chapter's issuance-errors line above, which is untouched by this fix.)
    assert "Human" in html
    assert "Technicals" in html
    # human group gets a real reliability table, including the honestly-listed empty bin
    assert "0.0–0.2" in html
    assert 'class="bin-empty"' in html
    # machine group shows the B2 discrimination-only note instead of a table
    assert "reliability bins would be tautological" in html
    # "In plain terms" box sentences
    assert "own realised base rate, not a coin flip" in html
    assert "Machine-issued forecasts carry a near-constant probability" in html


def test_new_chapters_omitted_entirely_when_key_missing() -> None:
    """The base W33-style fixture never carries `forecasts`/`calibration`
    keys (mirrors an older pack, or a calibration chapter-build
    failure, which drops the key entirely rather than writing an absent
    stand-in). Because every `.chapter` forces a page break, a heading-only
    stub would silently cost a page on every such pack — so a wholly
    missing key renders NEITHER a TOC entry nor a chapter section at all
    (page count unchanged), unlike the seven core chapters' `absent`-flag
    branch. Must not crash."""
    html = _render_html(_fixture_sections(), "2026-W33")
    assert "Forecasts Issued" not in html
    assert "Calibration" not in html
    assert "No issuer groups to grade yet." not in html


def test_new_chapters_omitted_keeps_chapter_section_count_unchanged() -> None:
    """Fix-first #5: the substring asserts above prove no
    text leaks in, but a stub `<section class="chapter">` with just a
    heading would still cost a real page without tripping any of them —
    every `.chapter` forces a page break (see `report_pack.html`). Count the
    rendered chapter-section opens directly (a page-count proxy independent
    of the CSS pagination the design review verified against the real
    Playwright render) so a keyless pack renders exactly the seven core
    chapters, never nine."""
    html = _render_html(_fixture_sections(), "2026-W33")
    expected = sum(1 for key, _title in _CHAPTER_ORDER if key not in ("forecasts", "calibration"))
    assert html.count('<section class="chapter" id="chapter-') == expected


def test_new_chapters_render_with_data_present() -> None:
    """Once the key IS present (even with nothing to show, e.g. a
    just-rolled-out empty calibration groups list), the chapter renders
    normally, heading and all."""
    sections = _fixture_sections()
    sections["calibration"] = {"engine": "calibration-v1", "how_to_read": "…", "groups": []}
    html = _render_html(sections, "2026-W33")
    assert "Calibration" in html
    assert "No issuer groups to grade yet." in html
    assert "Forecasts Issued" not in html  # still absent — key never set


def test_forecasts_chapter_absent_wrapped_form_renders_reason() -> None:
    sections = _fixture_sections()
    sections["forecasts"] = {
        "engine": "forecast-ledger-v1", "absent": True, "reason": "ledger issuance failed: boom",
    }
    html = _render_html(sections, "2026-W33")
    assert "ledger issuance failed: boom" in html


def test_normalize_sections_calibration_wrong_shapes_drop_to_missing() -> None:
    sections = _fixture_sections()
    sections["calibration"] = {
        "engine": "calibration-v1", "how_to_read": "…",
        "groups": [
            {"issued_by": "human", "horizon_days": 28, "summary": "not-a-dict", "bins": "not-a-list", "pending": 1},
            "not-a-dict-group",
        ],
    }
    html = _render_html(sections, "2026-W33")  # must not raise
    assert "Render Warnings" in html
    assert "calibration.groups" in html


def test_normalize_sections_forecasts_wrong_shapes_drop_to_missing() -> None:
    sections = _fixture_sections()
    sections["forecasts"] = {
        "engine": "forecast-ledger-v1", "how_to_read": "…",
        "issued": 5, "duplicates": 2, "skipped": "not-a-dict",
        "issuance_errors": {"not": "a-list"}, "pending_total": 9, "next_due": 123,
    }
    html = _render_html(sections, "2026-W33")  # must not raise
    assert "Render Warnings" in html


def test_calibration_all_zero_n_groups_render_without_exception() -> None:
    """Acceptance line: with an empty `outcomes` table the chapter must
    still render every issuer group that has pending forecasts, with
    `summary.n == 0`, `bins is None`, `pending > 0`, and no exception."""
    sections = _fixture_sections()
    calibration = _fixture_calibration()
    calibration["groups"] = [calibration["groups"][1]]  # the all-zero-n machine group only
    sections["calibration"] = calibration
    html = _render_html(sections, "2026-W33")
    # the group heading now shows the stripped/title-cased issuer
    # label ("Technicals"), never the raw "shadow:technicals" key.
    assert "Technicals" in html
    assert "shadow:technicals" not in html
    assert "3 forecast(s) still pending resolution." in html
    assert "reliability bins would be tautological" in html


def test_calibration_zero_n_group_drops_constant_and_implied_stat_rows() -> None:
    """F6a (fix round): `summary.n == 0` used to still render `Effective N`,
    `Brier (0.25 coin, reference only)`, `Pushes`, `Abstained` as bare "0"/
    "0.25" rows — none of it derived from data. Only `N (decided)` survives
    when those ancillary counts are ALSO exactly 0."""
    group = _fixture_calibration()["groups"][1]  # the n==0 shadow:technicals group
    assert group["summary"]["pushes"] == 0 and group["summary"]["abstained"] == 0
    view = _calibration_groups_view([group])
    labels = [row["label"] for row in view[0]["stat_rows"]]
    assert labels == ["N (decided)"]
    # `stats_omitted` names the omitted labels (the footnote must
    # name them), not just carry a bare count.
    assert len(view[0]["stats_omitted"]) == len(_CALIBRATION_STAT_LABELS) - 1
    assert "Effective N" in view[0]["stats_omitted"]
    assert "Brier (0.25 coin, reference only)" in view[0]["stats_omitted"]


def test_calibration_zero_n_group_keeps_nonzero_pushes_or_abstained() -> None:
    """F6a: a group can have `n == 0` decided cases while still carrying a
    REAL (non-zero) pushes/abstained count — that is not "no value yet" and
    must not be dropped alongside the constant/implied rows."""
    group = _fixture_calibration()["groups"][1]
    group = {**group, "summary": {**group["summary"], "abstained": 2, "abstention_rate": 1.0}}
    view = _calibration_groups_view([group])
    labels = [row["label"] for row in view[0]["stat_rows"]]
    assert "N (decided)" in labels
    assert "Abstained" in labels
    assert "Abstention rate" in labels
    assert "Effective N" not in labels  # still dropped: always implied by n==0
    assert "Brier (0.25 coin, reference only)" not in labels


# ── fix 5 (deferred design review PDF list) ──────────────────────────
def test_display_issuer_strips_shadow_prefix_and_titlecases() -> None:
    assert _display_issuer("shadow:technicals") == "Technicals"
    assert _display_issuer("shadow:signals") == "Signals"
    assert _display_issuer("human") == "Human"
    assert _display_issuer("") == "—"
    assert _display_issuer(None) == "—"


def test_calibration_groups_view_carries_issuer_label_and_priced_as_of() -> None:
    group = _fixture_calibration()["groups"][0]
    assert group["priced_as_of"] == "2026-08-30T00:00:00+00:00"
    view = _calibration_groups_view([group])
    assert view[0]["issuer_label"] == "Human"
    assert view[0]["priced_as_of"] == "2026-08-30T00:00:00+00:00"


def test_render_html_calibration_priced_as_of_rendered_trimmed() -> None:
    sections = _fixture_sections()
    sections["calibration"] = _fixture_calibration()
    html = _render_html(sections, "2026-W33")
    assert "priced as of 2026-08-30 00:00:00" in html


def test_calibration_bin_empty_marker_never_lighter_than_its_row_css() -> None:
    """fix 4: `.micro` inside an empty-bin row must
    inherit the row's own (darker) color, never render fainter than it —
    the static CSS rule ships in every render's embedded `<style>` block."""
    html = _render_html(_fixture_sections(), "2026-W33")
    assert "tr.bin-empty .micro { color: inherit; }" in html


def test_plain_terms_forecasts_value_gated_and_empty_when_absent() -> None:
    assert _plain_terms_forecasts({}) == []
    assert _plain_terms_forecasts({"absent": True, "reason": "boom"}) == []
    lines = _plain_terms_forecasts(_fixture_forecasts())
    assert any("5 new forecast(s)" in line for line in lines)
    assert any("9 forecast(s)" in line for line in lines)
    assert any("2 candidate forecast(s)" in line for line in lines)
    assert any("2026-09-14" in line for line in lines)


def test_render_html_forecasts_plain_terms_box_present() -> None:
    sections = _fixture_sections()
    sections["forecasts"] = _fixture_forecasts()
    html = _render_html(sections, "2026-W33")
    assert "new forecast(s) were issued this week" in html


def test_calibration_bin_with_n_none_is_not_marked_n_equals_zero() -> None:
    """F6b (fix round): `empty = (n_raw or 0) == 0` used to also fire on a
    malformed/missing `n` (`None`) — mislabelling an UNKNOWN count as a
    KNOWN-zero one. `_reliability_bins` itself never emits `n=None` (always
    a real int); this only guards a malformed bin dict."""
    group = {
        **_fixture_calibration()["groups"][0],
        "bins": [{"bin": "0.0–0.2", "lo": 0.0, "hi": 0.2, "n": None, "mean_p": None,
                   "hit_rate": None, "wilson_lb": None}],
    }
    view = _calibration_groups_view([group])
    assert view[0]["bins"][0]["empty"] is False

    zero_group = {
        **_fixture_calibration()["groups"][0],
        "bins": [{"bin": "0.0–0.2", "lo": 0.0, "hi": 0.2, "n": 0, "mean_p": None,
                   "hit_rate": None, "wilson_lb": None}],
    }
    view2 = _calibration_groups_view([zero_group])
    assert view2[0]["bins"][0]["empty"] is True
