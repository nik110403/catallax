"""Tests for the Portfolio Advisor rule engine.

Pure fixture tests: fake wealthfolio/ledger/risk/regime bridges are injected
directly into `AdvisorEngine.__init__` (its documented construction seam), so
nothing here touches Redis, Postgres, or live egress. `AdvisorEngine.evaluate`
takes no wall-clock input other than the caller-supplied `as_of`/`report_week`,
so identical fixtures must produce byte-identical serialized rows (`as_of` is a
`date` object in each row — JSON-dumped with `default=str` for the diff, same
as any other date-bearing API payload in this codebase).

Covers the four acceptance points:
  1. determinism — engine run twice on fixed fixtures, diff serialized rows.
  2. sell-gate flip — same-unit € arithmetic, benefit just above vs below
     cost × ratio.
  3. static book -> do-nothing week (no `proposed` rows; dormant DCA emits none).
  4. clearing-accounts-≠0 refusal, emitted as an output row with the reason.
Plus targeted regression coverage from a review round: per-position rule-1
evaluation, the rule-2 ruling (not ΔCVaR-gated, legs recorded),
per-symbol cooldown scoping, and the post-trim weight-vector construction.
"""
from __future__ import annotations

import asyncio
import json
import math
from datetime import date
from decimal import Decimal
from typing import Any

import pytest

from app.config import Settings
from app.services.advisor import AdvisorEngine
from app.services.risk import Coverage, VarReport
from app.services.wealthfolio import Holdings, HoldingAsset

AS_OF = date(2026, 8, 17)  # a Monday
REPORT_WEEK = "2026-W34"


def _settings(**kw: Any) -> Settings:
    kw.setdefault("demo_mode", False)
    # Explicit test fixtures (the shipped defaults are neutral and configurable).
    kw.setdefault("advisor_weight_bands", "CRYPTO:0.65,EQUITY:0.30,ETF:0.30,COMMODITY:0.15,OTHER:0.20")  # arbitrary fixture values (tests below are hand-computed against them)
    kw.setdefault("advisor_tax_rate", 0.25)
    return Settings(
        postgres_user="u", postgres_password="p", postgres_db="d", redis_password="r", **kw,
    )


def _holdings(assets: list[tuple], cash: float) -> Holdings:
    """Each asset tuple is `(symbol, asset_class, value)` or, for SE4's
    cost_basis-carrying fixtures, `(symbol, asset_class, value, cost_basis)`."""
    by_asset = []
    for a in assets:
        sym, cls, v, *rest = a
        cb = rest[0] if rest else None
        by_asset.append(HoldingAsset(
            symbol=sym, name=sym, asset_class=cls, quantity=Decimal("1"), value=Decimal(str(v)),
            cost_basis=Decimal(str(cb)) if cb is not None else None,
        ))
    investments = sum(a[2] for a in assets)
    return Holdings(
        base="EUR", as_of=AS_OF.isoformat(), investments=Decimal(str(investments)),
        cash=Decimal(str(cash)), total=Decimal(str(investments + cash)), by_asset=by_asset, by_account=[],
    )


class FakeWF:
    def __init__(self, holdings: Holdings) -> None:
        self._holdings = holdings

    async def holdings(self) -> Holdings:
        return self._holdings


class FakeLedger:
    """`query_rows` dispatches on the BQL text — the three query sites rule 2
    ever uses (expenses aggregate, clearing balance, staleness), and only when
    the life floor is ACTIVE (`ADVISOR_LIFE_FLOOR_MULTIPLIER > 0`). At the
    dormant default (multiplier == 0, design decision) rule 2 makes
    ZERO ledger calls — `calls` records every `query_rows` invocation so a
    test can assert exactly that. `cash()` (the reverted B2 fix — ledger bank
    cash is no longer part of rule 2's liquid measure at all) is intentionally
    NOT implemented here any more: a resurrected call site would fail loudly
    (AttributeError) rather than silently reading a stale fixture."""

    def __init__(
        self, monthly_expenses_eur: float = 0.0,
        clearing_balances: dict[str, float] | None = None,
        last_txn_date: date | None = None,
    ) -> None:
        # advisor.py divides the 12mo sum by 12 -> feed it the annual total.
        self._annual_expenses = monthly_expenses_eur * 12.0
        self._clearing = clearing_balances or {}
        self._last_txn = last_txn_date
        self.calls: list[str] = []

    async def query_rows(self, bql: str) -> list:
        self.calls.append(bql)
        if "max(date)" in bql:
            return [[self._last_txn.isoformat()]] if self._last_txn else [[None]]
        if "Expenses" in bql:
            return [[{"EUR": self._annual_expenses}]]
        if "account ~ '^" in bql:  # clearing-balance prefix query
            acct = bql.split("account ~ '^")[1].split("'")[0]
            return [[{"EUR": self._clearing.get(acct, 0.0)}]]
        return []

    async def cash(self, currency: str | None = None) -> Decimal:
        if self._cash_raises:
            raise RuntimeError("ledger unreachable (fixture)")
        return Decimal(str(self._ledger_cash_eur))


def _coverage() -> Coverage:
    return Coverage(
        n_assets_total=0, n_covered=0, n_skipped=0, covered_weight_pct=100.0,
        n_observations=100, window_start=None, window_end=None, by_reason={},
        cash_weight_pct=0.0, assumption="fixture",
    )


