"""Tests for `services/decisions.py`.

Pure-helper tests need no I/O. `chapter_decisions` is exercised against a
fake `session_scope` (mirrors `test_recommendation_lifecycle.py`'s /
`test_reports_router.py`'s convention — no live Postgres/Redis needed) plus
a fake `make_wealthfolio`/`RiskEngine` pair standing in for the ONE live
holdings fetch + ONE `bundle()` call `risk_budget_impact` needs.

The demo-leak tests for `GET /reports/{week}/decisions` live HERE (not in
`tests/test_reports_router.py`, which is outside this item's touched-file
fence) even though they extend that file's existing pattern — same
technique (`_boom_session_scope`, `RiskEngine`-must-never-construct),
against `app.routers.reports.get_report_decisions` directly.
"""
from __future__ import annotations

import contextlib
from datetime import date, datetime, timezone
from typing import Any

import pytest

import app.routers.reports as reports_module
import app.services.decisions as decisions_module
from app.config import Settings
from app.models import PortfolioSnapshot, Recommendation
from app.services.decisions import DecisionProjection, _funding_source, _invalidation, _legs_for, _lifecycle_chain


def _settings(**kw: Any) -> Settings:
    kw.setdefault("demo_mode", False)
    return Settings(
        postgres_user="u", postgres_password="p", postgres_db="d", redis_password="r", **kw,
    )


def _rec(**kw: Any) -> Recommendation:
    base: dict[str, Any] = dict(
        id=1, rule_id="weight_band_trim", as_of=date(2026, 8, 31), report_week="2026-W36",
        action="trim", sizing=None, input_snapshot={}, regime_component_set_id=None,
        status="evaluated", tax_cost_arithmetic=None, note="n", prior_id=None,
        created_at=datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc),
    )
    base.update(kw)
    return Recommendation(**base)


# ── pure helpers ─────────────────────────────────────────────────────────
def test_funding_source_no_fire_row_is_kind_none():
    rec = _rec(sizing=None, status="evaluated")
    assert _funding_source(rec, cash_eur=1000.0, clearing_account="unspecified") == {
        "kind": "none", "reason": "row status 'evaluated' — no action, nothing to fund",
    }


def test_funding_source_dca_is_clearing_cash_no_symbol():
    rec = _rec(
        rule_id="dca_modulation", action="buy", status="proposed", sizing=400.0,
        input_snapshot={"contribution_eur": 400.0},
    )
    fs = _funding_source(rec, cash_eur=2000.0, clearing_account="Assets:Clearing:IBKR")
    assert fs["kind"] == "clearing_cash"
    assert fs["account"] == "Assets:Clearing:IBKR"
    assert fs["amount_eur"] == 400.0
    assert fs["available_eur"] == 2000.0
    assert fs["covers"] is True
    assert "symbol" not in fs and "symbols" not in fs


def test_funding_source_weight_band_trim_is_proceeds_with_lots_parked():
    rec = _rec(
        rule_id="weight_band_trim", status="proposed", sizing=1500.0,
        input_snapshot={"symbol": "AAA", "trim_value_eur": 1500.0, "band_upper": 0.2},
    )
    fs = _funding_source(rec, cash_eur=0.0, clearing_account="unspecified")
    assert fs == {
        "kind": "proceeds", "symbols": ["AAA"], "est_proceeds_eur": 1500.0,
        "to": "cash", "lots": "not modelled (Parked)",
    }


def test_funding_source_cash_floor_band_sums_legs():
    rec = _rec(
        rule_id="cash_floor_band", status="proposed", sizing=300.0,
        input_snapshot={"legs": [{"symbol": "AAA", "trim_value_eur": 200.0}, {"symbol": "BBB", "trim_value_eur": 100.0}]},
    )
    fs = _funding_source(rec, cash_eur=0.0, clearing_account="unspecified")
    assert fs["kind"] == "proceeds"
    assert sorted(fs["symbols"]) == ["AAA", "BBB"]
    assert fs["est_proceeds_eur"] == 300.0
    assert fs["lots"] == "not modelled (Parked)"


def test_funding_source_unknown_rule_id():
    rec = _rec(rule_id="mystery_rule", status="proposed", sizing=1.0, input_snapshot={})
    fs = _funding_source(rec, cash_eur=0.0, clearing_account="unspecified")
    assert fs["kind"] == "unknown"
    assert "mystery_rule" in fs["reason"]


def test_invalidation_weight_band_trim_still_valid_when_outside_band():
    rec = _rec(
        rule_id="weight_band_trim", input_snapshot={"symbol": "AAA", "band_upper": 0.2},
    )
    inv = _invalidation(rec, value_by_symbol={"AAA": 2400.0}, nav=10000.0, cash=0.0)
    assert inv["still_valid"] is True
    assert inv["current_weight"] == pytest.approx(0.24)
    assert "AAA" in inv["text"] and "[0," in inv["text"]


