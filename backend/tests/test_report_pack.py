"""Tests for the weekly Report Pack chapter sweep engine.

Pure fixture tests, no live Postgres/Redis/egress — mirrors
`test_advisor_report.py`'s convention: every underlying engine is
monkeypatched at the module-level name (`report_pack_module.RiskEngine`
etc.), `session_scope` is faked the same way `test_demo_domains.py` does.

Covers the spec acceptance points:
  (a) `build_chapters` — all engines up -> no chapter absent; one dead
      engine -> only that chapter absent, the rest intact.
  (b) `chapter_portfolio` — positions/weights/WoW deltas vs the prior
      week's `portfolio_snapshots` (a new position has no WoW delta, not a
      zero one).
  (c) `chapter_market` / `chapter_quant` / `chapter_methodology` — chapter
      shape + the held-symbols-only cross-section slice.
  (d) `chapter_holdings` — per-symbol assembly + holdings-filtered news.
  (e) forensic score deltas: nonzero ONLY across a filing-vintage change,
      never a naive WoW figure (ties with the two most recent stored
      readings are skipped).
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import app.services.report_pack as report_pack_module
from app.config import Settings
from app.services.report_pack import ReportPackEngine, _filter_news, _project_news_item, _snapshot_dict


def _settings(**kw: Any) -> Settings:
    kw.setdefault("demo_mode", False)
    return Settings(
        postgres_user="u", postgres_password="p", postgres_db="d", redis_password="r", **kw,
    )


# ── fake DB / session_scope plumbing (mirrors test_advisor_report.py) ─────
class _FakeResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, results: list[_FakeResult]) -> None:
        self._results = list(results)

    async def execute(self, stmt):
        return self._results.pop(0)


def _wire_session_scope(monkeypatch, results_factory) -> None:
    @contextlib.asynccontextmanager
    async def _fake():
        yield _FakeDB(results_factory())

    monkeypatch.setattr(report_pack_module, "session_scope", _fake)


class _FakeSnapRow:
    def __init__(self, report_week: str, symbol: str, qty: float, value_eur: float, nav: float, cash: float) -> None:
        self.report_week = report_week
        self.symbol = symbol
        self.qty = qty
        self.value_eur = value_eur
        self.nav = nav
        self.cash = cash


# ── fake WealthfolioBridge ─────────────────────────────────────────────────
class _FakeAsset:
    def __init__(self, symbol: str, asset_class: str = "") -> None:
        self.symbol = symbol
        self.asset_class = asset_class


class _FakeHoldings:
    def __init__(self, symbols: list[str]) -> None:
        self.by_asset = [_FakeAsset(s) for s in symbols]


class _FakeWF:
    def __init__(self, symbols: list[str]) -> None:
        self._symbols = symbols

    async def holdings(self) -> _FakeHoldings:
        return _FakeHoldings(self._symbols)


def _make_fake_wf(symbols: list[str]):
    return lambda redis, settings: _FakeWF(symbols)


class _MD:
    """Stand-in for a pydantic model: only `.model_dump()` is used."""

    def __init__(self, d: dict[str, Any]) -> None:
        self._d = d

    def model_dump(self) -> dict[str, Any]:
        return self._d


# ── pure helpers ────────────────────────────────────────────────────────────
def test_filter_news_matches_title_and_topics_case_insensitively():
    rows = [
        {"title": "BTC rallies on ETF inflows", "summary": "", "topics": []},
        {"title": "Local weather report", "summary": "", "topics": ["ETH"]},
        {"title": "Macro roundup", "summary": "nothing about holdings", "topics": []},
    ]
    assert [r["title"] for r in _filter_news(rows, "btc")] == ["BTC rallies on ETF inflows"]
    assert [r["title"] for r in _filter_news(rows, "eth")] == ["Local weather report"]
    assert _filter_news(rows, "AAPL") == []


def test_project_news_item_allowlists_named_fields_only():
    """review fix round: the raw `IntelService.news()` row carries
    fields that must never be persisted into a report pack — only the named
    allowlist survives."""
    row = {
        "title": "BTC rallies on ETF inflows", "summary": "full article text",
        "link": "https://example.com/btc", "source": "CoinDesk",
        "published": "2026-08-14T00:00:00+00:00", "sentiment": "positive",
        "sentiment_score": 0.8, "topics": ["BTC"], "grounding": "speculative",
        "cluster_id": 42, "country": "US", "lat": 40.0, "lon": -74.0,
    }
    projected = _project_news_item(row)
    assert projected == {
        "title": "BTC rallies on ETF inflows", "source": "CoinDesk",
        "published": "2026-08-14T00:00:00+00:00", "sentiment": "positive",
        "score": 0.8, "topics": ["BTC"], "link": "https://example.com/btc",
    }


def test_snapshot_dict_projects_expected_fields():
    row = _FakeSnapRow("2026-W34", "BTC", 1.0, 1000.0, 1200.0, 200.0)
    assert _snapshot_dict(row) == {
        "report_week": "2026-W34", "symbol": "BTC",
        "qty": 1.0, "value_eur": 1000.0, "nav": 1200.0, "cash": 200.0,
    }


# ── chapter_portfolio: positions/weights/WoW deltas ─────────────────────────
def test_chapter_portfolio_wow_deltas_and_new_position_has_no_delta(monkeypatch):
    weeks = ["2026-W34", "2026-W33"]
    snap_rows = [
        _FakeSnapRow("2026-W33", "BTC", 1.0, 800.0, 1000.0, 200.0),
        _FakeSnapRow("2026-W34", "BTC", 1.0, 1000.0, 1200.0, 200.0),
        _FakeSnapRow("2026-W34", "ETH", 2.0, 200.0, 1200.0, 200.0),  # new this week
    ]
    _wire_session_scope(monkeypatch, lambda: [_FakeResult(weeks), _FakeResult(snap_rows)])

    class _FakeRiskEngine:
        def __init__(self, redis, settings) -> None:
            pass

        async def bundle(self) -> dict[str, Any]:
            return {"var": {"cvar_95_pct": -4.1}, "coverage": {"n_covered": 2}}

    monkeypatch.setattr(report_pack_module, "RiskEngine", _FakeRiskEngine)

    engine = ReportPackEngine(object(), _settings())
    chapter = asyncio.run(engine.chapter_portfolio("2026-W34"))

    assert chapter["prior_report_week"] == "2026-W33"
    assert chapter["nav_eur"] == 1200.0
    positions = {p["symbol"]: p for p in chapter["positions"]}
    btc = positions["BTC"]
    assert btc["weight_pct"] == round(1000 / 1200 * 100, 4)
    assert btc["wow_value_delta_eur"] == 200.0
    assert btc["wow_weight_delta_pct"] == round((1000 / 1200 * 100) - (800 / 1000 * 100), 4)
    eth = positions["ETH"]
    assert eth["wow_value_delta_eur"] is None  # new position, not a zero delta
    assert eth["wow_weight_delta_pct"] is None
    assert chapter["var_cvar"] == {"cvar_95_pct": -4.1}
    assert chapter["coverage"] == {"n_covered": 2}
    assert chapter["scorecard"]["depth_weeks"] == 2
    assert "engine" in chapter and "how_to_read" in chapter


def test_chapter_portfolio_folds_cash_equivalents_into_cash_without_double_count(monkeypatch):
    """Mirrors advisor.py: a stablecoin holding (USDC) is treated as cash —
    never a position/weight row — and its EUR value is folded into
    `cash_eur` additively alongside broker cash, with no double count."""
    weeks = ["2026-W34"]
    snap_rows = [
        _FakeSnapRow("2026-W34", "BTC", 1.0, 1000.0, 1400.0, 200.0),
        _FakeSnapRow("2026-W34", "USDC", 200.0, 200.0, 1400.0, 200.0),
    ]
    _wire_session_scope(monkeypatch, lambda: [_FakeResult(weeks), _FakeResult(snap_rows)])

    class _FakeRiskEngine:
        def __init__(self, redis, settings) -> None:
            pass

        async def bundle(self) -> dict[str, Any]:
            return {"var": None, "coverage": None}

    monkeypatch.setattr(report_pack_module, "RiskEngine", _FakeRiskEngine)

    engine = ReportPackEngine(object(), _settings())
    chapter = asyncio.run(engine.chapter_portfolio("2026-W34"))

    assert {p["symbol"] for p in chapter["positions"]} == {"BTC"}  # USDC never a position row
    assert chapter["cash_eur"] == 400.0  # broker cash (200) + USDC value (200), no double count
    assert chapter["cash_equivalents_eur"] == 200.0
    assert chapter["cash_equivalents"] == ["USDC"]


# ── chapter_market ───────────────────────────────────────────────────────────
class _FakeMarketResearchEngine:
    def __init__(self, redis, settings) -> None:
        pass

    async def bundle(self) -> dict[str, Any]:
        return {
            "regime": {"state": "risk-on"}, "cross_asset": {"a": 1}, "rotation": {"b": 2},
            "rrg": {"c": 3}, "volatility": {"d": 4}, "breadth": {"e": 5},
            "sentiment": {"f": 6}, "valuation": {"g": 7}, "conditions": {"h": 8},
        }


class _FakeCalendarEnvelope:
    def model_dump(self, mode: str | None = None) -> dict[str, Any]:
        return {"data": {"events": []}, "source": "fred", "stale": False}


def test_chapter_market_assembles_bundle_and_week_ahead_calendar(monkeypatch):
    seen_days: list[int] = []

    class _FakeMacroAdapter:
        def __init__(self, redis, settings) -> None:
            pass

        async def calendar(self, days: int = 14) -> _FakeCalendarEnvelope:
            seen_days.append(days)
            return _FakeCalendarEnvelope()

    monkeypatch.setattr(report_pack_module, "MarketResearchEngine", _FakeMarketResearchEngine)
    monkeypatch.setattr(report_pack_module, "MacroAdapter", _FakeMacroAdapter)

    engine = ReportPackEngine(object(), _settings())
    chapter = asyncio.run(engine.chapter_market())

    assert chapter["regime"] == {"state": "risk-on"}
    assert chapter["sentiment"] == {"f": 6}
    assert "conditions" not in chapter  # not part of the fixed ch3 interface
    assert chapter["econ_calendar_week_ahead"] == {"data": {"events": []}, "source": "fred", "stale": False}
    assert seen_days == [7]  # week-ahead, not the adapter's 14-day default


# ── chapter_holdings: per-symbol + holdings-filtered news ───────────────────
class _FakeResearchEngine:
    def __init__(self, redis, settings) -> None:
        pass

    async def fundamentals(self, symbol: str, *, asset_class: str | None = None):
        return _MD({"symbol": symbol}), {}

    async def valuation(self, symbol: str, *, asset_class: str | None = None):
        return _MD({"symbol": symbol}), {}

    async def scores(self, symbol: str, *, asset_class: str | None = None):
        return _MD({
            "symbol": symbol, "name": symbol, "fiscal_years": ["2025"],
            "cards": [{"key": "piotroski", "label": "Piotroski F", "score": 7.0,
                       "scale": "0-9", "verdict": "Strong", "components": []}],
            "warnings": [],
        }), {}

    async def technicals(self, symbol: str, *, asset_class: str | None = None):
        return _MD({"symbol": symbol}), {}


class _FakeScannerEngine:
    def __init__(self, redis, settings) -> None:
        pass

    async def scan(self, patterns, universe, emit: bool = False):
        return _MD({"universe": universe})


class _FakeIntelService:
    def __init__(self, redis, settings) -> None:
        pass

    async def news(self, limit: int = 60) -> list[dict[str, Any]]:
        return [
            {
                "title": "BTC rallies on ETF inflows", "summary": "full article text here",
                "topics": [], "link": "https://example.com/btc", "source": "CoinDesk",
                "published": "2026-08-14T00:00:00+00:00", "sentiment": "positive",
                "sentiment_score": 0.8, "grounding": "speculative", "cluster_id": 42,
                "country": "US", "lat": 40.0, "lon": -74.0,
            },
            {"title": "Local weather report", "summary": "", "topics": ["ETH"]},
            {"title": "Macro roundup", "summary": "nothing about holdings", "topics": []},
        ]


def test_chapter_holdings_assembles_per_symbol_and_filters_news(monkeypatch):
    monkeypatch.setattr(report_pack_module, "make_wealthfolio", _make_fake_wf(["BTC", "ETH"]))
    monkeypatch.setattr(report_pack_module, "ResearchEngine", _FakeResearchEngine)
    monkeypatch.setattr(report_pack_module, "ScannerEngine", _FakeScannerEngine)
    monkeypatch.setattr(report_pack_module, "IntelService", _FakeIntelService)
    _wire_session_scope(monkeypatch, lambda: [_FakeResult([])])  # no prior report_packs

    engine = ReportPackEngine(object(), _settings())
    chapter = asyncio.run(engine.chapter_holdings())

    assert set(chapter["symbols"]) == {"BTC", "ETH"}
    assert [n["title"] for n in chapter["symbols"]["BTC"]["news"]] == ["BTC rallies on ETF inflows"]
    assert [n["title"] for n in chapter["symbols"]["ETH"]["news"]] == ["Local weather report"]
    assert chapter["symbols"]["BTC"]["forensic"]["deltas"]["piotroski"]["delta"] is None
    assert chapter["symbols"]["BTC"]["scan"] == {"universe": ["BTC"]}
    assert chapter["n_symbols_failed"] == 0
    assert chapter["symbols_failed"] == {}
    # review fix round: only the allowlisted news fields survive
    # into storage — summary/grounding/cluster_id/country/lat/lon are gone.
    btc_news_item = chapter["symbols"]["BTC"]["news"][0]
    assert set(btc_news_item) == {"title", "source", "published", "sentiment", "score", "topics", "link"}
    assert btc_news_item["score"] == 0.8  # projected from the raw row's `sentiment_score`
    assert btc_news_item["link"] == "https://example.com/btc"


# ── controller fix round: per-symbol resilience within chapter_holdings ────
def test_chapter_holdings_isolates_a_failing_symbol_others_get_full_tear_sheets(monkeypatch):
    """3 held symbols, the middle one's engine raises: the chapter itself is
    NOT absent, the other two symbols get full tear sheets, and the failing
    one is isolated to its own `absent` slot with the failure counted +
    named — "a dead source never sinks the pack" applies WITHIN a chapter,
    not just across chapters."""

    class _FlakyResearchEngine(_FakeResearchEngine):
        async def fundamentals(self, symbol: str, *, asset_class: str | None = None):
            if symbol == "MID":
                raise ConnectionError("upstream fundamentals adapter down for MID")
            return await super().fundamentals(symbol)

    monkeypatch.setattr(report_pack_module, "make_wealthfolio", _make_fake_wf(["AAA", "MID", "ZZZ"]))
    monkeypatch.setattr(report_pack_module, "ResearchEngine", _FlakyResearchEngine)
    monkeypatch.setattr(report_pack_module, "ScannerEngine", _FakeScannerEngine)
    monkeypatch.setattr(report_pack_module, "IntelService", _FakeIntelService)
    _wire_session_scope(monkeypatch, lambda: [_FakeResult([])])

    engine = ReportPackEngine(object(), _settings())
    chapter = asyncio.run(engine.chapter_holdings())

    assert "absent" not in chapter  # the CHAPTER itself is not absent
    assert set(chapter["symbols"]) == {"AAA", "MID", "ZZZ"}
    assert chapter["symbols"]["MID"] == {
        "absent": True, "reason": "upstream fundamentals adapter down for MID",
    }
    for ok_symbol in ("AAA", "ZZZ"):
        entry = chapter["symbols"][ok_symbol]
        assert "absent" not in entry
        assert set(entry) == {"fundamentals", "valuation", "forensic", "technicals", "scan", "news"}
    assert chapter["n_symbols_failed"] == 1
    assert chapter["symbols_failed"] == {"MID": "upstream fundamentals adapter down for MID"}


def test_chapter_holdings_excludes_cash_equivalents_from_tear_sheets(monkeypatch):
    """Mirrors advisor.py: a stablecoin holding (USDC) never gets a tear
    sheet — it's cash, not a position — and is recorded additively so
    coverage stays honest instead of silently vanishing."""
    monkeypatch.setattr(report_pack_module, "make_wealthfolio", _make_fake_wf(["BTC", "USDC"]))
    monkeypatch.setattr(report_pack_module, "ResearchEngine", _FakeResearchEngine)
    monkeypatch.setattr(report_pack_module, "ScannerEngine", _FakeScannerEngine)
    monkeypatch.setattr(report_pack_module, "IntelService", _FakeIntelService)
    _wire_session_scope(monkeypatch, lambda: [_FakeResult([])])

    engine = ReportPackEngine(object(), _settings())
    chapter = asyncio.run(engine.chapter_holdings())

    assert set(chapter["symbols"]) == {"BTC"}  # USDC gets no tear sheet
    assert chapter["cash_equivalents_excluded"] == ["USDC"]
    assert chapter["n_symbols_failed"] == 0  # excluded, never a failure
    assert chapter["symbols_failed"] == {}


def test_build_chapters_threads_symbols_failed_into_holdings_source():
    """build_chapters' `sources["holdings"]` must surface a partial
    per-symbol degradation even though the chapter itself succeeded (not
    absent) — a consumer scanning `sources` alone must still see the pack
    is degraded."""

    class _PartialHoldingsEngine(_StubEngine):
        async def chapter_holdings(self) -> dict[str, Any]:
            return {
                "engine": "x", "how_to_read": "y",
                "symbols": {"AAA": {"ok": True}, "MID": {"absent": True, "reason": "boom"}},
                "symbols_failed": {"MID": "boom"}, "n_symbols_failed": 1,
            }

    engine = _PartialHoldingsEngine()
    sections, sources = asyncio.run(engine.build_chapters("2026-W34"))

    assert "absent" not in sections["holdings"]  # chapter-level success
    assert sources["holdings"]["partial"] == {"symbols_failed": {"MID": "boom"}, "n_symbols_failed": 1}
    assert sources["holdings"]["stale"] is False  # `stale` keeps its codebase-wide old/cached meaning
    assert sources["holdings"]["error"] is None


# ── forensic deltas: nonzero ONLY across a filing-vintage change ────────────
def test_forensic_deltas_skip_ties_and_land_on_the_last_changed_reading(monkeypatch):
    """Two most recent stored weeks tie with the current score (structurally
    zero — no new filing yet); the delta must land on the older week where
    the score actually changed, labeled with THAT week's fiscal year."""
    rows = [
        ("2026-W34", {"holdings": {"symbols": {"AAPL": {"forensic": {
            "cards": [{"key": "piotroski", "score": 7.0}], "fiscal_years": ["2025"],
        }}}}}),
        ("2026-W33", {"holdings": {"symbols": {"AAPL": {"forensic": {
            "cards": [{"key": "piotroski", "score": 7.0}], "fiscal_years": ["2025"],
        }}}}}),
        ("2026-W32", {"holdings": {"symbols": {"AAPL": {"forensic": {
            "cards": [{"key": "piotroski", "score": 5.0}], "fiscal_years": ["2024"],
        }}}}}),
    ]
    _wire_session_scope(monkeypatch, lambda: [_FakeResult(rows)])

    engine = ReportPackEngine(object(), _settings())
    deltas = asyncio.run(engine._forensic_deltas(
        "AAPL", [{"key": "piotroski", "score": 7.0}], ["2026"],
    ))
    assert deltas["piotroski"]["delta"] == 2.0  # 7.0 - 5.0, skipping the two ties
    assert deltas["piotroski"]["vs_report_week"] == "2026-W32"
    assert deltas["piotroski"]["vs_fiscal_year"] == "2024"
    assert "filing-vintage" in deltas["piotroski"]["label"]