def _var_report(cvar_pct: float, low_confidence: bool = False, n_scenarios: int = 100) -> VarReport:
    return VarReport(
        as_of=AS_OF.isoformat(), confidence=0.95, n_scenarios=n_scenarios, total_value_eur=100_000.0,
        var_eur=None, var_pct=None, cvar_eur=None, cvar_pct=cvar_pct, var99_eur=None, cvar99_eur=None,
        gaussian_var_eur=None, fat_tail_ratio=None, hist_vs_gaussian=None, worst_day=None,
        worst_day_pct=None, low_confidence=low_confidence, coverage=_coverage(),
    )


class FakeRisk:
    """Deterministic, pure function of the weight vector: CVaR grows with
    concentration (HHI-like), so a trim that de-concentrates the book always
    lowers CVaR — same monotone property the real engine has."""

    def __init__(self, base_pct: float = 1.0, concentration_scale: float = 20.0) -> None:
        self.base_pct = base_pct
        self.concentration_scale = concentration_scale
        self.calls: list[dict[str, float]] = []
        self.bundle_calls = 0

    async def bundle(self) -> None:
        self.bundle_calls += 1

    async def recompute_with_weights(self, weights: dict[str, float], frequency: str = "weekly"):
        self.calls.append(dict(weights))
        hhi = sum(w * w for w in weights.values())
        cvar_pct = self.base_pct + self.concentration_scale * hhi
        return _var_report(cvar_pct), {"warnings": [], "frequency": frequency}


class FakeRegime:
    def __init__(self, state: str = "neutral") -> None:
        self.state = state

    async def regime(self):
        class _R:
            state = self.state
        return _R(), {}


def _engine(holdings: Holdings, settings: Settings, ledger: FakeLedger | None = None,
            risk: FakeRisk | None = None, regime_state: str = "neutral") -> AdvisorEngine:
    return AdvisorEngine(
        redis=object(), settings=settings, wf=FakeWF(holdings), ledger=ledger or FakeLedger(),
        risk=risk or FakeRisk(), regime=FakeRegime(regime_state),
    )


# ── 1. determinism ─────────────────────────────────────────────────────
def test_evaluate_is_deterministic_on_fixed_fixtures():
    holdings = _holdings(
        [("BTC", "CRYPTO", 70_000.0), ("AAPL", "EQUITY", 20_000.0)], cash=10_000.0,
    )
    settings = _settings(advisor_life_floor_multiplier=6.0)
    ledger = FakeLedger(monthly_expenses_eur=500.0, last_txn_date=AS_OF)

    rows1 = asyncio.run(_engine(holdings, settings, ledger=ledger).evaluate(AS_OF, REPORT_WEEK))
    rows2 = asyncio.run(_engine(holdings, settings, ledger=ledger).evaluate(AS_OF, REPORT_WEEK))

    assert rows1  # sanity: something was evaluated
    assert json.dumps(rows1, sort_keys=True, default=str) == json.dumps(rows2, sort_keys=True, default=str)
    assert rows1[0]["as_of"] == AS_OF  # a native date, not a string


# ── 2. sell-gate flip (same-unit € arithmetic) ──────────────────────────
def test_sell_gate_flips_on_benefit_vs_cost_ratio():
    settings = _settings(
        advisor_review_horizon_weeks=12, advisor_benefit_cost_ratio=1.5,
        advisor_expected_return_map="CRYPTO:0.0,CASH:0.0", advisor_venue_cost_map="CRYPTO:flat:0.0",
        advisor_tax_rate=0.25, advisor_loss_offset_factor=0.0,
    )
    trimmed = [{"symbol": "BTC", "asset_class": "CRYPTO", "trim_value_eur": 1000.0}]
    current_weights = {"BTC": 0.7}
    nav = 100_000.0

    # cost_eur = 1000 * 0.25 = 250; threshold = 250 * 1.5 = 375.
    # benefit_eur = delta_cvar_pct/100 * nav * sqrt(12) — solve for the delta
    # that lands benefit just above / just below the threshold.
    threshold = 1000.0 * 0.25 * 1.5
    delta_above = (threshold + 1.0) / (nav * math.sqrt(12)) * 100.0
    delta_below = (threshold - 1.0) / (nav * math.sqrt(12)) * 100.0

    async def gate(delta_cvar_pct: float) -> dict:
        risk = FakeRisk()
        base = 5.0
        risk.recompute_with_weights = _fixed_delta_risk(base, delta_cvar_pct)  # type: ignore[method-assign]
        eng = AdvisorEngine(redis=object(), settings=settings, wf=FakeWF(_holdings([], 0)),
                             ledger=FakeLedger(), risk=risk, regime=FakeRegime())
        return await eng._sell_gate(trimmed, current_weights, nav)  # noqa: SLF001

    fired = asyncio.run(gate(delta_above))
    not_fired = asyncio.run(gate(delta_below))
    assert fired["fires"] is True
    assert not_fired["fires"] is False
    assert fired["cost_eur"] == not_fired["cost_eur"] == 250.0
    assert fired["scaling_assumption"]
    assert fired["tax_label"] == "indicative estimate, not tax advice"