def test_invalidation_weight_band_trim_invalidated_once_back_inside_band():
    rec = _rec(rule_id="weight_band_trim", input_snapshot={"symbol": "BBB", "band_upper": 0.2})
    inv = _invalidation(rec, value_by_symbol={"BBB": 500.0}, nav=10000.0, cash=0.0)
    assert inv["still_valid"] is False
    assert inv["current_weight"] == pytest.approx(0.05)


def test_invalidation_cash_floor_band():
    rec = _rec(rule_id="cash_floor_band", input_snapshot={"regime_band_pct": 0.1, "cash_equivalents_eur": 0.0})
    inv = _invalidation(rec, value_by_symbol={}, nav=10000.0, cash=500.0)
    assert inv["still_valid"] is True  # 500/10000 = 0.05 < 0.1 band
    assert inv["current"] == pytest.approx(0.05)


def test_invalidation_cash_floor_band_refuse_row_has_no_band():
    rec = _rec(rule_id="cash_floor_band", action="refuse", input_snapshot={"clearing_sum_eur": 5.0})
    inv = _invalidation(rec, value_by_symbol={}, nav=10000.0, cash=500.0)
    assert inv["still_valid"] is None


def test_invalidation_dca_modulation_still_valid_unless_executed():
    proposed = _rec(rule_id="dca_modulation", status="proposed", input_snapshot={})
    executed = _rec(rule_id="dca_modulation", status="executed", input_snapshot={})
    assert _invalidation(proposed, value_by_symbol={}, nav=1.0, cash=0.0)["still_valid"] is True
    assert _invalidation(executed, value_by_symbol={}, nav=1.0, cash=0.0)["still_valid"] is False
    assert _invalidation(proposed, value_by_symbol={}, nav=1.0, cash=0.0)["text"] == "contribution already made this period"


def test_invalidation_unknown_rule():
    rec = _rec(rule_id="mystery_rule", input_snapshot={})
    inv = _invalidation(rec, value_by_symbol={}, nav=1.0, cash=0.0)
    assert inv["still_valid"] is None
    assert "mystery_rule" in inv["text"]


def test_legs_for_no_fire_is_empty():
    assert _legs_for(_rec(sizing=None)) == []


def test_legs_for_dca_has_no_symbol_leg():
    rec = _rec(rule_id="dca_modulation", sizing=400.0, input_snapshot={"contribution_eur": 400.0})
    assert _legs_for(rec) == []


def test_legs_for_weight_band_trim():
    rec = _rec(sizing=1500.0, input_snapshot={"symbol": "AAA", "trim_value_eur": 1500.0})
    assert _legs_for(rec) == [("AAA", 1500.0)]


def test_legs_for_cash_floor_band():
    rec = _rec(
        rule_id="cash_floor_band", sizing=300.0,
        input_snapshot={"legs": [{"symbol": "AAA", "trim_value_eur": 200.0}, {"symbol": "BBB", "trim_value_eur": 100.0}]},
    )
    assert _legs_for(rec) == [("AAA", 200.0), ("BBB", 100.0)]


def test_lifecycle_chain_root_to_leaf_order():
    root = _rec(id=10, status="proposed", input_snapshot={})
    leaf = _rec(id=11, status="accepted", prior_id=10, input_snapshot={"human_note": "ok", "human_action": None})
    chain = _lifecycle_chain(leaf, {10: root, 11: leaf})
    assert [c["id"] for c in chain] == [10, 11]
    assert [c["status"] for c in chain] == ["proposed", "accepted"]
    assert chain[1]["human_note"] == "ok"


# ── chapter_decisions (fake session_scope + fake risk/holdings) ──────────
class _SeqResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self) -> "_SeqResult":
        return self

    def all(self) -> list[Any]:
        return self._rows


class _SeqDB:
    def __init__(self, results: list[_SeqResult]) -> None:
        self._results = list(results)

    async def execute(self, stmt: object) -> _SeqResult:
        return self._results.pop(0)


def _wire_session(monkeypatch, rec_rows: list[Recommendation], snap_rows: list[PortfolioSnapshot]) -> None:
    db = _SeqDB([_SeqResult(rec_rows), _SeqResult(snap_rows)])

    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        yield db

    monkeypatch.setattr(decisions_module, "session_scope", _fake_session_scope)


class _FakeAsset:
    def __init__(self, symbol: str, value: float) -> None:
        self.symbol = symbol
        self.value = value


class _FakeHoldings:
    def __init__(self, assets: list[_FakeAsset], total: float) -> None:
        self.by_asset = assets
        self.total = total