def test_forensic_deltas_none_when_no_prior_reading_matches(monkeypatch):
    _wire_session_scope(monkeypatch, lambda: [_FakeResult([])])
    engine = ReportPackEngine(object(), _settings())
    deltas = asyncio.run(engine._forensic_deltas(
        "AAPL", [{"key": "piotroski", "score": 7.0}], ["2026"],
    ))
    assert deltas["piotroski"]["delta"] is None
    assert deltas["piotroski"]["label"] == "no prior stored reading to compare against"


def test_forensic_deltas_unchanged_label_when_all_prior_readings_tie(monkeypatch):
    """review fix round: prior stored readings EXIST but are all
    identical to the current score (the normal ~51-weeks/year steady state,
    no new filing) — must be labeled "unchanged since <last stored
    reading>", never the "no prior stored reading" falsehood (which is only
    true when NO prior reading exists at all)."""
    rows = [
        ("2026-W34", {"holdings": {"symbols": {"AAPL": {"forensic": {
            "cards": [{"key": "piotroski", "score": 7.0}], "fiscal_years": ["2025"],
        }}}}}),
        ("2026-W33", {"holdings": {"symbols": {"AAPL": {"forensic": {
            "cards": [{"key": "piotroski", "score": 7.0}], "fiscal_years": ["2025"],
        }}}}}),
    ]
    _wire_session_scope(monkeypatch, lambda: [_FakeResult(rows)])

    engine = ReportPackEngine(object(), _settings())
    deltas = asyncio.run(engine._forensic_deltas(
        "AAPL", [{"key": "piotroski", "score": 7.0}], ["2026"],
    ))
    assert deltas["piotroski"]["delta"] == 0.0
    assert deltas["piotroski"]["vs_report_week"] == "2026-W34"  # most recent prior reading
    assert deltas["piotroski"]["vs_fiscal_year"] == "2025"
    assert deltas["piotroski"]["label"].startswith("unchanged since report 2026-W34")
    assert "no prior stored reading" not in deltas["piotroski"]["label"]