# ── CVaR low-confidence passthrough ─────────
def test_sell_gate_carries_low_confidence_and_n_scenarios_from_cvar():
    """`risk_lookback_days=504` -> ~71 weekly windows < `risk_var_min_scenarios`
    (120) -> the real engine's `_var_block` sets `low_confidence=True` on both
    VarReports it hands to `recompute_with_weights` — that flag used to be
    dropped here instead of reaching the gate row, so a fired sell gate could
    look like it ran on full-strength CVaR when it didn't."""
    settings = _settings()
    trimmed = [{"symbol": "BTC", "asset_class": "CRYPTO", "trim_value_eur": 1000.0}]
    current_weights = {"BTC": 0.7}
    nav = 100_000.0
    risk = FakeRisk()

    async def _fn(weights: dict[str, float], frequency: str = "weekly"):
        return _var_report(5.0, low_confidence=True, n_scenarios=71), {"warnings": [], "frequency": frequency}
    risk.recompute_with_weights = _fn  # type: ignore[method-assign]

    eng = AdvisorEngine(redis=object(), settings=settings, wf=FakeWF(_holdings([], 0)),
                         ledger=FakeLedger(), risk=risk, regime=FakeRegime())
    gate = asyncio.run(eng._sell_gate(trimmed, current_weights, nav))  # noqa: SLF001
    assert gate["low_confidence"] is True
    assert gate["n_scenarios"] == 71


def _fixed_delta_risk(base_pct: float, delta_cvar_pct: float):
    """Returns a `recompute_with_weights` bound method stand-in: current call
    reports `base_pct`, the post-trim call reports `base_pct - delta_cvar_pct`
    (so current - post == delta_cvar_pct exactly, in the caller's same units)."""
    state = {"n": 0}

    async def _fn(weights: dict[str, float], frequency: str = "weekly"):
        state["n"] += 1
        pct = base_pct if state["n"] == 1 else base_pct - delta_cvar_pct
        return _var_report(pct), {"warnings": [], "frequency": frequency}

    return _fn


# ── 3. static book -> do-nothing week ───────────────────────────────────
def test_static_compliant_book_produces_no_proposed_rows():
    # BTC at 64.7% (< 65% crypto band), AAPL at 23.5% (< 30% equity band); nav
    # 85_000, cash 10_000 (11.8%) clears both the neutral 10% band (8_500) and
    # the 6x-monthly-expenses life floor (600).
    holdings = _holdings(
        [("BTC", "CRYPTO", 55_000.0), ("AAPL", "EQUITY", 20_000.0)], cash=10_000.0,
    )
    settings = _settings(advisor_dca_base=0.0)  # dormant DCA (the default)
    ledger = FakeLedger(monthly_expenses_eur=100.0, last_txn_date=AS_OF)  # floor 600 << band 8_500
    rows = asyncio.run(_engine(holdings, settings, ledger=ledger).evaluate(AS_OF, REPORT_WEEK))

    assert rows  # rules still leave an audit trail — one per evaluated position/rule
    assert {r["rule_id"] for r in rows} == {"weight_band_trim", "cash_floor_band"}
    assert {r["input_snapshot"]["symbol"] for r in rows if r["rule_id"] == "weight_band_trim"} == {"BTC", "AAPL"}
    assert all(r["status"] != "proposed" for r in rows)
    assert all(r["rule_id"] != "dca_modulation" for r in rows)  # dormant -> no rows at all


def test_dca_dormant_emits_no_rows_and_reports_inactive_label():
    holdings = _holdings([("BTC", "CRYPTO", 10_000.0)], cash=90_000.0)
    settings = _settings()  # ADVISOR_DCA_BASE default = 0.0
    eng = _engine(holdings, settings, ledger=FakeLedger(last_txn_date=AS_OF))
    rows = asyncio.run(eng.evaluate(AS_OF, REPORT_WEEK))
    assert not any(r["rule_id"] == "dca_modulation" for r in rows)
    assert eng.rule_status()["dca_modulation"] == "inactive (no contribution schedule set)"


# ── 4. clearing-accounts-≠0 refusal ─────────────────────────────────────
def test_nonzero_clearing_account_refuses_cash_floor_sizing():
    # The clearing-accounts guard only runs when the life floor is ACTIVE
    # (multiplier > 0) — it exists to protect the ledger-derived floor leg.
    holdings = _holdings([("AAPL", "EQUITY", 50_000.0)], cash=1_000.0)
    settings = _settings(advisor_clearing_accounts="Assets:Clearing:Broker", advisor_life_floor_multiplier=6.0)
    ledger = FakeLedger(
        monthly_expenses_eur=500.0, last_txn_date=AS_OF,
        clearing_balances={"Assets:Clearing:Broker": 22_700.0},
    )
    rows = asyncio.run(_engine(holdings, settings, ledger=ledger).evaluate(AS_OF, REPORT_WEEK))

    refusals = [r for r in rows if r["rule_id"] == "cash_floor_band" and r["action"] == "refuse"]
    assert len(refusals) == 1
    assert refusals[0]["status"] == "evaluated"
    assert "22700.00" in refusals[0]["note"] or "22700.0" in refusals[0]["note"]
    assert "clearing accounts sum to" in refusals[0]["note"]
    assert refusals[0]["input_snapshot"]["clearing_sum_eur"] == 22_700.0


def test_clearing_prefix_match_catches_sub_accounts():
    """A parent account (e.g. "Assets:Clearing") must catch its sub-accounts
    (e.g. "Assets:Clearing:Broker") via a prefix match, not just an exact one."""
    holdings = _holdings([("AAPL", "EQUITY", 50_000.0)], cash=1_000.0)
    settings = _settings(advisor_clearing_accounts="Assets:Clearing", advisor_life_floor_multiplier=6.0)
    ledger = FakeLedger(
        monthly_expenses_eur=500.0, last_txn_date=AS_OF,
        clearing_balances={"Assets:Clearing": 5_000.0},  # FakeLedger keys on the exact BQL prefix
    )
    rows = asyncio.run(_engine(holdings, settings, ledger=ledger).evaluate(AS_OF, REPORT_WEEK))
    refusals = [r for r in rows if r["rule_id"] == "cash_floor_band" and r["action"] == "refuse"]
    assert len(refusals) == 1