class _FakeWF:
    def __init__(self, holdings: _FakeHoldings) -> None:
        self._h = holdings

    async def holdings(self) -> _FakeHoldings:
        return self._h


def _wire_risk(monkeypatch, bundle_calls: list[int], recompute_calls: list[dict[str, float]]) -> None:
    holdings = _FakeHoldings([_FakeAsset("AAA", 3000.0), _FakeAsset("BBB", 500.0)], 10000.0)
    monkeypatch.setattr(decisions_module, "make_wealthfolio", lambda redis, settings: _FakeWF(holdings))

    class _FakeRiskEngine:
        def __init__(self, redis, settings) -> None:
            pass

        async def bundle(self) -> dict[str, Any]:
            bundle_calls.append(1)
            return {}

        async def recompute_with_weights(self, weights: dict[str, float], frequency: str = "weekly"):
            recompute_calls.append(dict(weights))
            from types import SimpleNamespace
            cvar = 5.0 - weights.get("AAA", 0.0) * 10.0  # lower AAA weight -> lower CVaR
            return SimpleNamespace(cvar_pct=cvar), {}

    monkeypatch.setattr(decisions_module, "RiskEngine", _FakeRiskEngine)


async def test_chapter_decisions_fixture_three_rows(monkeypatch):
    trim = _rec(
        id=1, rule_id="weight_band_trim", action="trim", status="proposed", sizing=1500.0,
        input_snapshot={"symbol": "AAA", "trim_value_eur": 1500.0, "band_upper": 0.2},
        created_at=datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc),
    )
    dca_root = _rec(
        id=2, rule_id="dca_modulation", action="buy", status="proposed", sizing=400.0,
        input_snapshot={"contribution_eur": 400.0}, prior_id=None,
        created_at=datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc),
    )
    dca_leaf = _rec(
        id=3, rule_id="dca_modulation", action="buy", status="accepted", sizing=400.0,
        input_snapshot={"contribution_eur": 400.0, "human_note": "ok"}, prior_id=2,
        created_at=datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc),
    )
    no_fire = _rec(
        id=4, rule_id="weight_band_trim", action="hold", status="evaluated", sizing=None,
        input_snapshot={"symbol": "BBB", "band_upper": 0.2},
        created_at=datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc),
    )
    rec_rows = [trim, dca_root, dca_leaf, no_fire]
    snap_rows = [
        PortfolioSnapshot(
            id=1, report_week="2026-W36", date=date(2026, 8, 31), symbol="AAA", qty=1.0,
            value_eur=2400.0, cash=2000.0, nav=10000.0,
        ),
        PortfolioSnapshot(
            id=2, report_week="2026-W36", date=date(2026, 8, 31), symbol="BBB", qty=1.0,
            value_eur=500.0, cash=2000.0, nav=10000.0,
        ),
    ]
    _wire_session(monkeypatch, rec_rows, snap_rows)
    bundle_calls: list[int] = []
    recompute_calls: list[dict[str, float]] = []
    _wire_risk(monkeypatch, bundle_calls, recompute_calls)

    settings = _settings()
    result = await DecisionProjection(redis=object(), settings=settings).chapter_decisions("2026-W36")

    rows = result["rows"]
    assert len(rows) == 3
    for row in rows:
        for key in ("funding_source", "risk_budget_impact", "invalidation", "review_at", "expires_at", "lifecycle"):
            assert key in row

    by_rule = {(r["rule_id"], r["status"]): r for r in rows}
    dca_row = by_rule[("dca_modulation", "accepted")]
    assert dca_row["funding_source"]["kind"] == "clearing_cash"
    assert dca_row["funding_source"]["amount_eur"] == 400.0
    assert "symbol" not in dca_row["funding_source"] and "symbols" not in dca_row["funding_source"]
    assert len(dca_row["lifecycle"]) == 2  # proposed -> accepted

    trim_row = by_rule[("weight_band_trim", "proposed")]
    assert set(trim_row["risk_budget_impact"]) == {"cvar95_now", "cvar95_post", "delta_pct"}

    no_fire_row = by_rule[("weight_band_trim", "evaluated")]
    assert no_fire_row["risk_budget_impact"] == {
        "unavailable": "row status 'evaluated' — no action, nothing to project",
    }

    assert result["no_action_rate"] == pytest.approx(1 / 3)
    assert result["counts"] == {"proposed": 1, "accepted": 1, "evaluated": 1}

    # F8: exactly one bundle() call per chapter call, regardless of how many
    # actionable rows needed risk figures.
    assert len(bundle_calls) == 1
    # current_weights recomputed once, shared across every row that needs it
    # (memoized by weight-vector signature) — one "current" + one "post" call
    # for the single trim row here.
    assert len(recompute_calls) == 2