# ── chapter_quant: factor embedding + held-symbols-only cross-section ──────
class _FakeCatalogEntry:
    def __init__(self, signal_id: str, last_date: str | None) -> None:
        self.signal_id = signal_id
        self.last_date = last_date


class _FakeCatalog:
    def __init__(self, entries: list[_FakeCatalogEntry]) -> None:
        self.signals = entries


class _FakeCrossSectionRow:
    def __init__(self, symbol: str, value: float) -> None:
        self.symbol = symbol
        self.value = value

    def model_dump(self) -> dict[str, Any]:
        return {"symbol": self.symbol, "value": self.value}


class _FakeCrossSection:
    def __init__(self, as_of_date: str, rows: list[_FakeCrossSectionRow]) -> None:
        self.as_of_date = as_of_date
        self.rows = rows


class _FakeSignalEngine:
    def __init__(self, redis, settings) -> None:
        pass

    async def scoreboard(self):
        return _MD({"rows": []})

    async def catalog(self) -> _FakeCatalog:
        return _FakeCatalog([_FakeCatalogEntry("mom_63d", "2026-08-14")])

    async def cross_section(self, signal_id: str, as_of: str) -> _FakeCrossSection:
        return _FakeCrossSection(as_of, [
            _FakeCrossSectionRow("BTC", 1.2), _FakeCrossSectionRow("AAPL", 0.5),
        ])