# ── rule 1: every position is evaluated individually ────────────────────
def test_weight_band_trim_evaluates_every_position_not_just_the_heaviest():
    holdings = _holdings(
        [("BTC", "CRYPTO", 66_000.0), ("ETH", "CRYPTO", 10_000.0), ("AAPL", "EQUITY", 14_000.0)],
        cash=10_000.0,
    )  # nav 100_000: BTC 66% (breaches 65%), ETH 10% (in-band), AAPL 14% (in-band)
    settings = _settings()
    ledger = FakeLedger(monthly_expenses_eur=100.0, last_txn_date=AS_OF)
    rows = asyncio.run(_engine(holdings, settings, ledger=ledger).evaluate(AS_OF, REPORT_WEEK))

    trim_rows = {r["input_snapshot"]["symbol"]: r for r in rows if r["rule_id"] == "weight_band_trim"}
    assert set(trim_rows) == {"BTC", "ETH", "AAPL"}  # every position got its own audit row
    assert trim_rows["ETH"]["status"] == "evaluated" and trim_rows["ETH"]["action"] == "hold"
    assert trim_rows["AAPL"]["status"] == "evaluated" and trim_rows["AAPL"]["action"] == "hold"
    # materiality thresholds in force are recorded on the breach row (finding 10)
    assert trim_rows["BTC"]["input_snapshot"]["materiality_eur_threshold"] == settings.advisor_materiality_eur
    assert (
        trim_rows["BTC"]["input_snapshot"]["materiality_nav_pct_threshold"]
        == settings.advisor_materiality_nav_pct
    )


def test_post_trim_weight_vector_only_changes_the_trimmed_symbol():
    # BTC breaches the 65% crypto band by exactly 1pp -> a €1,000 trim on
    # €100,000 NAV. ETH/AAPL stay in-band and must be untouched in the
    # post-trim weight vector fed to the sell gate.
    holdings = _holdings(
        [("BTC", "CRYPTO", 66_000.0), ("ETH", "CRYPTO", 10_000.0), ("AAPL", "EQUITY", 14_000.0)],
        cash=10_000.0,
    )
    settings = _settings()
    risk = FakeRisk()
    ledger = FakeLedger(monthly_expenses_eur=100.0, last_txn_date=AS_OF)  # target == cash -> rule 2 quiet
    eng = AdvisorEngine(redis=object(), settings=settings, wf=FakeWF(holdings), ledger=ledger,
                         risk=risk, regime=FakeRegime())
    asyncio.run(eng.evaluate(AS_OF, REPORT_WEEK))

    assert risk.bundle_calls == 1  # bundle() called exactly once, ahead of any recompute
    assert len(risk.calls) == 2  # only BTC breaches -> exactly one gate -> (current, post) pair
    current_call, post_call = risk.calls
    assert current_call == {"BTC": pytest.approx(0.66), "ETH": pytest.approx(0.10), "AAPL": pytest.approx(0.14)}
    assert post_call["BTC"] == pytest.approx(current_call["BTC"] - 0.01)
    assert post_call["ETH"] == pytest.approx(current_call["ETH"])
    assert post_call["AAPL"] == pytest.approx(current_call["AAPL"])


# ── rule 2: NOT ΔCVaR-gated ──────────────────────────
def test_cash_floor_fires_on_materiality_alone_and_records_legs():
    holdings = _holdings([("AAPL", "EQUITY", 90_000.0)], cash=2_000.0)  # nav 92_000
    # This fixture's single AAPL leg has no cost_basis (SE4's conservative
    # full-trim-value-as-gain fallback), so tax alone (25%) would exceed
    # the new default 10% cost-fraction floor —
    # that gate is covered by its own dedicated tests below; disable it here
    # so this test keeps testing what it always tested (materiality alone).
    settings = _settings(advisor_cost_fraction_max=1.0)
    risk = FakeRisk()
    ledger = FakeLedger(monthly_expenses_eur=100.0, last_txn_date=AS_OF)  # floor 600 << band 9_200
    eng = AdvisorEngine(redis=object(), settings=settings, wf=FakeWF(holdings), ledger=ledger,
                         risk=risk, regime=FakeRegime())
    rows = asyncio.run(eng.evaluate(AS_OF, REPORT_WEEK))

    cash_rows = [r for r in rows if r["rule_id"] == "cash_floor_band"]
    assert len(cash_rows) == 1
    row = cash_rows[0]
    assert row["status"] == "proposed" and row["action"] == "trim"
    assert row["sizing"] == pytest.approx(7_200.0)  # target 9_200 - cash 2_000
    assert row["input_snapshot"]["legs"] == [
        {"symbol": "AAPL", "asset_class": "EQUITY", "trim_value_eur": 7200.0, "cost_basis_eur": None}
    ]
    assert row["input_snapshot"]["materiality_eur_threshold"] == settings.advisor_materiality_eur
    # informational cost arithmetic, never a veto: no benefit/CVaR/fires key at all.
    cost = row["tax_cost_arithmetic"]
    assert set(cost) == {"tax_eur", "venue_cost_eur", "cost_eur", "cost_basis_available", "tax_label"}
    assert cost["tax_label"] == "indicative estimate, not tax advice"
    assert cost["tax_eur"] == pytest.approx(7200.0 * 0.25)
    # rule 1 (AAPL at 97.8%, way past its 30% band) will itself call the risk
    # engine, but rule 2's own path never does — it has no CVaR dependency.
    assert risk.calls  # sanity: rule 1 did fire and use the fake
    assert "benefit_eur" not in cost


