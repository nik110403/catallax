"""Tests for `services/cockpit_tools.py`.

Every handler wraps an existing service — tested here against fakes
monkeypatched onto the exact names `cockpit_tools`/its lazily-imported
neighbours resolve (mirrors `test_decisions.py`'s `_wire_session`
convention), never a live Postgres/Redis. Two exceptions genuinely touch a
DB session: `journal_entries`/`notes`, which read `JournalEntry`/`Note`
directly (the journal and notes services are not depended on here)
— exercised the same fake-`session_scope` way as `test_decisions.py`.
"""
from __future__ import annotations

import contextlib
import inspect
import re
from datetime import date, datetime, timezone
from types import SimpleNamespace
from typing import Any

import app.routers.forecasts as forecasts_router_module
import app.routers.reports as reports_module
import app.services.cockpit_tools as cockpit_tools
from app.config import Settings
from app.errors import NotFound
from app.models import JournalEntry, Note
from app.services.cockpit_tools import TOOLS, ToolResult, call, schemas
from app.services.markets import MarketComposite, MarketGroup, MarketQuote, MarketsOverview
from app.services.portfolio import Holding


def _settings(**kw: Any) -> Settings:
    kw.setdefault("demo_mode", False)
    return Settings(postgres_user="u", postgres_password="p", postgres_db="d", redis_password="r", **kw)


def _fake_session_scope(db: Any):
    @contextlib.asynccontextmanager
    async def _scope():
        yield db
    return _scope


# ── registry shape ──────────────────────────────────────────────────────
_EXPECTED_TOOL_NAMES = {
    "portfolio_exposure", "risk_metrics", "market_regime", "recommendations",
    "forecasts", "report_packs", "journal_entries", "notes", "provenance",
}


def test_registry_has_exactly_the_spec_tools():
    assert set(TOOLS) == _EXPECTED_TOOL_NAMES


def test_schemas_are_valid_json_schema_fragments():
    defs = schemas()
    assert len(defs) == len(TOOLS)
    for d in defs:
        assert set(d) == {"name", "description", "input_schema"}
        schema = d["input_schema"]
        assert schema["type"] == "object"
        assert isinstance(schema["properties"], dict)
        for prop_name, prop in schema["properties"].items():
            assert "type" in prop or "enum" in prop, f"{d['name']}.{prop_name} has no type/enum"
        for req in schema.get("required", []):
            assert req in schema["properties"]


def test_call_unknown_tool_raises_not_found():
    async def _run():
        try:
            await call("does_not_exist", {}, settings=_settings())
            raise AssertionError("expected NotFound")
        except NotFound as exc:
            assert "does_not_exist" in str(exc)
    import asyncio
    asyncio.run(_run())


# ── invariant 1: read-only, no write path anywhere in the module ────
def test_module_source_has_no_write_path():
    src = inspect.getsource(cockpit_tools)
    assert re.search(r"session\.add|\.commit\(\)|INSERT|UPDATE|DELETE", src) is None


# ── invariant 2 (record_ids) + 3 (bounded/truncated): portfolio_exposure ──
async def test_portfolio_exposure_caps_rows_and_sets_note_when_no_ids(monkeypatch):
    holdings = [
        Holding(symbol=f"SYM{i}", asset_class="equity", quantity=1, cost=1, value=1,
                pnl=0, pnl_pct=0.0, weight=1.0, currency="EUR")
        for i in range(5)
    ]

    class _FakeSvc:
        def __init__(self, redis, settings) -> None:
            pass

        async def holdings(self) -> list[Holding]:
            return holdings

    monkeypatch.setattr(cockpit_tools, "PortfolioService", _FakeSvc)
    monkeypatch.setattr(cockpit_tools, "get_redis", lambda: object())

    result = await call("portfolio_exposure", {"limit": 2}, settings=_settings())
    assert isinstance(result, ToolResult)
    assert len(result.rows) == 2
    assert result.truncated is True
    assert result.record_ids == []
    assert result.note  # explicit note when no ids (invariant 2)