def test_chapter_quant_filters_cross_section_to_held_symbols_and_embeds_factor(monkeypatch):
    monkeypatch.setattr(report_pack_module, "SignalEngine", _FakeSignalEngine)
    monkeypatch.setattr(report_pack_module, "make_wealthfolio", _make_fake_wf(["BTC"]))

    engine = ReportPackEngine(object(), _settings())
    factor_payload = {"ic": 0.05}
    chapter = asyncio.run(engine.chapter_quant(factor_payload))

    assert chapter["factor"] == factor_payload
    cs = chapter["cross_section"]["mom_63d"]
    assert cs["n_universe"] == 2  # full universe size recorded...
    assert [r["symbol"] for r in cs["rows"]] == ["BTC"]  # ...but rows sliced to holdings only
    assert "how_to_read" in chapter and "within-book" in chapter["how_to_read"]


class _FakeSignalEngineWithStablecoin(_FakeSignalEngine):
    async def cross_section(self, signal_id: str, as_of: str) -> _FakeCrossSection:
        return _FakeCrossSection(as_of, [
            _FakeCrossSectionRow("BTC", 1.2), _FakeCrossSectionRow("USDC", 0.0),
            _FakeCrossSectionRow("AAPL", 0.5),
        ])


def test_chapter_quant_excludes_cash_equivalents_from_held_set(monkeypatch):
    """Mirrors advisor.py: a held stablecoin (USDC) is cash, not a position
    — it never enters the held-set used to slice signal cross-sections,
    even when the cross-section itself has a row for it."""
    monkeypatch.setattr(report_pack_module, "SignalEngine", _FakeSignalEngineWithStablecoin)
    monkeypatch.setattr(report_pack_module, "make_wealthfolio", _make_fake_wf(["BTC", "USDC"]))

    engine = ReportPackEngine(object(), _settings())
    chapter = asyncio.run(engine.chapter_quant({"ic": 0.05}))

    cs = chapter["cross_section"]["mom_63d"]
    assert [r["symbol"] for r in cs["rows"]] == ["BTC"]  # USDC held but excluded
    assert cs["n_universe"] == 3