# ── M3: rule 2's persisted legs carry cost_basis_eur per leg ─────────────
def test_cash_floor_legs_persist_cost_basis_eur_per_leg():
    # Same shape as the materiality test above but AAPL carries real cost
    # basis (60_000 on the whole 90_000 position) — the pro-rata trim
    # fraction (7_200 / 90_000 = 0.08) must scale the leg's cost_basis_eur
    # the same way it scales trim_value_eur, so a mixed-basis row (some legs
    # real, some conservative) is auditable per leg, not just via the row's
    # single top-level `cost_basis_available` flag.
    holdings = _holdings([("AAPL", "EQUITY", 90_000.0, 60_000.0)], cash=2_000.0)  # nav 92_000
    settings = _settings()
    ledger = FakeLedger(monthly_expenses_eur=100.0, last_txn_date=AS_OF)
    eng = AdvisorEngine(redis=object(), settings=settings, wf=FakeWF(holdings), ledger=ledger,
                         risk=FakeRisk(), regime=FakeRegime())
    rows = asyncio.run(eng.evaluate(AS_OF, REPORT_WEEK))

    row = next(r for r in rows if r["rule_id"] == "cash_floor_band")
    legs = row["input_snapshot"]["legs"]
    assert legs == [
        {"symbol": "AAPL", "asset_class": "EQUITY", "trim_value_eur": 7200.0, "cost_basis_eur": 4800.0}
    ]


# ── rule 2's cost gate ───────────────────────
def test_cash_floor_cost_gate_fires_when_cost_clears_the_floor():
    """Real cost basis keeps the taxable gain (and therefore the cost
    fraction) small -> well under the default 10% floor -> the raise still
    proposes, same shortfall/fraction shape as the plain materiality test."""
    holdings = _holdings([("AAPL", "EQUITY", 90_000.0, 88_000.0)], cash=2_000.0)  # nav 92_000
    settings = _settings()  # default ADVISOR_COST_FRACTION_MAX = 0.10
    ledger = FakeLedger(monthly_expenses_eur=100.0, last_txn_date=AS_OF)
    eng = AdvisorEngine(redis=object(), settings=settings, wf=FakeWF(holdings), ledger=ledger,
                         risk=FakeRisk(), regime=FakeRegime())
    rows = asyncio.run(eng.evaluate(AS_OF, REPORT_WEEK))

    row = next(r for r in rows if r["rule_id"] == "cash_floor_band")
    assert row["status"] == "proposed" and row["action"] == "trim"
    assert row["sizing"] == pytest.approx(7_200.0)
    assert row["input_snapshot"]["cost_fraction"] < settings.advisor_cost_fraction_max


def test_cash_floor_cost_gate_suppresses_when_cost_exceeds_the_floor():
    """No cost basis -> SE4's conservative full-trim-value-as-gain fallback
    -> tax alone (25%) blows through the default 10% cost-fraction floor
    -> the rule does NOT propose, and states the arithmetic instead (the
    's €32-venue-cost-on-€100-raise case, generalised)."""
    holdings = _holdings([("AAPL", "EQUITY", 90_000.0)], cash=2_000.0)  # nav 92_000, no cost basis
    settings = _settings()  # default ADVISOR_COST_FRACTION_MAX = 0.10
    ledger = FakeLedger(monthly_expenses_eur=100.0, last_txn_date=AS_OF)
    eng = AdvisorEngine(redis=object(), settings=settings, wf=FakeWF(holdings), ledger=ledger,
                         risk=FakeRisk(), regime=FakeRegime())
    rows = asyncio.run(eng.evaluate(AS_OF, REPORT_WEEK))

    row = next(r for r in rows if r["rule_id"] == "cash_floor_band")
    assert row["status"] == "evaluated" and row["action"] == "hold"
    assert row["sizing"] is None
    assert "cash-floor raise suppressed" in row["note"]
    snap = row["input_snapshot"]
    assert snap["cost_fraction"] > settings.advisor_cost_fraction_max
    assert snap["cost_fraction_max"] == settings.advisor_cost_fraction_max
    cost = row["tax_cost_arithmetic"]
    assert cost["cost_eur"] == pytest.approx(7_200.0 * 0.25 + 1.0)  # tax + €1 flat EQUITY venue cost


