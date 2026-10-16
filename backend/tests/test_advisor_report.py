"""Tests for weekly Advisor report generation.

Pure fixture tests, no live Postgres/Redis/egress — mirrors `test_advisor.py`
and `test_demo_domains.py`'s conventions: DB touches are monkeypatched at the
module-level function seam (`advisor_report._last_report_week` etc.), fakes
stand in for the wealthfolio/regime/advisor/LLM bridges.

Covers the spec acceptance points plus the review fixes:
  (a) catch-up — container down over an ISO-week boundary produces exactly
      ONE catch-up report on boot; an immediate re-run within the same week
      inserts nothing (`report_due` alone, then the full orchestration).
      Also: the gate is content-independent (`max(report_week)`, not
      "newest row"), and closes even on a zero-recommendation week.
  (b) narration — a failed probe (warnings on the LLM response) ships an
      un-narrated, visibly-marked report with a distinct absence reason from
      "never attempted".
  (c) snapshots + recommendations persist inside one transaction.
  (d) `_load_overrides` cooldown-key/metric-source fixture coverage.
  (e) `assemble_report` / `iso_week` determinism.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
from datetime import date, datetime, timedelta, timezone
from typing import Any

import app.services.advisor_report as advisor_report_module
from app.adapters.llm.base import Grounding, LLMResponse
from app.config import Settings
from app.services.advisor_report import (
    _rule_arithmetic,
    assemble_report,
    component_set_id,
    generate_weekly_report,
    iso_week,
    report_due,
)


def _settings(**kw: Any) -> Settings:
    kw.setdefault("demo_mode", False)
    return Settings(
        postgres_user="u", postgres_password="p", postgres_db="d", redis_password="r", **kw,
    )


# ── iso_week / report_week format ────────────────────────────────────────
def test_iso_week_label_matches_report_week_format():
    assert iso_week(date(2026, 8, 17)) == "2026-W34"  # a Monday
    assert iso_week(date(2026, 1, 1)) == "2026-W01"  # zero-padded single-digit week
    assert iso_week(datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc)) == "2026-W34"


# ── (a) catch-up: pure `report_due` gate ─────────────────────────────────
def test_report_due_catchup_no_prior_report_is_always_due():
    now = datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc)
    assert report_due(None, now) is True
    assert report_due("not-a-week", now) is True  # malformed record fails open


def test_report_due_catchup_rerun_same_week_adds_nothing():
    now = datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc)
    this_week = iso_week(now)
    assert report_due(this_week, now) is False


def test_report_due_catchup_after_container_downtime_across_boundary_fires_once():
    last_run = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)  # prior Monday
    last_week = iso_week(last_run)
    # container down over the week boundary; boots up 8 days later, mid the NEW week
    boot_now = last_run + timedelta(days=8)
    assert report_due(last_week, boot_now) is True  # exactly one catch-up run fires
    # immediately after that catch-up run persists, `last_report_week` becomes
    # boot_now's week -> an immediate re-run at (about) the same instant adds nothing
    new_week = iso_week(boot_now)
    assert report_due(new_week, boot_now) is False


# ── orchestration fakes ────────────────────────────────────────────────────
class _FakeHoldingAsset:
    def __init__(self, symbol: str, qty: float, value: float, cost_basis: float | None = None) -> None:
        self.symbol = symbol
        self.quantity = qty
        self.value = value
        self.cost_basis = cost_basis


class _FakeHoldings:
    def __init__(self, by_asset: list[_FakeHoldingAsset] | None = None) -> None:
        self.total = 100_000.0
        self.cash = 10_000.0
        self.by_asset = by_asset if by_asset is not None else [_FakeHoldingAsset("BTC", 1.0, 90_000.0)]


class _FakeWF:
    def __init__(self, holdings: _FakeHoldings | None = None) -> None:
        self._holdings = holdings or _FakeHoldings()

    async def holdings(self) -> _FakeHoldings:
        return self._holdings


class _FakeRegimeReport:
    state = "neutral"
    score = 0.5
    components: list[Any] = []


class _FakeRegimeEngine:
    def __init__(self, redis: Any, settings: Any) -> None:
        pass

    async def regime(self):
        return _FakeRegimeReport(), {}


class _FakeAdvisor:
    def __init__(self, redis: Any, settings: Any, rows: list[dict] | None = None) -> None:
        self.calls = 0
        self._rows = rows

    async def evaluate(self, as_of, report_week, overrides=None):
        self.calls += 1
        if self._rows is not None:
            return [{**r, "report_week": report_week} for r in self._rows]
        return [{
            "rule_id": "cash_floor_band", "as_of": as_of, "report_week": report_week,
            "action": "hold", "sizing": None, "input_snapshot": {}, "regime_component_set_id": None,
            "status": "evaluated", "tax_cost_arithmetic": None, "note": "ok",
        }]

    def rule_status(self):
        return {"weight_band_trim": "active", "cash_floor_band": "active",
                "dca_modulation": "inactive (no contribution schedule set)"}


class _FakeDB:
    """Stand-in for the `AsyncSession` yielded by the monkeypatched
    `session_scope` — `_persist_snapshots`/`_persist_recommendations` are
    also monkeypatched away in these tests, so nothing ever calls a real
    method on it except `commit()`."""

    async def commit(self) -> None:
        pass


class _Recorder:
    """Records every persistence call the orchestration makes, and answers
    `_last_report_week()` the same way the real function does (max across
    BOTH snapshot and recommendation report_weeks) from its
    own in-memory state. Also counts `session_scope()` entries so a test can
    assert snapshots + recommendations share exactly one transaction."""

    def __init__(self) -> None:
        self.snap_week: str | None = None
        self.rec_week: str | None = None
        self.snapshot_calls: list[list[dict]] = []
        self.recommendation_calls: list[list[dict]] = []
        self.narration_calls: list[tuple] = []
        self.session_scope_entries = 0

    async def last_report_week(self) -> str | None:
        weeks = [w for w in (self.rec_week, self.snap_week) if w]
        return max(weeks) if weeks else None

    async def persist_snapshots(self, db, rows: list[dict]) -> None:
        self.snapshot_calls.append(rows)
        if rows:
            self.snap_week = rows[0]["report_week"]

    async def persist_recommendations(self, db, rows: list[dict], regime_component_set_id) -> None:
        self.recommendation_calls.append(rows)
        if rows:
            self.rec_week = rows[0]["report_week"]

    async def persist_narration(self, report_week: str, facts: dict, narration: dict) -> None:
        self.narration_calls.append((report_week, facts, narration))

    def session_scope(self):
        self.session_scope_entries += 1
        return self._ctx()

    @contextlib.asynccontextmanager
    async def _ctx(self):
        yield _FakeDB()


def _wire_fakes(monkeypatch, recorder: _Recorder, adapter=None, holdings=None, rows=None) -> None:
    monkeypatch.setattr(advisor_report_module, "session_scope", recorder.session_scope)
    monkeypatch.setattr(advisor_report_module, "_last_report_week", recorder.last_report_week)
    monkeypatch.setattr(advisor_report_module, "_persist_snapshots", recorder.persist_snapshots)
    monkeypatch.setattr(advisor_report_module, "_persist_recommendations", recorder.persist_recommendations)
    monkeypatch.setattr(advisor_report_module, "_persist_narration", recorder.persist_narration)
    monkeypatch.setattr(advisor_report_module, "_load_overrides", lambda as_of: asyncio.sleep(0, result={}))
    monkeypatch.setattr(advisor_report_module, "make_wealthfolio", lambda redis, settings: _FakeWF(holdings))
    monkeypatch.setattr(advisor_report_module, "MarketResearchEngine", _FakeRegimeEngine)
    monkeypatch.setattr(
        advisor_report_module, "make_advisor", lambda redis, settings: _FakeAdvisor(redis, settings, rows)
    )
    if adapter is not None:
        monkeypatch.setattr(advisor_report_module, "build_llm_adapter", lambda name, settings: adapter)


# ── (a) catch-up: full orchestration, everything faked ───────────────────
def test_generate_weekly_report_catchup_across_boundary_fires_once_then_rerun_adds_nothing(monkeypatch):
    recorder = _Recorder()
    _wire_fakes(monkeypatch, recorder)
    settings = _settings(llm_provider="noop")
    now = datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc)

    report1 = asyncio.run(generate_weekly_report(object(), settings, now=now))
    assert report1 is not None
    assert len(recorder.snapshot_calls) == 1
    assert len(recorder.recommendation_calls) == 1

    # immediate re-run "on boot" at (about) the same instant -> report_due is
    # False -> nothing is touched past the last_report_week read (no second
    # snapshot/recommendation persist call at all).
    report2 = asyncio.run(generate_weekly_report(object(), settings, now=now))
    assert report2 is None
    assert len(recorder.snapshot_calls) == 1
    assert len(recorder.recommendation_calls) == 1


def test_generate_weekly_report_catchup_from_recorded_state_regenerates_current_week(monkeypatch):
    """Recorder seeded with a stale `last_report_week` (as if
    read straight from the DB) two weeks behind `now` -> exactly one
    generation call, stamped with the CURRENT week, not the stale one. This
    is the class of test that would have caught the
    "newest-by-created_at" bug: an out-of-order INSERT for an old week must
    never make this gate reopen the OLD week)."""
    recorder = _Recorder()
    recorder.snap_week = "2026-W33"
    recorder.rec_week = "2026-W33"
    _wire_fakes(monkeypatch, recorder)
    settings = _settings(llm_provider="noop")
    now = datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc)  # two ISO weeks after 2026-W33
    current_week = iso_week(now)
    assert current_week != "2026-W33"

    report = asyncio.run(generate_weekly_report(object(), settings, now=now))
    assert report is not None
    assert report["report_week"] == current_week
    assert len(recorder.recommendation_calls) == 1
    assert recorder.recommendation_calls[0][0]["report_week"] == current_week
    assert len(recorder.snapshot_calls) == 1
    assert recorder.snapshot_calls[0][0]["report_week"] == current_week


def test_generate_weekly_report_zero_recommendation_rows_still_closes_the_gate(monkeypatch):
    """A week where `AdvisorEngine.evaluate()` returns an
    empty list (the engine never does this today, but the gate must not depend on
    that holding forever) must still close the gate via the snapshot rows —
    otherwise the tick re-runs the full pipeline on every poll for the rest
    of the week."""
    recorder = _Recorder()
    _wire_fakes(monkeypatch, recorder, rows=[])
    settings = _settings(llm_provider="noop")
    now = datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc)

    report1 = asyncio.run(generate_weekly_report(object(), settings, now=now))
    assert report1 is not None
    assert recorder.recommendation_calls == [[]]  # the (empty) call still happened
    assert recorder.rec_week is None  # empty rows -> recommendations side never sets a week
    assert recorder.snap_week == iso_week(now)  # but snapshots did -> gate closes anyway

    report2 = asyncio.run(generate_weekly_report(object(), settings, now=now))
    assert report2 is None  # gate correctly closed even though recommendations was empty
    assert len(recorder.snapshot_calls) == 1


def test_generate_weekly_report_snapshots_and_recommendations_share_one_transaction(monkeypatch):
    """Exactly one `session_scope()` entry covers both
    persist calls for a given run."""
    recorder = _Recorder()
    _wire_fakes(monkeypatch, recorder)
    settings = _settings(llm_provider="noop")
    now = datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc)

    asyncio.run(generate_weekly_report(object(), settings, now=now))
    assert recorder.session_scope_entries == 1


# ── DEMO_MODE: the tick is a total no-op ─────────────────────────────
def test_generate_weekly_report_is_a_total_noop_in_demo_mode(monkeypatch):
    """No advisor code path may perform live egress under DEMO_MODE — the
    weekly tick must not fetch real holdings, probe the real regime/LLM, or
    persist synthetic-derived rows into the real DB. Proven the same way
    `test_demo_domains.py` proves a DB-touch guard: `session_scope` and
    every fetch seam are wired to explode the instant they're called;
    reaching `report_due`/any DB read at all fails the test before the
    assertion does."""
    recorder = _Recorder()
    _wire_fakes(monkeypatch, recorder)

    def _boom(*args: object, **kw: object):
        raise AssertionError("no advisor fetch/persistence seam may be reached under DEMO_MODE")

    monkeypatch.setattr(advisor_report_module, "make_wealthfolio", _boom)
    monkeypatch.setattr(advisor_report_module, "MarketResearchEngine", _boom)
    monkeypatch.setattr(advisor_report_module, "_last_report_week", _boom)
    settings = _settings(demo_mode=True, llm_provider="noop")
    now = datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc)

    report = asyncio.run(generate_weekly_report(object(), settings, now=now))
    assert report is None
    assert recorder.session_scope_entries == 0
    assert recorder.snapshot_calls == []
    assert recorder.recommendation_calls == []
    assert recorder.narration_calls == []


def test_generate_weekly_report_regime_down_degrades_to_absent_section(monkeypatch):
    """The `macro` profile being down must never crash the tick — regime
    section comes back absent, everything else still persists."""
    recorder = _Recorder()
    _wire_fakes(monkeypatch, recorder)

    class _BoomRegime:
        def __init__(self, redis: Any, settings: Any) -> None:
            pass

        async def regime(self):
            raise ConnectionError("macro sidecar unreachable")

    monkeypatch.setattr(advisor_report_module, "MarketResearchEngine", _BoomRegime)
    settings = _settings(llm_provider="noop")
    now = datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc)

    report = asyncio.run(generate_weekly_report(object(), settings, now=now))
    assert report is not None
    assert report["what_happened"]["regime_state"] is None
    assert len(recorder.snapshot_calls) == 1  # snapshots still persisted (regime null, not absent row)


def test_generate_weekly_report_all_cash_portfolio_nav_cash_not_zeroed(monkeypatch):
    """`assemble_report` must read nav/cash from the explicit parameters, not
    `snapshots[0]` — an all-cash book has an EMPTY snapshots list but a real
    nav/cash."""
    recorder = _Recorder()
    _wire_fakes(monkeypatch, recorder, holdings=_FakeHoldings(by_asset=[]))
    settings = _settings(llm_provider="noop")
    now = datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc)

    report = asyncio.run(generate_weekly_report(object(), settings, now=now))
    assert report["what_happened"]["positions"] == []
    assert report["what_happened"]["nav_eur"] == 100_000.0
    assert report["what_happened"]["cash_eur"] == 10_000.0


# ── (b) narration ─────────────────────────────────────────────────────────
class _FakeAdapter:
    def __init__(self, resp: LLMResponse) -> None:
        self._resp = resp

    async def complete(self, messages, *, facts=None) -> LLMResponse:
        return self._resp


def test_narration_absent_when_probe_returns_warnings(monkeypatch):
    recorder = _Recorder()
    adapter = _FakeAdapter(LLMResponse(
        text="", provider="claude-code", grounding=Grounding.unverifiable,
        warnings=["claude-code: binary not found (is the ai-runner image built?)"],
    ))
    _wire_fakes(monkeypatch, recorder, adapter=adapter)
    settings = _settings(llm_provider="claude-code")
    now = datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc)

    report = asyncio.run(generate_weekly_report(object(), settings, now=now))
    assert report["narrated"] is False
    assert report["narration"] is None
    assert report["narration_absent_reason"] == "narration probe failed (see backend logs)"
    assert recorder.narration_calls == []  # nothing written to ai_runs on a failed probe


def test_narration_present_when_probe_succeeds(monkeypatch):
    recorder = _Recorder()
    adapter = _FakeAdapter(LLMResponse(
        text="Your portfolio held steady this week.", provider="claude-code",
        grounding=Grounding.grounded, warnings=[],
    ))
    _wire_fakes(monkeypatch, recorder, adapter=adapter)
    settings = _settings(llm_provider="claude-code")
    now = datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc)

    report = asyncio.run(generate_weekly_report(object(), settings, now=now))
    assert report["narrated"] is True
    assert report["narration"]["text"] == "Your portfolio held steady this week."
    assert report["narration"]["grounding"] == "grounded"
    assert report["narration_absent_reason"] is None
    assert len(recorder.narration_calls) == 1


def test_narration_skipped_entirely_when_llm_provider_is_noop(monkeypatch):
    """No-op provider (the default, `ai` profile not up) never even attempts
    a probe call — `build_llm_adapter` is not invoked at all, and the report
    is marked "not_configured", distinct from a genuine probe failure."""
    recorder = _Recorder()
    _wire_fakes(monkeypatch, recorder)

    def _boom(name, settings):
        raise AssertionError("build_llm_adapter must not be called when llm_provider is noop")

    monkeypatch.setattr(advisor_report_module, "build_llm_adapter", _boom)
    settings = _settings(llm_provider="noop")
    now = datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc)

    report = asyncio.run(generate_weekly_report(object(), settings, now=now))
    assert report["narrated"] is False
    assert report["narration_absent_reason"] == "not configured in this deployment (ai profile not enabled)"


def test_narration_absent_reason_distinguishes_not_configured_from_probe_failed():
    """Pure `assemble_report` check: the reason string is only surfaced when
    `narration is None`, and is exactly whatever the caller passed."""
    r_not_configured = assemble_report(
        "2026-W34", 100.0, 10.0, [], [], {}, "neutral", None, "not configured in this deployment (ai profile not enabled)"
    )
    r_failed = assemble_report(
        "2026-W34", 100.0, 10.0, [], [], {}, "neutral", None, "narration probe failed (see backend logs)"
    )
    r_ok = assemble_report(
        "2026-W34", 100.0, 10.0, [], [], {}, "neutral", {"text": "x", "grounding": "grounded", "model": None}, "ignored-when-narrated"
    )
    assert r_not_configured["narration_absent_reason"] == "not configured in this deployment (ai profile not enabled)"
    assert r_failed["narration_absent_reason"] == "narration probe failed (see backend logs)"
    assert r_ok["narration_absent_reason"] is None  # never surfaced once narrated


# ── B1: read-route narration reason ────
def test_narration_absent_reason_for_reassembly_not_configured_when_noop():
    settings = _settings(llm_provider="noop")
    assert (
        advisor_report_module.narration_absent_reason_for_reassembly(settings, None)
        == "not configured in this deployment (ai profile not enabled)"
    )


def test_narration_absent_reason_for_reassembly_probe_failed_when_configured_but_no_row():
    settings = _settings(llm_provider="claude-code")
    assert (
        advisor_report_module.narration_absent_reason_for_reassembly(settings, None)
        == "narration probe failed (see backend logs)"
    )


def test_narration_absent_reason_for_reassembly_none_when_narrated_regardless_of_provider():
    narration = {"text": "steady week", "grounding": "grounded", "model": None}
    for provider in ("noop", "claude-code"):
        settings = _settings(llm_provider=provider)
        assert advisor_report_module.narration_absent_reason_for_reassembly(settings, narration) is None


# ── (d) `_load_overrides` cooldown keys + metric sources ──────────────────
class _FakeRecommendationRow:
    def __init__(
        self, rule_id: str, status: str, input_snapshot: dict, tax_cost_arithmetic: dict | None,
        report_week: str, as_of: date, created_at: datetime,
    ) -> None:
        self.rule_id = rule_id
        self.status = status
        self.input_snapshot = input_snapshot
        self.tax_cost_arithmetic = tax_cost_arithmetic
        self.report_week = report_week
        self.as_of = as_of
        self.created_at = created_at


class _FakeOverridesResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeOverridesDB:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    async def execute(self, stmt):
        return _FakeOverridesResult(self._rows)


def test_load_overrides_builds_exact_cooldown_keys_and_metric_sources(monkeypatch):
    """Rows returned newest-first (matching the real query's ORDER BY
    created_at desc). rule 1 (weight_band_trim) is scoped per-symbol
    ("rule_id:SYMBOL"), sourced from `tax_cost_arithmetic["benefit_eur"]`;
    rule 2 (cash_floor_band) is a bare rule_id, sourced from
    `input_snapshot["shortfall_eur"]` when there's no `benefit_eur` (it isn't
    ΔCVaR-gated)."""
    rows = [
        _FakeRecommendationRow(
            "weight_band_trim", "overridden", {"symbol": "BTC"}, {"benefit_eur": 123.45},
            "2026-W33", date(2026, 8, 10), datetime(2026, 8, 10, tzinfo=timezone.utc),
        ),
        _FakeRecommendationRow(
            "cash_floor_band", "overridden", {"shortfall_eur": 500.0}, None,
            "2026-W32", date(2026, 8, 3), datetime(2026, 8, 3, tzinfo=timezone.utc),
        ),
    ]

    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        yield _FakeOverridesDB(rows)

    monkeypatch.setattr(advisor_report_module, "session_scope", _fake_session_scope)
    overrides = asyncio.run(advisor_report_module._load_overrides(date(2026, 8, 17)))
    assert overrides == {
        "weight_band_trim:BTC": ("2026-W33", 123.45),
        "cash_floor_band": ("2026-W32", 500.0),
    }


def test_load_overrides_newest_wins_per_key(monkeypatch):
    rows = [
        _FakeRecommendationRow(  # newest (rows arrive created_at-desc)
            "weight_band_trim", "overridden", {"symbol": "BTC"}, {"benefit_eur": 999.0},
            "2026-W34", date(2026, 8, 17), datetime(2026, 8, 17, tzinfo=timezone.utc),
        ),
        _FakeRecommendationRow(  # older, same key -> must be ignored
            "weight_band_trim", "overridden", {"symbol": "BTC"}, {"benefit_eur": 1.0},
            "2026-W30", date(2026, 7, 20), datetime(2026, 7, 20, tzinfo=timezone.utc),
        ),
    ]

    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        yield _FakeOverridesDB(rows)

    monkeypatch.setattr(advisor_report_module, "session_scope", _fake_session_scope)
    overrides = asyncio.run(advisor_report_module._load_overrides(date(2026, 8, 17)))
    assert overrides == {"weight_band_trim:BTC": ("2026-W34", 999.0)}


# ── determinism ────────────────────────────────────────────────────────────
def test_assemble_report_report_week_output_is_deterministic():
    snapshots = [{"symbol": "BTC", "value_eur": 90_000.0, "qty": 1.0}]
    recs = [{
        "rule_id": "cash_floor_band", "action": "hold", "status": "evaluated", "note": "ok",
    }]
    rule_status = {"weight_band_trim": "active"}

    r1 = assemble_report("2026-W34", 100_000.0, 10_000.0, snapshots, recs, rule_status, "neutral", None, "not configured in this deployment (ai profile not enabled)")
    r2 = assemble_report("2026-W34", 100_000.0, 10_000.0, snapshots, recs, rule_status, "neutral", None, "not configured in this deployment (ai profile not enabled)")
    assert json.dumps(r1, sort_keys=True, default=str) == json.dumps(r2, sort_keys=True, default=str)
    assert r1["report_week"] == "2026-W34"
    assert r1["narrated"] is False


# ── follow-up: rule_evaluations.arithmetic ──
def test_assemble_report_suppressed_row_exposes_would_fire_at_arithmetic():
    """A SUPPRESSED row (below materiality or on cooldown -> status=evaluated,
    action=hold) must still expose its would-fire-at figures on the client —
    the legacy {rule_id, action, status, note} projection stripped
    `input_snapshot`/`tax_cost_arithmetic` entirely, leaving no way to reach
    materiality thresholds or the sell-gate's benefit/threshold except by
    string-parsing `note`. `material` is the UI's near-miss SIGNAL: a
    suppressed row that's already material (only cooldown/sell-gate blocked
    it) reads very differently from one that never even cleared the
    materiality gate — proven here on both a suppressed row (material=False)
    and, cheaply in the same call, a proposed row (material=True)."""
    recs = [{
        "rule_id": "weight_band_trim", "action": "hold", "status": "evaluated",
        "note": "in band; below materiality",
        "input_snapshot": {
            "symbol": "BTC", "asset_class": "CRYPTO", "trim_value_eur": 45.0,
            "material": False,
            "materiality_eur_threshold": 50.0, "materiality_nav_pct_threshold": 0.5,
        },
        "tax_cost_arithmetic": {
            "benefit_eur": 12.0, "threshold_eur": 30.0, "cost_eur": 18.0, "fires": False,
            "tax_eur": 10.0, "venue_cost_eur": 8.0,  # extra keys NOT on the allow-list
        },
    }, {
        "rule_id": "weight_band_trim", "action": "trim", "status": "proposed",
        "note": "over band; material and cleared the sell gate",
        "input_snapshot": {
            "symbol": "ETH", "asset_class": "CRYPTO", "trim_value_eur": 500.0,
            "material": True,
            "materiality_eur_threshold": 50.0, "materiality_nav_pct_threshold": 0.5,
        },
        "tax_cost_arithmetic": {"benefit_eur": 80.0, "threshold_eur": 30.0, "cost_eur": 18.0, "fires": True},
    }]
    r = assemble_report("2026-W34", 100_000.0, 10_000.0, [], recs, {}, "neutral", None, "n/a")
    suppressed, proposed = r["where_we_stand"]["rule_evaluations"]
    # the four legacy keys are untouched
    assert suppressed["rule_id"] == "weight_band_trim"
    assert suppressed["action"] == "hold"
    assert suppressed["status"] == "evaluated"
    assert suppressed["note"] == "in band; below materiality"
    # structured arithmetic: named fields only, present ones surfaced
    assert suppressed["arithmetic"] == {
        "symbol": "BTC", "trim_value_eur": 45.0, "material": False,
        "materiality_eur_threshold": 50.0, "materiality_nav_pct_threshold": 0.5,
        "benefit_eur": 12.0, "threshold_eur": 30.0, "cost_eur": 18.0, "fires": False,
    }
    assert suppressed["arithmetic"]["material"] is False
    # the full input_snapshot/tax_cost_arithmetic never leaks
    assert "asset_class" not in suppressed["arithmetic"]
    assert "tax_eur" not in suppressed["arithmetic"]
    assert "venue_cost_eur" not in suppressed["arithmetic"]
    # a proposed (fired) row's near-miss signal reads the other way
    assert proposed["arithmetic"]["material"] is True


def test_rule_arithmetic_absent_tolerant_for_cash_floor_band_shape():
    """`cash_floor_band` has no top-level `symbol` (its per-symbol detail
    lives under `legs`, never surfaced), and its `tax_cost_arithmetic` is
    the sell-gate's cost-only dict — no `benefit_eur`/`threshold_eur`/`fires`
    (those are `weight_band_trim`-only, sell-gate fields)."""
    row = {
        "input_snapshot": {
            "shortfall_eur": 3297.74, "materiality_eur_threshold": 50.0,
            "materiality_nav_pct_threshold": 0.5, "legs": [{"symbol": "BTC"}],
        },
        "tax_cost_arithmetic": {"tax_eur": 906.88, "cost_eur": 948.57, "venue_cost_eur": 41.69},
    }
    assert _rule_arithmetic(row) == {
        "shortfall_eur": 3297.74, "materiality_eur_threshold": 50.0,
        "materiality_nav_pct_threshold": 0.5, "cost_eur": 948.57,
    }


def test_rule_arithmetic_projects_tax_label_and_cost_basis_available():
    """A row's would-fire-at figures previously
    stopped at benefit/threshold/cost/fires — the binding "indicative
    estimate, not tax advice" label was only reachable through the fully
    undisclosed `tax_cost_arithmetic`, so a client showing "fires at €X" at
    rest had no label alongside it."""
    row = {
        "input_snapshot": {"symbol": "BTC", "trim_value_eur": 500.0, "material": True},
        "tax_cost_arithmetic": {
            "benefit_eur": 80.0, "threshold_eur": 30.0, "cost_eur": 18.0, "fires": True,
            "tax_label": "indicative estimate, not tax advice", "cost_basis_available": False,
        },
    }
    arithmetic = _rule_arithmetic(row)
    assert arithmetic["tax_label"] == "indicative estimate, not tax advice"
    assert arithmetic["cost_basis_available"] is False


def test_rule_arithmetic_empty_when_no_snapshot_or_cost_present():
    assert _rule_arithmetic({}) == {}
    assert _rule_arithmetic({"input_snapshot": {}, "tax_cost_arithmetic": None}) == {}


def test_component_set_id_stable_and_none_when_empty():
    class _C:
        def __init__(self, key, weight):
            self.key = key
            self.weight = weight

    a = component_set_id([_C("vix", 0.3), _C("hy_spread", 0.7)])
    b = component_set_id([_C("hy_spread", 0.7), _C("vix", 0.3)])  # order-independent
    assert a == b
    assert component_set_id([]) is None