def test_chapter_quant_factor_absent_when_none(monkeypatch):
    monkeypatch.setattr(report_pack_module, "SignalEngine", _FakeSignalEngine)
    monkeypatch.setattr(report_pack_module, "make_wealthfolio", _make_fake_wf(["BTC"]))

    engine = ReportPackEngine(object(), _settings())
    chapter = asyncio.run(engine.chapter_quant(None))
    assert chapter["factor"]["absent"] is True


# ── chapter_methodology ──────────────────────────────────────────────────────
def test_chapter_methodology_is_static_and_versioned():
    engine = ReportPackEngine(object(), _settings())
    chapter = asyncio.run(engine.chapter_methodology())
    assert chapter["version"] == report_pack_module._METHODOLOGY_VERSION
    assert {"risk.RiskEngine", "signals.SignalEngine"} <= {t["tool"] for t in chapter["tools"]}
    for tool in chapter["tools"]:
        assert {"tool", "measures", "endpoint", "limitations", "grading_horizon"} <= tool.keys()


def test_chapter_methodology_covers_the_shadow_grading_engine():
    """review fix round: the appendix must cover chapter 6's
    shadow-grading engine too, not just this module's own five tools —
    grading horizon, both benchmark definitions, and the evidence-only
    clause must all be present."""
    engine = ReportPackEngine(object(), _settings())
    chapter = asyncio.run(engine.chapter_methodology())
    shadow = next(t for t in chapter["tools"] if "shadow" in t["tool"].lower())
    assert "evidence-only" in shadow["measures"]
    assert shadow["grading_horizon"] == "4-week forward window from the verdict's report_week"
    assert "book-median" in shadow["benchmark"]
    assert "up-rate" in shadow["benchmark"]
    assert "overlap veto" in shadow["limitations"]
    assert "forensic" in shadow["limitations"].lower()