def test_cash_floor_per_leg_materiality_excludes_small_legs_and_states_residual():
    """A pro-rata trim can spread across many small legs — a sub-€50 leg and
    a sub-cent leg must not be proposed on their own; the row sizes to what
    survives (`raise_eur`) and states the unmet gap as `residual_eur`, never
    silently dropping it."""
    holdings = _holdings(
        [("AAPL", "EQUITY", 90_000.0), ("PENNY", "EQUITY", 900.0), ("DUST", "EQUITY", 0.01)],
        cash=9_100.0,
    )  # nav ~100_000.01; neutral 10% band -> shortfall ~900 -> fraction ~0.0099
    settings = _settings(advisor_cost_fraction_max=1.0)  # isolate materiality from the cost gate
    ledger = FakeLedger(monthly_expenses_eur=100.0, last_txn_date=AS_OF)
    eng = AdvisorEngine(redis=object(), settings=settings, wf=FakeWF(holdings), ledger=ledger,
                         risk=FakeRisk(), regime=FakeRegime())
    rows = asyncio.run(eng.evaluate(AS_OF, REPORT_WEEK))

    row = next(r for r in rows if r["rule_id"] == "cash_floor_band")
    snap = row["input_snapshot"]
    assert {leg["symbol"] for leg in snap["legs"]} == {"AAPL"}  # PENNY (sub-€50) + DUST (sub-cent) dropped
    assert snap["n_legs_dropped_sub_materiality"] == 2
    assert snap["raise_eur"] < snap["shortfall_eur"]
    assert snap["residual_eur"] == pytest.approx(snap["shortfall_eur"] - snap["raise_eur"], abs=0.05)
    assert snap["residual_eur"] > 0
    assert row["status"] == "proposed" and row["action"] == "trim"
    assert row["sizing"] == pytest.approx(snap["raise_eur"])
    assert "leg(s) below the per-leg materiality floor excluded" in row["note"]


def test_cash_floor_all_legs_sub_materiality_holds_with_no_legs():
    """Every candidate leg below the per-leg floor -> nothing achievable at
    all -> `hold`, same posture as the pre-existing "no investable positions"
    case, not a silently-empty proposal."""
    holdings = _holdings([("PENNY", "EQUITY", 40.0)], cash=0.0)  # nav 40; the only leg is sub-materiality
    # regardless of the pro-rata fraction (holding value itself < materiality)
    settings = _settings(advisor_cost_fraction_max=1.0)
    ledger = FakeLedger(monthly_expenses_eur=100.0, last_txn_date=AS_OF)
    eng = AdvisorEngine(redis=object(), settings=settings, wf=FakeWF(holdings), ledger=ledger,
                         risk=FakeRisk(), regime=FakeRegime())
    rows = asyncio.run(eng.evaluate(AS_OF, REPORT_WEEK))

    row = next(r for r in rows if r["rule_id"] == "cash_floor_band")
    assert row["status"] == "evaluated" and row["action"] == "hold"
    assert row["input_snapshot"]["legs"] == []
    assert "nothing achievable this week" in row["note"]


# ── rule 2: WF-only liquid measure (design decision — reverts an earlier change) ──
def test_cash_floor_liquid_measure_is_wf_cash_plus_cash_equivalents_only():
    """Ledger bank cash is NOT part of the liquid measure any more (reverts
    the earlier B2 fix, which folded it in) — only WF cash + cash-equivalent
    holdings (stablecoins, ADVISOR_CASH_EQUIVALENT_SYMBOLS) count. USDC here
    joins liquid and is excluded from rule 1's weight-band evaluation
    entirely, collapsing into one summary row instead of a per-symbol one.
    Also covers: multiplier-0 (the default) makes zero ledger calls, and the
    rule targets the regime band only (no life-floor leg to compare against)."""
    holdings = _holdings(
        [("AAPL", "EQUITY", 88_500.0), ("USDC", "CRYPTO", 1_500.0)], cash=2_000.0,
    )  # nav 92_000
    settings = _settings()  # ADVISOR_CASH_EQUIVALENT_SYMBOLS default includes USDC; multiplier default 0
    ledger = FakeLedger(monthly_expenses_eur=100.0, last_txn_date=AS_OF)
    eng = AdvisorEngine(redis=object(), settings=settings, wf=FakeWF(holdings), ledger=ledger,
                         risk=FakeRisk(), regime=FakeRegime())
    rows = asyncio.run(eng.evaluate(AS_OF, REPORT_WEEK))

    assert ledger.calls == []  # multiplier 0 -> zero ledger touches at all

    cash_row = next(r for r in rows if r["rule_id"] == "cash_floor_band")
    snap = cash_row["input_snapshot"]
    assert snap["cash_wf_eur"] == 2_000.0
    assert snap["cash_equivalents_eur"] == 1_500.0
    assert snap["liquid_eur"] == 3_500.0  # WF cash + USDC, NOT ledger cash
    assert "cash_ledger_eur" not in snap  # the reverted key is gone entirely
    assert snap["life_floor_active"] is False
    assert snap["life_floor_eur"] is None
    assert snap["target_liquid_eur"] == snap["regime_band_eur"]  # band-only targeting
    assert "life floor inactive (multiplier 0)" in cash_row["note"]

    # USDC gets ONE summary row in rule 1's output, never a per-symbol row.
    wbt_rows = [r for r in rows if r["rule_id"] == "weight_band_trim"]
    assert "USDC" not in {r["input_snapshot"].get("symbol") for r in wbt_rows}
    ce_summary = next(r for r in wbt_rows if "cash-equivalents" in (r["note"] or ""))
    assert ce_summary["input_snapshot"]["cash_equivalent_symbols"] == ["USDC"]
    assert ce_summary["input_snapshot"]["cash_equivalents_eur"] == 1_500.0
    assert "material" not in ce_summary["input_snapshot"]  # never materiality-evaluated


