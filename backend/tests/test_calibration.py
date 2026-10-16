"""Calibration chapter — pure helpers over the forecast
ledger's already-priced rows (`_reliability_bins`, `_calibration_summary`,
`_bin_index`) plus `ShadowEngine.calibration()`/`chapter_calibration()`,
exercised via the monkeypatched `_load_calibration_rows` seam (matches
`test_shadow.py`'s `_load_history`/`_fetch_return` convention — no DB/Redis).
"""
from __future__ import annotations

from datetime import timedelta

import pytest

from app.services.shadow import (
    ShadowEngine,
    _bin_index,
    _calibration_summary,
    _reliability_bins,
    _week_start,
)


class _FakeSettings:
    report_pack_calibration_bins = 5
    forecast_probability_min_n = 8
    report_pack_calibration_window_weeks = 104


# ── _bin_index: half-open bins except the closed last one ─────────────────
def test_bin_index_boundaries_are_half_open_except_last():
    assert _bin_index(0.0, 5) == 0
    assert _bin_index(0.2, 5) == 1
    assert _bin_index(0.4, 5) == 2
    assert _bin_index(0.6, 5) == 3
    assert _bin_index(0.8, 5) == 4
    assert _bin_index(1.0, 5) == 4  # m13 — the last bin is closed [0.8, 1.0]


# ── _reliability_bins: hand-computed n/mean_p/hit_rate, empty bins kept ────
def test_reliability_bins_hand_computed_with_empty_bins_and_closed_last_bin():
    observations = (
        [{"p": 0.1, "hit": True}] * 3 + [{"p": 0.1, "hit": False}]
        + [{"p": 0.5, "hit": True}] * 2 + [{"p": 0.5, "hit": False}] * 2
        + [{"p": 0.9, "hit": True}] * 3 + [{"p": 1.0, "hit": False}]
    )
    bins = _reliability_bins(observations, 5)
    assert len(bins) == 5
    assert bins[0]["n"] == 4
    assert bins[0]["mean_p"] == pytest.approx(0.1)
    assert bins[0]["hit_rate"] == pytest.approx(0.75)
    assert bins[1]["n"] == 0  # empty bin, listed not dropped
    assert bins[1]["hit_rate"] is None
    assert bins[2]["n"] == 4
    assert bins[2]["mean_p"] == pytest.approx(0.5)
    assert bins[2]["hit_rate"] == pytest.approx(0.5)
    assert bins[3]["n"] == 0  # second empty bin
    assert bins[4]["n"] == 4
    assert bins[4]["mean_p"] == pytest.approx(0.925)  # (0.9*3 + 1.0) / 4
    assert bins[4]["hit_rate"] == pytest.approx(0.75)
    assert bins[4]["lo"] == pytest.approx(0.8)
    assert bins[4]["hi"] == pytest.approx(1.0)  # closed last bin holds p=1.0


# ── _calibration_summary: Brier/reference/skill table tests ───────────────
def test_calibration_summary_all_p_half_brier_mean_and_ref_are_exactly_quarter():
    observations = (
        [{"p": 0.5, "hit": True, "benchmark_hit": True}] * 5
        + [{"p": 0.5, "hit": False, "benchmark_hit": False}] * 5
    )
    summary = _calibration_summary(observations, cross_sectional=False)
    assert summary["n"] == 10
    assert summary["hit_rate"] == pytest.approx(0.5)
    assert summary["brier_mean"] == pytest.approx(0.25)
    assert summary["brier_ref"] == pytest.approx(0.25)
    assert summary["brier_skill"] == pytest.approx(0.0)
    assert summary["brier_coin"] == 0.25


def test_calibration_summary_base_rate_0p8_brier_ref():
    observations = (
        [{"p": 0.8, "hit": True, "benchmark_hit": True}] * 8
        + [{"p": 0.8, "hit": False, "benchmark_hit": False}] * 2
    )
    summary = _calibration_summary(observations, cross_sectional=False)
    assert summary["hit_rate"] == pytest.approx(0.8)
    assert summary["brier_ref"] == pytest.approx(0.16)


def test_calibration_summary_cross_sectional_effective_n_is_halved():
    observations = (
        [{"p": 0.6, "hit": True, "benchmark_hit": True}] * 4
        + [{"p": 0.6, "hit": False, "benchmark_hit": False}] * 2
    )
    summary = _calibration_summary(observations, cross_sectional=True)
    assert summary["n"] == 6
    assert summary["effective_n"] == 3  # B4 — n // 2


def test_calibration_summary_pushes_and_abstained_excluded_and_counted_separately():
    observations = (
        [{"p": 0.6, "hit": True, "benchmark_hit": True}] * 2
        + [{"p": 0.6, "hit": None, "benchmark_hit": False, "abstained": False}]  # push
        + [{"p": 0.6, "hit": None, "benchmark_hit": None, "abstained": True}]  # uncovered/abstained
    )
    summary = _calibration_summary(observations, cross_sectional=False)
    assert summary["n"] == 2  # pushes/abstained excluded from the graded denominator
    assert summary["pushes"] == 1
    assert summary["abstained"] == 1
    assert summary["abstention_rate"] == pytest.approx(1 / 4)


def test_calibration_summary_empty_observations_never_divides_by_zero():
    summary = _calibration_summary([], cross_sectional=False)
    assert summary["n"] == 0
    assert summary["effective_n"] == 0
    assert summary["brier_mean"] is None
    assert summary["brier_ref"] is None
    assert summary["brier_skill"] is None
    assert summary["abstention_rate"] is None