# ── build_chapters: orchestration + per-chapter isolation ───────────────────
class _StubEngine(ReportPackEngine):
    """Overrides every chapter_* at the class level so build_chapters' own
    try/except-per-chapter orchestration is tested in isolation from the
    real engines (those are covered chapter-by-chapter above)."""

    def __init__(self) -> None:  # no redis/settings needed for these tests
        pass

    async def chapter_portfolio(self, report_week: str) -> dict[str, Any]:
        return {"engine": "x", "how_to_read": "y", "chapter": "portfolio"}

    async def chapter_market(self) -> dict[str, Any]:
        return {"engine": "x", "how_to_read": "y", "chapter": "market"}

    async def chapter_holdings(self) -> dict[str, Any]:
        return {"engine": "x", "how_to_read": "y", "chapter": "holdings"}

    async def chapter_quant(self, factor: dict[str, Any] | None) -> dict[str, Any]:
        return {"engine": "x", "how_to_read": "y", "chapter": "quant"}

    async def chapter_methodology(self) -> dict[str, Any]:
        return {"engine": "x", "how_to_read": "y", "chapter": "methodology"}


def test_build_chapters_all_engines_up_none_absent():
    engine = _StubEngine()
    sections, sources = asyncio.run(engine.build_chapters("2026-W34"))
    assert set(sections) == {"portfolio", "market", "holdings", "quant", "methodology"}
    for key in sections:
        assert "absent" not in sections[key]
        assert sources[key]["error"] is None
        assert sources[key]["stale"] is False