async def test_chapter_decisions_symbol_present_on_every_row(monkeypatch):
    """fix 1: `symbol` is a top-level, joinable field on every projected
    row — present (possibly `None`) regardless of rule_id, so a consumer
    never has to re-read the recommendation's `input_snapshot` to join a
    decision to an instrument."""
    trim = _rec(
        id=1, rule_id="weight_band_trim", action="trim", status="proposed", sizing=1500.0,
        input_snapshot={"symbol": "AAA", "trim_value_eur": 1500.0, "band_upper": 0.2},
    )
    cash_floor = _rec(
        id=2, rule_id="cash_floor_band", action="trim", status="proposed", sizing=300.0,
        input_snapshot={"legs": [{"symbol": "AAA", "trim_value_eur": 200.0}], "regime_band_pct": 0.1},
    )
    dca = _rec(
        id=3, rule_id="dca_modulation", action="buy", status="proposed", sizing=400.0,
        input_snapshot={"contribution_eur": 400.0},
    )
    no_fire = _rec(id=4, rule_id="weight_band_trim", action="hold", status="evaluated", sizing=None, input_snapshot={})
    rec_rows = [trim, cash_floor, dca, no_fire]
    snap_rows = [
        PortfolioSnapshot(
            id=1, report_week="2026-W36", date=date(2026, 8, 31), symbol="AAA", qty=1.0,
            value_eur=2400.0, cash=2000.0, nav=10000.0,
        ),
    ]
    _wire_session(monkeypatch, rec_rows, snap_rows)
    _wire_risk(monkeypatch, [], [])

    result = await DecisionProjection(redis=object(), settings=_settings()).chapter_decisions("2026-W36")
    rows = result["rows"]
    assert len(rows) == 4
    for row in rows:
        assert "symbol" in row  # present on every row,
    by_id = {r["id"]: r for r in rows}
    assert by_id[1]["symbol"] == "AAA"  # weight_band_trim: input_snapshot["symbol"] verbatim
    assert by_id[2]["symbol"] is None  # cash_floor_band: multi-leg, exposed via funding_source.symbols instead
    assert by_id[3]["symbol"] is None  # dca_modulation: no symbol
    assert by_id[4]["symbol"] is None  # no-fire row: empty input_snapshot


async def test_chapter_decisions_risk_engine_outage_returns_generic_message_not_raw_exception(monkeypatch, caplog):
    """Security fix (LOW): a `holdings()`/`bundle()` failure used to serve
    the raw `str(exc)` straight through `/reports/{week}/decisions` and the
    pack. It must now log the full detail and return a generic string."""
    trim = _rec(
        id=1, rule_id="weight_band_trim", action="trim", status="proposed", sizing=1500.0,
        input_snapshot={"symbol": "AAA", "trim_value_eur": 1500.0, "band_upper": 0.2},
        created_at=datetime(2026, 8, 31, 9, 0, tzinfo=timezone.utc),
    )
    rec_rows = [trim]
    snap_rows = [
        PortfolioSnapshot(
            id=1, report_week="2026-W36", date=date(2026, 8, 31), symbol="AAA", qty=1.0,
            value_eur=2400.0, cash=2000.0, nav=10000.0,
        ),
    ]
    _wire_session(monkeypatch, rec_rows, snap_rows)

    class _BoomWF:
        async def holdings(self):
            raise RuntimeError("secret internal db dsn leaked here")

    monkeypatch.setattr(decisions_module, "make_wealthfolio", lambda redis, settings: _BoomWF())

    settings = _settings()
    with caplog.at_level("WARNING", logger="catallax.decisions"):
        result = await DecisionProjection(redis=object(), settings=settings).chapter_decisions("2026-W36")

    trim_row = result["rows"][0]
    assert trim_row["risk_budget_impact"] == {"unavailable": "risk engine unavailable"}
    assert "secret internal db dsn" not in str(result)  # never served to the client
    assert "secret internal db dsn" in caplog.text  # but still logged for operators


# ── demo-mode leak guard for GET /reports/{week}/decisions ───────────────
def _boom_session_scope():
    raise AssertionError("demo mode must never open a DB session")


async def test_get_report_decisions_demo_mode_never_touches_db_or_risk_engine(monkeypatch):
    monkeypatch.setattr(reports_module, "session_scope", _boom_session_scope)

    class _BoomDecisionProjection:
        def __init__(self, *a, **kw) -> None:
            raise AssertionError("demo mode must never construct DecisionProjection")

    monkeypatch.setattr(reports_module, "DecisionProjection", _BoomDecisionProjection)
    settings = _settings(demo_mode=True)

    result = await reports_module.get_report_decisions("latest", redis=object(), settings=settings)
    assert result["engine"] == "decision-projection-v1"
    assert len(result["rows"]) == 3