def test_stablecoin_excluded_from_sell_gate_weight_vector():
    """A cash-equivalent holding must never appear in the weight vector fed
    to `RiskEngine.recompute_with_weights` — the engine imputes any leftover
    (not in the vector) as cash, so excluding it here has exactly the same
    effect as counting it as cash there."""
    holdings = _holdings(
        [("BTC", "CRYPTO", 70_000.0), ("USDC", "CRYPTO", 5_000.0)], cash=25_000.0,
    )  # BTC 70% > 65% crypto band -> fires a sell-gate call
    settings = _settings()
    risk = FakeRisk()
    eng = AdvisorEngine(redis=object(), settings=settings, wf=FakeWF(holdings), ledger=FakeLedger(),
                         risk=risk, regime=FakeRegime())
    asyncio.run(eng.evaluate(AS_OF, REPORT_WEEK))

    assert risk.calls  # rule 1 fired (BTC breach) and used the fake risk engine
    for weights in risk.calls:
        assert "USDC" not in weights


def test_life_floor_dormant_by_default_makes_zero_ledger_calls():
    """The dormant default (multiplier 0): even a stale/nonzero clearing
    balance must NOT refuse — the guard never runs at all when there's no
    ledger-derived floor to protect."""
    holdings = _holdings([("AAPL", "EQUITY", 10_000.0)], cash=5_000.0)
    settings = _settings(advisor_clearing_accounts="Assets:Clearing")
    assert settings.advisor_life_floor_multiplier == 0.0  # the new default
    ledger = FakeLedger(
        monthly_expenses_eur=999.0, last_txn_date=AS_OF, clearing_balances={"Assets:Clearing": 123.0},
    )
    eng = AdvisorEngine(redis=object(), settings=settings, wf=FakeWF(holdings), ledger=ledger,
                         risk=FakeRisk(), regime=FakeRegime())
    rows = asyncio.run(eng.evaluate(AS_OF, REPORT_WEEK))

    assert ledger.calls == []  # expenses BQL, staleness, AND the clearing guard all skipped
    cash_row = next(r for r in rows if r["rule_id"] == "cash_floor_band")
    assert cash_row["action"] != "refuse"  # the guard never ran despite a nonzero balance
    snap = cash_row["input_snapshot"]
    assert snap["avg_monthly_expenses_eur"] is None
    assert snap["ledger_staleness_days"] is None


def test_life_floor_active_when_multiplier_positive_and_dominates_band():
    """Setting the multiplier > 0 re-enables the whole leg (expenses BQL,
    staleness, clearing guard) with no code change, exactly as before this
    follow-up — and, unlike every other fixture in this file (all
    "floor << band"), this one has the floor actually DOMINATE the band, so
    the multiplier>0 arithmetic path is genuinely exercised, not just
    reachable."""
    holdings = _holdings([("AAPL", "EQUITY", 90_000.0)], cash=2_000.0)  # nav 92_000, band 9_200 (10%)
    settings = _settings(advisor_life_floor_multiplier=6.0)
    ledger = FakeLedger(monthly_expenses_eur=5_000.0, last_txn_date=AS_OF)  # floor 30_000 >> band 9_200
    eng = AdvisorEngine(redis=object(), settings=settings, wf=FakeWF(holdings), ledger=ledger,
                         risk=FakeRisk(), regime=FakeRegime())
    rows = asyncio.run(eng.evaluate(AS_OF, REPORT_WEEK))

    cash_row = next(r for r in rows if r["rule_id"] == "cash_floor_band")
    snap = cash_row["input_snapshot"]
    assert snap["life_floor_active"] is True
    assert snap["life_floor_eur"] == pytest.approx(30_000.0)
    assert snap["target_liquid_eur"] == pytest.approx(30_000.0)  # floor dominates band
    assert snap["avg_monthly_expenses_eur"] == pytest.approx(5_000.0)
    assert snap["ledger_staleness_days"] == 0
    assert ledger.calls  # expenses/staleness BQL DID fire
    assert "life floor inactive" not in cash_row["note"]


# ── rule 1: unclassified asset class still gets an audit row (latent finding) ──
def test_weight_band_trim_unclassified_asset_class_emits_hold_row_not_silently_skipped():
    holdings = _holdings(
        [("XYZ", "EXOTIC", 5_000.0), ("AAPL", "EQUITY", 5_000.0)], cash=90_000.0,
    )  # "EXOTIC" is not in ADVISOR_WEIGHT_BANDS at all
    settings = _settings()
    ledger = FakeLedger(monthly_expenses_eur=100.0, last_txn_date=AS_OF)
    rows = asyncio.run(_engine(holdings, settings, ledger=ledger).evaluate(AS_OF, REPORT_WEEK))

    trim_rows = {r["input_snapshot"]["symbol"]: r for r in rows if r["rule_id"] == "weight_band_trim"}
    assert "XYZ" in trim_rows  # used to be silently `continue`d — no row at all
    row = trim_rows["XYZ"]
    assert row["status"] == "evaluated" and row["action"] == "hold"
    assert "unclassified asset class EXOTIC" in row["note"]
    assert row["input_snapshot"]["asset_class"] == "EXOTIC"
    # never stamped `material` — materiality was never evaluated for this row
    # (the material-stamping invariant)
    assert "material" not in row["input_snapshot"]