def test_build_chapters_one_dead_engine_only_that_chapter_absent():
    class _OneDeadEngine(_StubEngine):
        async def chapter_portfolio(self, report_week: str) -> dict[str, Any]:
            raise ConnectionError("risk engine down")

    engine = _OneDeadEngine()
    sections, sources = asyncio.run(engine.build_chapters("2026-W34"))

    assert sections["portfolio"] == {"absent": True, "reason": "risk engine down"}
    assert sources["portfolio"]["error"] == "risk engine down"
    # review fix round: a hard chapter failure serves NOTHING, so it
    # is not staleness (`stale` = old/cached data being served) — the
    # `error` field alone carries the failure reason.
    assert sources["portfolio"]["stale"] is False
    for key in ("market", "holdings", "quant", "methodology"):
        assert "absent" not in sections[key]
        assert sources[key]["error"] is None


# ── controller-ordered hook: embedded factor payload markers -> sources ────
def test_build_chapters_projects_factor_symbol_resolution_failures_into_partial():
    """The in-process factor study can partially fail per symbol without
    failing the whole quant chapter — those failures must reach
    `sources["quant"]` with counts, never silently dropped inside the
    chapter body alone."""

    class _FactorFailuresEngine(_StubEngine):
        async def chapter_quant(self, factor: dict[str, Any] | None) -> dict[str, Any]:
            return {
                "engine": "x", "how_to_read": "y",
                "factor": {
                    "ic": 0.05,
                    "symbol_resolution_failures": {"XYZ": "no price history"},
                    "n_symbols_resolution_failed": 1,
                },
            }

    engine = _FactorFailuresEngine()
    sections, sources = asyncio.run(engine.build_chapters("2026-W34"))

    assert "absent" not in sections["quant"]  # chapter itself succeeded
    assert sources["quant"]["partial"] == {
        "symbol_resolution_failures": {"XYZ": "no price history"},
        "n_symbols_resolution_failed": 1,
    }
    assert sources["quant"]["stale"] is False  # resolution failures =/= staleness
    assert sources["quant"]["error"] is None