# ── risk_metrics ───────────────────────────────────────────────────────
async def test_risk_metrics_combines_var_stress_concentration(monkeypatch):
    var_report = SimpleNamespace(
        as_of="2026-09-01", confidence=0.95, var_eur=100.0, var_pct=1.0,
        cvar_eur=150.0, cvar_pct=1.5, var99_eur=200.0, cvar99_eur=250.0,
    )
    stress_report = SimpleNamespace(scenarios=[
        SimpleNamespace(name="2020 COVID crash", portfolio_return_pct=-20.0, portfolio_pnl_eur=-2000.0),
    ])
    xray_report = SimpleNamespace(risk_contributors=[
        SimpleNamespace(symbol="AAA", contribution_pct=40.0, weight_pct=30.0),
    ])

    class _FakeEngine:
        def __init__(self, redis, settings) -> None:
            pass

        async def var(self):
            return var_report, {}

        async def stress(self):
            return stress_report

        async def xray(self):
            return xray_report, {}

    monkeypatch.setattr(cockpit_tools, "RiskEngine", _FakeEngine)
    monkeypatch.setattr(cockpit_tools, "get_redis", lambda: object())

    result = await call("risk_metrics", {}, settings=_settings())
    kinds = {r["kind"] for r in result.rows}
    assert kinds == {"var", "stress", "concentration"}
    assert result.record_ids == []
    assert result.truncated is False


# ── market_regime ──────────────────────────────────────────────────────
async def test_market_regime_returns_composite_and_components(monkeypatch):
    overview = MarketsOverview(
        as_of=datetime(2026, 9, 1, tzinfo=timezone.utc),
        groups=[MarketGroup(key="indices", label="Indices", items=[
            MarketQuote(symbol="SPX", label="S&P 500", price="5000", change="+10", change_pct=0.2, currency="USD"),
        ])],
        composite=MarketComposite(score=0.2, label="Risk-On", advancers=1, decliners=0, total=1),
    )

    async def _fake_get_overview(redis, settings):
        return overview

    monkeypatch.setattr(cockpit_tools, "get_overview", _fake_get_overview)
    monkeypatch.setattr(cockpit_tools, "get_redis", lambda: object())

    result = await call("market_regime", {}, settings=_settings())
    kinds = [r["kind"] for r in result.rows]
    assert kinds == ["composite", "component"]
    assert result.rows[0]["label"] == "Risk-On"
    assert result.record_ids == []


# ── recommendations ────────────────────────────────────────────────────
async def test_recommendations_real_mode_resolves_latest_and_projects(monkeypatch):
    row = SimpleNamespace(report_week="2026-W36")

    async def _fake_resolve(db, week):
        assert week == "latest"
        return row

    class _FakeProjection:
        def __init__(self, redis, settings) -> None:
            pass

        async def chapter_decisions(self, report_week: str) -> dict[str, Any]:
            assert report_week == "2026-W36"
            return {"rows": [{"id": 1, "rule_id": "r", "status": "proposed"}]}

    monkeypatch.setattr(reports_module, "_resolve_pack_row", _fake_resolve)
    monkeypatch.setattr(cockpit_tools, "DecisionProjection", _FakeProjection)
    monkeypatch.setattr(cockpit_tools, "session_scope", _fake_session_scope(object()))
    monkeypatch.setattr(cockpit_tools, "get_redis", lambda: object())

    result = await call("recommendations", {}, settings=_settings())
    assert result.rows == [{"id": 1, "rule_id": "r", "status": "proposed"}]
    assert result.record_ids == [{"kind": "recommendation", "id": 1}]


async def test_recommendations_no_pack_returns_empty_with_note(monkeypatch):
    async def _fake_resolve(db, week):
        return None

    monkeypatch.setattr(reports_module, "_resolve_pack_row", _fake_resolve)
    monkeypatch.setattr(cockpit_tools, "session_scope", _fake_session_scope(object()))

    result = await call("recommendations", {"report_week": "2026-W99"}, settings=_settings())
    assert result.rows == [] and result.record_ids == [] and result.note