# ── cooldown is scoped per (rule_id, symbol), not per rule ───────────────
def test_cooldown_suppresses_only_the_overridden_symbol():
    holdings = _holdings(
        [("BTC", "CRYPTO", 66_000.0), ("AAPL", "EQUITY", 33_000.0)], cash=1_000.0,
    )  # nav 100_000: BTC 66% (> 65% band), AAPL 33% (> 30% band) — both breach
    settings = _settings(
        advisor_expected_return_map="CRYPTO:0.0,EQUITY:0.0,CASH:0.0",
        advisor_venue_cost_map="CRYPTO:flat:0.0,EQUITY:flat:0.0",
    )
    ledger = FakeLedger(monthly_expenses_eur=100.0, last_txn_date=AS_OF)

    baseline = asyncio.run(_engine(holdings, settings, ledger=ledger).evaluate(AS_OF, REPORT_WEEK))
    trims = {r["input_snapshot"]["symbol"]: r for r in baseline if r["rule_id"] == "weight_band_trim"}
    assert trims["BTC"]["status"] == "proposed" and trims["AAPL"]["status"] == "proposed"

    overrides = {"weight_band_trim:BTC": (REPORT_WEEK, trims["BTC"]["tax_cost_arithmetic"]["benefit_eur"])}
    rows2 = asyncio.run(_engine(holdings, settings, ledger=ledger).evaluate(AS_OF, REPORT_WEEK, overrides=overrides))
    trims2 = {r["input_snapshot"]["symbol"]: r for r in rows2 if r["rule_id"] == "weight_band_trim"}

    assert trims2["BTC"]["status"] == "evaluated"
    assert "cooldown" in trims2["BTC"]["note"]
    assert trims2["AAPL"]["status"] == "proposed"  # a different symbol's override must not suppress this


# ── SE4: gain-trigger goes live — real cost_basis in the tax leg ────────
def _basic_engine(settings: Settings | None = None) -> AdvisorEngine:
    return _engine(_holdings([], 0), settings or _settings())


def test_tax_and_venue_uses_real_cost_basis_when_present():
    settings = _settings(advisor_tax_rate=0.25, advisor_loss_offset_factor=0.0,
                          advisor_venue_cost_map="CRYPTO:flat:0.0")
    eng = _basic_engine(settings)
    trimmed = [{"symbol": "BTC", "asset_class": "CRYPTO", "trim_value_eur": 1000.0, "cost_basis_eur": 600.0}]
    cost = eng._tax_and_venue(trimmed)  # noqa: SLF001
    assert cost["cost_basis_available"] is True
    assert cost["tax_eur"] == pytest.approx((1000.0 - 600.0) * 0.25)  # real gain, not the full trim value


def test_tax_and_venue_floors_gain_at_zero_for_a_loss_leg():
    settings = _settings(advisor_tax_rate=0.25, advisor_loss_offset_factor=0.0,
                          advisor_venue_cost_map="EQUITY:flat:0.0")
    eng = _basic_engine(settings)
    # cost_basis_eur > trim_value_eur: this leg is trimmed at an unrealised loss.
    trimmed = [{"symbol": "AAPL", "asset_class": "EQUITY", "trim_value_eur": 400.0, "cost_basis_eur": 900.0}]
    cost = eng._tax_and_venue(trimmed)  # noqa: SLF001
    assert cost["cost_basis_available"] is True
    assert cost["tax_eur"] == 0.0  # floored, never negative


def test_tax_and_venue_mixed_legs_marks_cost_basis_available_false():
    """One leg with real basis, one without: the tax figure is still computed
    per-leg (real gain for the leg that has it, conservative full value for
    the one that doesn't) — but the top-level flag stays False since the
    total isn't fully live (absent-tolerant per leg, not all-or-nothing)."""
    settings = _settings(advisor_tax_rate=0.25, advisor_loss_offset_factor=0.0,
                          advisor_venue_cost_map="CRYPTO:flat:0.0,EQUITY:flat:0.0")
    eng = _basic_engine(settings)
    trimmed = [
        {"symbol": "BTC", "asset_class": "CRYPTO", "trim_value_eur": 1000.0, "cost_basis_eur": 600.0},
        {"symbol": "AAPL", "asset_class": "EQUITY", "trim_value_eur": 500.0},  # no basis
    ]
    cost = eng._tax_and_venue(trimmed)  # noqa: SLF001
    assert cost["cost_basis_available"] is False
    expected_gain = (1000.0 - 600.0) + 500.0  # real gain + conservative full value
    assert cost["tax_eur"] == pytest.approx(expected_gain * 0.25)


def test_weight_band_trim_row_carries_live_cost_basis_arithmetic():
    # BTC at 66% of a 100k nav (> the 65% crypto band) -> trims to the edge,
    # trim_value = 1_000. cost_basis (33_000 on the whole 66_000 position)
    # scales proportionally: 33_000 * (1_000 / 66_000) ~= 500.
    holdings = _holdings([("BTC", "CRYPTO", 66_000.0, 33_000.0)], cash=34_000.0)
    settings = _settings(
        advisor_expected_return_map="CRYPTO:0.0,CASH:0.0", advisor_venue_cost_map="CRYPTO:flat:0.0",
    )
    ledger = FakeLedger(monthly_expenses_eur=100.0, last_txn_date=AS_OF)
    rows = asyncio.run(_engine(holdings, settings, ledger=ledger).evaluate(AS_OF, REPORT_WEEK))
    row = next(r for r in rows if r["rule_id"] == "weight_band_trim")
    cost = row["tax_cost_arithmetic"]
    assert cost["cost_basis_available"] is True
    expected_gain = 1_000.0 - (33_000.0 * (1_000.0 / 66_000.0))
    assert cost["tax_eur"] == pytest.approx(expected_gain * 0.25, rel=1e-6)