def test_build_chapters_projects_factor_stale_fallback_into_sources_stale():
    """The stale-fallback (quant work timed out this week -> ships the
    latest COMPLETED prior pack's factor reading) stamps `stale` in the
    chapter payload; that must ALSO reach `sources["quant"].stale` — the
    codebase-wide cached/old-data meaning applies here."""

    class _StaleFactorEngine(_StubEngine):
        async def chapter_quant(self, factor: dict[str, Any] | None) -> dict[str, Any]:
            return {
                "engine": "x", "how_to_read": "y",
                "factor": {
                    "ic": 0.03, "stale": True,
                    "stale_source_report_week": "2026-W33",
                    "stale_reason": "quant work failed/timed out this week",
                },
            }

    engine = _StaleFactorEngine()
    sections, sources = asyncio.run(engine.build_chapters("2026-W34"))

    assert "absent" not in sections["quant"]
    assert sources["quant"]["stale"] is True
    assert sources["quant"]["error"] is None
    assert "partial" not in sources["quant"]  # no resolution failures here


def test_build_chapters_no_factor_leaves_quant_source_unchanged():
    """chapter_quant(factor=None) -> the absent-factor marker carries no
    `symbol_resolution_failures`/`stale` keys, so `sources["quant"]` stays
    exactly the base 4-key shape — no spurious `partial`, `stale` stays
    False."""
    class _NoFactorEngine(_StubEngine):
        async def chapter_quant(self, factor: dict[str, Any] | None) -> dict[str, Any]:
            return {
                "engine": "x", "how_to_read": "y",
                "factor": {"absent": True, "reason": "no factor study payload for this report week"},
            }

    engine = _NoFactorEngine()
    sections, sources = asyncio.run(engine.build_chapters("2026-W34"))

    assert "absent" not in sections["quant"]
    assert sources["quant"] == {
        "engine": report_pack_module._ENGINE_NAMES["quant"],
        "fetched_at": sources["quant"]["fetched_at"],
        "stale": False, "error": None,
    }


# ── review-225d5f4 finding 4: the holding's known asset class reaches fundamentals ──
def test_chapter_holdings_threads_each_holdings_asset_class_into_research(monkeypatch):
    """An equity spelled like a coin (`SOL`) must reach the fundamentals /
    valuation / scores calls WITH its Wealthfolio class, so the crypto skip
    keys on class, not ticker spelling."""
    seen: list[tuple[str, str, Any]] = []

    class _RecordingResearch(_FakeResearchEngine):
        async def fundamentals(self, symbol: str, *, asset_class: str | None = None):
            seen.append(("fundamentals", symbol, asset_class))
            return await super().fundamentals(symbol)

        async def valuation(self, symbol: str, *, asset_class: str | None = None):
            seen.append(("valuation", symbol, asset_class))
            return await super().valuation(symbol)

        async def scores(self, symbol: str, *, asset_class: str | None = None):
            seen.append(("scores", symbol, asset_class))
            return await super().scores(symbol)

    class _ClassedHoldings:
        by_asset = [_FakeAsset("SOL", "Equity"), _FakeAsset("BTC", "CRYPTO")]

    class _ClassedWF:
        async def holdings(self):
            return _ClassedHoldings()

    monkeypatch.setattr(report_pack_module, "make_wealthfolio", lambda redis, settings: _ClassedWF())
    monkeypatch.setattr(report_pack_module, "ResearchEngine", _RecordingResearch)
    monkeypatch.setattr(report_pack_module, "ScannerEngine", _FakeScannerEngine)
    monkeypatch.setattr(report_pack_module, "IntelService", _FakeIntelService)
    _wire_session_scope(monkeypatch, lambda: [_FakeResult([])])

    chapter = asyncio.run(ReportPackEngine(object(), _settings()).chapter_holdings())

    assert chapter["symbols_failed"] == {}
    by_symbol = {(kind, sym): cls for kind, sym, cls in seen}
    for kind in ("fundamentals", "valuation", "scores"):
        assert by_symbol[(kind, "SOL")] == "EQUITY"
        assert by_symbol[(kind, "BTC")] == "CRYPTO"