async def test_recommendations_demo_mode_uses_demo_decisions(monkeypatch):
    monkeypatch.setattr(reports_module, "_demo_decisions", lambda: {"rows": [{"id": 9, "status": "proposed"}]})

    result = await call("recommendations", {}, settings=_settings(demo_mode=True))
    assert result.rows == [{"id": 9, "status": "proposed"}]
    assert result.record_ids == [{"kind": "recommendation", "id": 9}]


# ── forecasts ───────────────────────────────────────────────────────────
async def test_forecasts_real_mode_lists_and_adds_issuer_stats(monkeypatch):
    class _FakeLedger:
        def __init__(self, redis, settings) -> None:
            pass

        async def list(self, status=None, week=None, issued_by=None, limit=100):
            return [{"id": 1, "status": "pending"}, {"id": 2, "status": "resolved"}]

        async def stats_by_issuer(self, horizon_days: int) -> dict[str, dict[str, Any]]:
            assert horizon_days == 28
            return {"human": {"hits": 1, "n": 1}}

    monkeypatch.setattr(cockpit_tools, "ForecastLedger", _FakeLedger)
    monkeypatch.setattr(cockpit_tools, "get_redis", lambda: object())

    result = await call("forecasts", {}, settings=_settings())
    forecast_rows = [r for r in result.rows if r["kind"] == "forecast"]
    stats_rows = [r for r in result.rows if r["kind"] == "issuer_stats"]
    assert {r["id"] for r in forecast_rows} == {1, 2}
    assert stats_rows == [{"kind": "issuer_stats", "issued_by": "human", "horizon_days": 28, "hits": 1, "n": 1}]
    assert result.record_ids == [{"kind": "forecast", "id": 1}, {"kind": "forecast", "id": 2}]


async def test_forecasts_demo_mode_uses_fixture_sample_never_stats(monkeypatch):
    monkeypatch.setattr(
        forecasts_router_module, "_demo_forecasts_sample",
        lambda: [{"id": 1, "status": "pending"}, {"id": 2, "status": "resolved"}],
    )

    class _BoomLedger:
        def __init__(self, *a, **kw) -> None:
            raise AssertionError("demo mode must never construct ForecastLedger")

    monkeypatch.setattr(cockpit_tools, "ForecastLedger", _BoomLedger)

    result = await call("forecasts", {"status": "pending"}, settings=_settings(demo_mode=True))
    assert [r["id"] for r in result.rows] == [1]
    assert result.note and "demo" in result.note


# ── report_packs ────────────────────────────────────────────────────────
async def test_report_packs_real_mode_lists_weeks_and_section_keys(monkeypatch):
    async def _fake_report_weeks():
        return ["2026-W36", "2026-W35"]

    async def _fake_resolve(db, week):
        assert week == "2026-W36"
        return SimpleNamespace(report_week="2026-W36", sections={"portfolio": {}, "market": {}})

    monkeypatch.setattr(reports_module, "_report_weeks", _fake_report_weeks)
    monkeypatch.setattr(reports_module, "_resolve_pack_row", _fake_resolve)
    monkeypatch.setattr(cockpit_tools, "session_scope", _fake_session_scope(object()))

    result = await call("report_packs", {"report_week": "2026-W36"}, settings=_settings())
    weeks_rows = [r for r in result.rows if r["kind"] == "week"]
    section_rows = [r for r in result.rows if r["kind"] == "sections"]
    assert {r["report_week"] for r in weeks_rows} == {"2026-W36", "2026-W35"}
    assert section_rows[0]["section_keys"] == ["market", "portfolio"]
    assert {"kind": "report_pack", "id": "2026-W36"} in result.record_ids


async def test_report_packs_demo_mode_uses_demo_fixtures(monkeypatch):
    monkeypatch.setattr(reports_module, "_demo_weeks", lambda: ["2026-W36"])

    async def _fake_demo_pack(week, settings):
        return {"report_week": week, "sections": {"portfolio": {}}}

    monkeypatch.setattr(reports_module, "_demo_pack", _fake_demo_pack)

    result = await call("report_packs", {"report_week": "2026-W36"}, settings=_settings(demo_mode=True))
    assert any(r["kind"] == "sections" for r in result.rows)