# ── ShadowEngine.calibration()/chapter_calibration() ───────────────────────
async def test_calibration_window_reads_config_weeks_back_from_week_start():
    engine = ShadowEngine(redis=None, settings=_FakeSettings())
    captured: dict[str, object] = {}

    async def fake_rows(floor_date, ceiling_date):
        captured["floor"] = floor_date
        captured["ceiling"] = ceiling_date
        return []

    engine._load_calibration_rows = fake_rows
    await engine.calibration("2026-W10")

    assert captured["ceiling"] == _week_start("2026-W10")
    assert captured["floor"] == _week_start("2026-W10") - timedelta(weeks=104)


async def test_calibration_empty_outcomes_renders_pending_groups_with_n_zero_no_exception():
    engine = ShadowEngine(redis=None, settings=_FakeSettings())

    async def fake_rows(floor_date, ceiling_date):
        return [
            {"issued_by": "shadow:technicals", "horizon_days": 28, "status": "pending"},
            {"issued_by": "shadow:technicals", "horizon_days": 28, "status": "pending"},
            {"issued_by": "human", "horizon_days": 28, "status": "pending"},
        ]

    engine._load_calibration_rows = fake_rows
    board = await engine.calibration("2026-W36")

    assert board["engine"] == "calibration-v1"
    assert isinstance(board["groups"], list)
    assert len(board["groups"]) == 2  # one per (issued_by, horizon_days)
    for g in board["groups"]:
        assert g["summary"]["n"] == 0
        assert g["bins"] is None
        assert g["pending"] > 0


async def test_calibration_constant_probability_machine_group_skips_bins():
    engine = ShadowEngine(redis=None, settings=_FakeSettings())

    async def fake_rows(floor_date, ceiling_date):
        return [
            {"issued_by": "shadow:technicals", "horizon_days": 28, "status": "resolved",
             "p": 0.5, "hit": True, "abstained": False, "benchmark_hit": True},
            {"issued_by": "shadow:technicals", "horizon_days": 28, "status": "resolved",
             "p": 0.5, "hit": False, "abstained": False, "benchmark_hit": False},
        ]

    engine._load_calibration_rows = fake_rows
    board = await engine.calibration("2026-W36")

    assert len(board["groups"]) == 1
    group = board["groups"][0]
    assert group["bins"] is None
    assert group["bins_note"] is not None and "tautological" in group["bins_note"]
    assert group["summary"]["brier_mean"] == pytest.approx(0.25)
    assert group["summary"]["brier_ref"] == pytest.approx(0.25)
    assert group["summary"]["brier_skill"] == pytest.approx(0.0)


async def test_calibration_varying_probability_human_group_gets_bins():
    engine = ShadowEngine(redis=None, settings=_FakeSettings())

    async def fake_rows(floor_date, ceiling_date):
        return [
            {"issued_by": "human", "horizon_days": 14, "status": "resolved",
             "p": 0.2, "hit": False, "abstained": False, "benchmark_hit": False},
            {"issued_by": "human", "horizon_days": 14, "status": "resolved",
             "p": 0.8, "hit": True, "abstained": False, "benchmark_hit": True},
        ]

    engine._load_calibration_rows = fake_rows
    board = await engine.calibration("2026-W36")

    group = board["groups"][0]
    assert group["bins"] is not None
    assert group["bins_note"] is None
    assert len(group["bins"]) == 5


async def test_calibration_deflation_note_present_when_two_or_more_groups_graded():
    engine = ShadowEngine(redis=None, settings=_FakeSettings())

    async def fake_rows(floor_date, ceiling_date):
        rows = []
        for tool in ("technicals", "patterns"):
            rows.append({"issued_by": f"shadow:{tool}", "horizon_days": 28, "status": "resolved",
                         "p": 0.6, "hit": True, "abstained": False, "benchmark_hit": True})
            rows.append({"issued_by": f"shadow:{tool}", "horizon_days": 28, "status": "resolved",
                         "p": 0.6, "hit": False, "abstained": False, "benchmark_hit": False})
        return rows

    engine._load_calibration_rows = fake_rows
    board = await engine.calibration("2026-W36")

    assert len(board["groups"]) == 2
    for g in board["groups"]:
        assert g["deflation_note"] is not None and "k=2" in g["deflation_note"]


async def test_calibration_cross_sectional_group_effective_n_and_low_confidence():
    engine = ShadowEngine(redis=None, settings=_FakeSettings())

    async def fake_rows(floor_date, ceiling_date):
        rows = []
        for _ in range(3):
            rows.append({"issued_by": "shadow:factor", "horizon_days": 28, "status": "resolved",
                         "p": 0.6, "hit": True, "abstained": False, "benchmark_hit": True})
            rows.append({"issued_by": "shadow:factor", "horizon_days": 28, "status": "resolved",
                         "p": 0.6, "hit": False, "abstained": False, "benchmark_hit": False})
        return rows

    engine._load_calibration_rows = fake_rows
    board = await engine.calibration("2026-W36")

    group = board["groups"][0]
    assert group["summary"]["n"] == 6
    assert group["summary"]["effective_n"] == 3
    assert group["low_confidence"] is True  # 3 < forecast_probability_min_n (8)


async def test_chapter_calibration_wraps_how_to_read_and_keeps_groups_and_engine():
    engine = ShadowEngine(redis=None, settings=_FakeSettings())

    async def fake_rows(floor_date, ceiling_date):
        return []

    engine._load_calibration_rows = fake_rows
    chapter = await engine.chapter_calibration("2026-W36")

    assert chapter["engine"] == "calibration-v1"
    assert chapter["groups"] == []
    assert "Brier" in chapter["how_to_read"]
    assert "bins" in chapter["how_to_read"]