# ── journal_entries (direct model read — no journal service exists yet) ───────────
class _SeqResult:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _OneStmtDB:
    def __init__(self, rows: list[Any]) -> None:
        self._rows = rows

    async def execute(self, stmt: object) -> _SeqResult:
        return _SeqResult(self._rows)


def _journal_row(**kw: Any) -> JournalEntry:
    base: dict[str, Any] = dict(
        id=1, entry_date=date(2026, 9, 1), title="t", view="v", probability=0.6,
        review_date=date(2026, 9, 30), horizon_days=29, subject="AAPL", status="open",
        source="human", forecast_id=None,
    )
    base.update(kw)
    return JournalEntry(**base)


async def test_journal_entries_real_mode_shapes_rows(monkeypatch):
    rows = [_journal_row(id=1), _journal_row(id=2, status="reviewed")]
    monkeypatch.setattr(cockpit_tools, "session_scope", _fake_session_scope(_OneStmtDB(rows)))

    result = await call("journal_entries", {}, settings=_settings())
    assert [r["id"] for r in result.rows] == [1, 2]
    assert result.record_ids == [{"kind": "journal_entry", "id": 1}, {"kind": "journal_entry", "id": 2}]


async def test_journal_entries_demo_mode_is_empty():
    result = await call("journal_entries", {}, settings=_settings(demo_mode=True))
    assert result.rows == [] and result.record_ids == [] and result.note


# ── notes (direct model read — no notes service exists yet) ──────────────────────
def _note_row(**kw: Any) -> Note:
    base: dict[str, Any] = dict(
        id=1, slug="s1", kind="research_note", title="t", status="draft",
        one_sentence="x", subjects=["AAPL"], evidence_refs=[{"kind": "url", "id": "u", "label": "l"}],
    )
    base.update(kw)
    return Note(**base)


async def test_notes_real_mode_shapes_rows(monkeypatch):
    rows = [_note_row(id=1), _note_row(id=2, kind="thesis")]
    monkeypatch.setattr(cockpit_tools, "session_scope", _fake_session_scope(_OneStmtDB(rows)))

    result = await call("notes", {}, settings=_settings())
    assert [r["id"] for r in result.rows] == [1, 2]
    assert result.record_ids == [{"kind": "note", "id": 1}, {"kind": "note", "id": 2}]


async def test_notes_demo_mode_is_empty():
    result = await call("notes", {}, settings=_settings(demo_mode=True))
    assert result.rows == [] and result.record_ids == [] and result.note


# ── provenance ─────────────────────────────────────────────────────────
async def test_provenance_requires_report_week():
    result = await call("provenance", {}, settings=_settings())
    assert result.rows == [] and result.note


async def test_provenance_real_mode_reads_report_pack_sources(monkeypatch):
    async def _fake_resolve(db, week):
        return SimpleNamespace(report_week="2026-W36", sources={
            "portfolio": {"engine": "portfolio-v1", "fetched_at": "2026-09-01T00:00:00Z", "stale": False, "error": None},
            "market": {"engine": "market-v1", "fetched_at": "2026-09-01T00:00:00Z", "stale": True, "error": None},
        })

    monkeypatch.setattr(reports_module, "_resolve_pack_row", _fake_resolve)
    monkeypatch.setattr(cockpit_tools, "session_scope", _fake_session_scope(object()))

    result = await call("provenance", {"report_week": "latest", "key": "market"}, settings=_settings())
    assert len(result.rows) == 1
    assert result.rows[0]["section"] == "market"
    assert result.rows[0]["stale"] is True
    assert result.record_ids == [{"kind": "report_pack", "id": "2026-W36"}]


async def test_provenance_demo_mode_reads_demo_pack_sources(monkeypatch):
    async def _fake_demo_pack(week, settings):
        return {"report_week": week, "sources": {"portfolio": {"engine": "demo fixtures", "stale": False, "error": None}}}

    monkeypatch.setattr(reports_module, "_demo_pack", _fake_demo_pack)

    result = await call("provenance", {"report_week": "2026-W36"}, settings=_settings(demo_mode=True))
    assert result.rows[0]["engine"] == "demo fixtures"
