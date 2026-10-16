"""Tests for the reports router + scorecard.

Mirrors `test_demo_domains.py`'s style (pure unit tests, hand-rolled fakes,
no real Postgres/Redis) and `test_advisor_report.py`'s DB-seam monkeypatch
convention:
  (a) DEMO_MODE: reads come back empty/404 WITHOUT touching the DB at all
      (`_boom_session_scope` proves it — the same technique
      `test_demo_domains.py` uses); mutations are rejected with the same
      403 (`DemoModeReadOnly`) shape as signals/backtests/lab/algo.
  (b) demo_mode=False: every read/mutation reaches the real DB seam
      (`_DBTouched` proves the guard did NOT short-circuit).
  (c) append-only: accepting/overriding a recommendation inserts a NEW row
      referencing the prior row's id, and leaves EVERY column of the prior
      row exactly as it was — a full-column snapshot taken before the
      transition and compared after (controller review item 3: the earlier
      version of this test only checked `.status`, which couldn't have
      caught a stray write to some other column). The prior-row SELECT also
      carries `FOR UPDATE`, asserted via the compiled SQL. The new
      row carries the prior's own `note` (rule arithmetic) forward and files
      the human's reason under `input_snapshot["human_note"]` instead of
      overwriting it. The task's live curl exercise additionally
      proves the DB-level round trip.
  (d) `services/scorecard.py`'s pure TWR/sign-test math, computed honestly
      at depth 0, depth 1 (insufficient history), a no-trades window (arms
      coincide, `n_effective=0`), a quantity-changing trade (arms diverge),
      a cash-only deposit week with UNCHANGED quantities (item 2: arms must
      still diverge — the old quantity-only check would have missed this),
      and a long dormant run (item 1: many tied sub-periods must never
      inflate `n_effective`/deflate `p_floor_two_sided`).
"""
from __future__ import annotations

import contextlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal

import pytest
from fastapi.responses import FileResponse
from pydantic import ValidationError
from sqlalchemy.dialects import postgresql

import app.routers.reports as reports_router
import app.services.advisor_report as advisor_report_module
import app.services.recommendation_lifecycle as recommendation_lifecycle
from app.config import Settings
from app.errors import DemoModeReadOnly, InvalidRequest, NotFound
from app.models import AiRun, PortfolioSnapshot, Recommendation, ReportPack
from app.routers.reports import (
    RecommendationAction,
    RecommendationExecute,
    RecommendationModify,
    _assemble_for_week,
    _current_recommendations,
    accept_recommendation,
    execute_recommendation,
    get_report,
    get_report_pack,
    get_report_pdf,
    latest_report,
    list_weeks,
    modify_recommendation,
    override_recommendation,
    reject_recommendation,
)
from app.services.advisor import make_advisor
from app.services.demo import WF_ASSETS, WF_CASH, WF_TOTAL
from app.services.intel_service import IntelService
from app.services.market_research import MarketResearchEngine
from app.services.research import ResearchEngine
from app.services.risk import RiskEngine
from app.services.scorecard import (
    chain_link,
    compute_scorecard,
    modified_dietz_return,
    sign_test_two_sided_p_floor,
)
from app.services.shadow import ShadowEngine


def _settings(**kw: object) -> Settings:
    kw.setdefault("demo_mode", False)
    # Explicit test fixture (the shipped default band is neutral and configurable).
    kw.setdefault("advisor_tax_rate", 0.25)
    kw.setdefault("advisor_weight_bands", "CRYPTO:0.50,EQUITY:0.30,ETF:0.30,COMMODITY:0.15,OTHER:0.20")  # arbitrary fixture values
    return Settings(
        postgres_user="u", postgres_password="p", postgres_db="d", redis_password="r", **kw,
    )


class _DBTouched(Exception):
    """Raised the instant the faked `session_scope` is entered — proves
    execution reached a real DB read/write, i.e. the demo_mode guard
    upstream was NOT taken."""


def _boom_session_scope():
    raise _DBTouched()


# ── DEMO_MODE: a full synthetic report, still never touching the DB ──
async def test_list_weeks_nonempty_synthetic_in_demo_mode(monkeypatch) -> None:
    monkeypatch.setattr(reports_router, "session_scope", _boom_session_scope)
    weeks = (await list_weeks(_settings(demo_mode=True)))["weeks"]
    assert weeks  # a deterministic trailing window, no longer an empty stub
    assert all(w.count("-W") == 1 for w in weeks)


async def test_latest_report_full_synthetic_report_in_demo_mode(monkeypatch) -> None:
    monkeypatch.setattr(reports_router, "session_scope", _boom_session_scope)
    report = await latest_report(object(), _settings(demo_mode=True))
    assert report["report_week"]
    assert report["what_happened"]["nav_eur"] > 0
    assert report["what_happened"]["positions"]  # all four sections present
    assert report["where_we_stand"]["rule_evaluations"]
    assert report["scorecard"]["performance"] is not None
    # narration: absent-with-reason, not fabricated LLM output
    assert report["narrated"] is False
    assert report["narration"] is None
    assert report["narration_absent_reason"]

    # cash_floor_band still evaluates every week (it's account-level, not
    # per-position). The demo persona's liquid cash (fixed €1,900) doesn't
    # clear the neutral-regime band on a ~€48.5k NAV book, so a shortfall
    # exists — but it's suppressed by the cost gate, not proposed: `_week_holdings()` (this router's own
    # week-sampled snapshot bridge) carries no `cost_basis` at all, so every
    # trimmed leg falls back to SE4's conservative full-value-as-gain tax
    # estimate, and that conservative cost clears the default 10%
    # cost-fraction floor on this fixture. Either way it surfaces SOMEWHERE
    # (`where_we_stand.rule_evaluations` always carries it, `arithmetic`
    # included) — this is the suppressed-but-visible case, not a silent drop.
    cash_floor_evals = [r for r in report["where_we_stand"]["rule_evaluations"] if r["rule_id"] == "cash_floor_band"]
    assert cash_floor_evals
    assert cash_floor_evals[0]["status"] == "evaluated" and cash_floor_evals[0]["action"] == "hold"
    assert "cash-floor raise suppressed" in cash_floor_evals[0]["note"]


async def test_cash_floor_life_floor_reaches_the_engine_end_to_end_in_demo_mode(monkeypatch) -> None:
    """Design acceptance criterion 1 (nonzero demo life floor), exercised
    end-to-end: `AdvisorEngine`'s REAL BQL f-string, through the demo
    bridge's `ledger_query_rows` dispatch — not a hand-rolled copy of that
    string (`test_demo_seam.py` covers the dispatcher directly; this is the
    drift guard — if advisor.py's query shape ever changes and the demo
    dispatcher stops matching it, `life_floor_eur` silently reverts to 0 and
    THIS assertion is what catches it). The life floor itself is dormant by
    default now (`ADVISOR_LIFE_FLOOR_MULTIPLIER=0`)
    — explicitly re-enabled here (multiplier > 0) to keep exercising this
    drift guard, exactly the "multiplier > 0 fixtures where they test floor
    behavior" carve-out. Calls `evaluate()` directly rather than through the
    assembled report: a `hold` row's full `input_snapshot` (life_floor_eur
    included) isn't reachable through the report's summarized
    `rule_evaluations` view — only through the raw row, which this test
    reads directly."""
    monkeypatch.setattr(reports_router, "session_scope", _boom_session_scope)
    advisor = make_advisor(object(), _settings(demo_mode=True, advisor_life_floor_multiplier=6.0))
    rows = await advisor.evaluate(date.today(), "2026-W34")
    cash_floor_rows = [r for r in rows if r["rule_id"] == "cash_floor_band"]
    assert cash_floor_rows
    assert cash_floor_rows[0]["input_snapshot"]["life_floor_eur"] > 0


async def test_demo_mode_never_leaks_real_clearing_account_names_into_reports_payload(monkeypatch) -> None:
    """SEV2 (security review, 2026-08-16): DEMO_MODE runs the real
    `advisor.evaluate()` over real `Settings` — a nonzero-clearing-balance
    refusal row's full `input_snapshot` (including `clearing_accounts`, real
    ledger account paths) reaches the client verbatim via
    `what_to_do_next.refused`. Forces the refusal branch (a plausible real
    account path with a nonzero demo balance + the life floor active, same
    setup as the end-to-end drift guard above) and asserts the real value
    never appears anywhere in the assembled `/reports` payload — scrubbed at
    the source (`advisor.py`), not downstream."""
    monkeypatch.setattr(reports_router, "session_scope", _boom_session_scope)
    real_account = "Assets:Bank:Checking"
    settings = _settings(
        demo_mode=True, advisor_life_floor_multiplier=6.0, advisor_clearing_accounts=real_account,
    )
    report = await latest_report(object(), settings)
    refused = report["what_to_do_next"]["refused"]
    assert any(r["rule_id"] == "cash_floor_band" and r["action"] == "refuse" for r in refused)  # guard did fire
    payload = json.dumps(report, default=str)
    assert real_account not in payload
    assert "Assets:Demo:Clearing" in payload


async def test_get_report_full_synthetic_report_for_a_known_demo_week(monkeypatch) -> None:
    monkeypatch.setattr(reports_router, "session_scope", _boom_session_scope)
    weeks = (await list_weeks(_settings(demo_mode=True)))["weeks"]
    report = await get_report(weeks[-1], object(), _settings(demo_mode=True))
    assert report["report_week"] == weeks[-1]


async def test_get_report_404_for_unknown_week_in_demo_mode(monkeypatch) -> None:
    monkeypatch.setattr(reports_router, "session_scope", _boom_session_scope)
    with pytest.raises(NotFound):
        await get_report("1999-W01", object(), _settings(demo_mode=True))


# ── report-pack read surface (/weeks availability, /pack, /pdf) ──────
async def _boom_engine_method(self, *a, **kw) -> None:
    raise AssertionError("live engine touched under DEMO_MODE (no demo seam exists)")


async def test_demo_pack_never_touches_engines_without_demo_seams(monkeypatch) -> None:
    """Binding: RiskEngine/MarketResearchEngine/ResearchEngine/
    IntelService/ShadowEngine's DB-bound methods have NO demo seam — patched
    on the CLASS objects themselves (not the module attribute reports.py
    happens to import under), so this catches a future call regardless of
    how it's wired in. Chapters 1/3/4/5 stay absent; 2/6/7 are still built."""
    monkeypatch.setattr(reports_router, "session_scope", _boom_session_scope)
    monkeypatch.setattr(RiskEngine, "bundle", _boom_engine_method)
    monkeypatch.setattr(MarketResearchEngine, "bundle", _boom_engine_method)
    monkeypatch.setattr(ResearchEngine, "fundamentals", _boom_engine_method)
    monkeypatch.setattr(IntelService, "news", _boom_engine_method)
    monkeypatch.setattr(ShadowEngine, "scoreboard", _boom_engine_method)
    monkeypatch.setattr(ShadowEngine, "_load_history", _boom_engine_method)

    weeks = (await list_weeks(_settings(demo_mode=True)))["weeks"]
    pack = await get_report_pack(weeks[-1], _settings(demo_mode=True))

    assert set(pack["sections"]) == {
        "exec_summary", "portfolio", "market", "holdings", "quant", "shadow", "methodology",
    }
    for key in ("exec_summary", "market", "holdings", "quant"):
        assert pack["sections"][key] == {"absent": True, "reason": "demo mode"}
    assert pack["sections"]["portfolio"]["positions"]
    assert pack["sections"]["shadow"]["verdicts"]
    assert pack["sections"]["methodology"]["tools"]
    assert pack["pdf"] is False
    assert pack["narrated"] is False


# Belt-and-braces only: a denylist can never
# be the control here — a portfolio can hold any symbol, and no made-up
# euro figure proves anything about one. The WHITELIST test below is
# the actual control; this list just adds a second, independent tripwire.
_FORBIDDEN_FIXTURE_SYMBOLS = {"TSLA", "GOOGL", "AMZN", "SPY", "PLTR"}


async def test_demo_pack_content_no_symbols_or_nav_figures_beyond_the_fixture(monkeypatch) -> None:
    """WHITELIST is the control: every symbol
    appearing anywhere in the demo pack must be a member of the demo's own
    `WF_ASSETS` fixture universe — not "absent from a hand-picked denylist of
    3-5 tickers out of a real, much larger book". Every NAV/cash/position
    €-figure must EQUAL what the fixture actually computes for this week —
    not just "isn't one specific made-up literal". `"latest"` (= today) is
    used because `wf_networth_series` pins its LAST point exactly to
    WF_TOTAL/WF_CASH/WF_INVESTMENTS, giving an exact expected value with no
    RNG state to reproduce."""
    monkeypatch.setattr(reports_router, "session_scope", _boom_session_scope)
    pack = await get_report_pack("latest", _settings(demo_mode=True))
    portfolio = pack["sections"]["portfolio"]
    shadow = pack["sections"]["shadow"]

    fixture_symbols = {a.symbol for a in WF_ASSETS}
    seen_symbols = {p["symbol"] for p in portfolio["positions"]} | set(shadow["verdicts"])
    assert seen_symbols  # the check isn't vacuously true (no symbols found at all)
    assert seen_symbols <= fixture_symbols  # WHITELIST: nothing outside the fixture universe

    assert portfolio["nav_eur"] == pytest.approx(float(WF_TOTAL))
    assert portfolio["cash_eur"] == pytest.approx(float(WF_CASH))
    for p in portfolio["positions"]:
        asset = next(a for a in WF_ASSETS if a.symbol == p["symbol"])
        assert p["value_eur"] == pytest.approx(float(asset.value))  # fixture-derived, not a magic literal

    payload = json.dumps(pack, default=str)  # belt-and-braces (see denylist comment above)
    for sym in _FORBIDDEN_FIXTURE_SYMBOLS:
        assert sym not in payload


async def test_get_report_pack_404_for_unknown_demo_week(monkeypatch) -> None:
    monkeypatch.setattr(reports_router, "session_scope", _boom_session_scope)
    with pytest.raises(NotFound):
        await get_report_pack("1999-W01", _settings(demo_mode=True))


class _FakeScalarResult:
    def __init__(self, value: object) -> None:
        self._value = value

    def scalar_one_or_none(self) -> object:
        return self._value


class _FakeScalarDB:
    def __init__(self, value: object) -> None:
        self._value = value

    async def execute(self, stmt: object) -> _FakeScalarResult:
        return _FakeScalarResult(self._value)


def _wire_scalar_db(monkeypatch, value: object) -> None:
    db = _FakeScalarDB(value)

    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        yield db

    monkeypatch.setattr(reports_router, "session_scope", _fake_session_scope)


def _pack_row(**kw: object) -> ReportPack:
    base = dict(
        report_week="2026-W34", status="complete",
        sections={"exec_summary": {"nav_eur": 1.0}}, sources={"exec_summary": {"engine": "x"}},
        narrated=True, pdf_path="/data/reports/catallax-weekly-2026-W34.pdf",
    )
    base.update(kw)
    return ReportPack(**base)


async def test_get_report_pack_real_mode_straight_row_read(monkeypatch) -> None:
    """Row-shaping only — the `pdf` flag's own file-existence/containment
    logic (`_pdf_available`) is exercised on its own further down, so it's
    stubbed here to decouple the two concerns."""
    row = _pack_row()
    _wire_scalar_db(monkeypatch, row)
    monkeypatch.setattr(reports_router, "_pdf_available", lambda p: True)
    pack = await get_report_pack("2026-W34", _settings(demo_mode=False))
    assert pack == {
        "report_week": "2026-W34", "status": "complete",
        "sections": row.sections, "sources": row.sources,
        "narrated": True, "pdf": True,
    }


async def test_get_report_pack_404_for_missing_row(monkeypatch) -> None:
    _wire_scalar_db(monkeypatch, None)
    with pytest.raises(NotFound):
        await get_report_pack("2099-W01", _settings(demo_mode=False))


async def test_get_report_pack_reaches_db_when_demo_off(monkeypatch) -> None:
    monkeypatch.setattr(reports_router, "session_scope", _boom_session_scope)
    with pytest.raises(_DBTouched):
        await get_report_pack("2026-W34", _settings(demo_mode=False))


# ── /pdf ──────────────────────────────────────────────────────────────────
async def test_get_report_pdf_404_in_demo_mode_before_any_db_read(monkeypatch) -> None:
    monkeypatch.setattr(reports_router, "session_scope", _boom_session_scope)
    with pytest.raises(NotFound):
        await get_report_pdf("2026-W34", _settings(demo_mode=True))


async def test_get_report_pdf_reaches_db_when_demo_off(monkeypatch) -> None:
    monkeypatch.setattr(reports_router, "session_scope", _boom_session_scope)
    with pytest.raises(_DBTouched):
        await get_report_pdf("2026-W34", _settings(demo_mode=False))


async def test_get_report_pdf_404_when_no_row_or_null_pdf_path(monkeypatch) -> None:
    _wire_scalar_db(monkeypatch, None)  # covers both "no row" and "row with pdf_path NULL"
    with pytest.raises(NotFound):
        await get_report_pdf("2026-W34", _settings(demo_mode=False))


async def test_get_report_pdf_success_calls_resolve_and_returns_file_response(monkeypatch, tmp_path) -> None:
    """End-to-end through the REAL `resolve_pdf_path` + on-disk check
    (SEV3 fix): a strict subpath of the base dir that actually exists."""
    pdf_file = tmp_path / "catallax-weekly-2026-W34.pdf"
    pdf_file.write_bytes(b"%PDF-1.4 fake")
    monkeypatch.setattr(reports_router, "_REPORT_PDF_BASE", str(tmp_path))
    _wire_scalar_db(monkeypatch, _pack_row(pdf_path=str(pdf_file)))

    response = await get_report_pdf("2026-W34", _settings(demo_mode=False))
    assert isinstance(response, FileResponse)
    assert response.media_type == "application/pdf"


async def test_get_report_pdf_propagates_containment_failure(monkeypatch) -> None:
    """Defense-in-depth: a `resolve_pdf_path` ValueError (traversal/
    symlink escape) must propagate, never be swallowed into a 404/200 response."""
    _wire_scalar_db(monkeypatch, _pack_row(pdf_path="/etc/passwd"))

    def _fake_resolve(path: str, base: str = "/data/reports") -> str:
        raise ValueError("path escapes base")

    monkeypatch.setattr(reports_router, "resolve_pdf_path", _fake_resolve)
    with pytest.raises(ValueError):
        await get_report_pdf("2026-W34", _settings(demo_mode=False))


async def test_get_report_pdf_404_when_resolved_path_is_the_base_dir_itself(monkeypatch, tmp_path) -> None:
    """`resolve_pdf_path` alone ALLOWS the resolved path to
    equal the base directory itself (not a traversal escape by its own
    definition) — this call site must reject it anyway: a directory is
    never a valid PDF, and must never be a strict subpath requirement."""
    monkeypatch.setattr(reports_router, "_REPORT_PDF_BASE", str(tmp_path))
    _wire_scalar_db(monkeypatch, _pack_row(pdf_path=str(tmp_path)))
    with pytest.raises(NotFound):
        await get_report_pdf("2026-W34", _settings(demo_mode=False))


async def test_get_report_pdf_404_when_pdf_path_set_but_file_missing(monkeypatch, tmp_path) -> None:
    """A stored `pdf_path` that no longer points at a real
    file (deleted, volume not mounted, ...) must 404, never 500."""
    monkeypatch.setattr(reports_router, "_REPORT_PDF_BASE", str(tmp_path))
    missing = tmp_path / "does-not-exist.pdf"
    _wire_scalar_db(monkeypatch, _pack_row(pdf_path=str(missing)))
    with pytest.raises(NotFound):
        await get_report_pdf("2026-W34", _settings(demo_mode=False))


# ── `_resolve_and_check_pdf` / `_pdf_available` — direct unit coverage ─────
def test_resolve_and_check_pdf_rejects_base_dir_itself(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(reports_router, "_REPORT_PDF_BASE", str(tmp_path))
    assert reports_router._resolve_and_check_pdf(str(tmp_path)) is None


def test_resolve_and_check_pdf_rejects_missing_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(reports_router, "_REPORT_PDF_BASE", str(tmp_path))
    assert reports_router._resolve_and_check_pdf(str(tmp_path / "gone.pdf")) is None


def test_resolve_and_check_pdf_accepts_a_real_file_under_base(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr(reports_router, "_REPORT_PDF_BASE", str(tmp_path))
    f = tmp_path / "x.pdf"
    f.write_bytes(b"x")
    assert reports_router._resolve_and_check_pdf(str(f)) == str(f.resolve())


def test_resolve_and_check_pdf_none_for_no_path() -> None:
    assert reports_router._resolve_and_check_pdf(None) is None


def test_pdf_available_swallows_value_error_as_unavailable(monkeypatch) -> None:
    def _boom(path: str | None) -> str | None:
        raise ValueError("escape")

    monkeypatch.setattr(reports_router, "_resolve_and_check_pdf", _boom)
    assert reports_router._pdf_available("/anything") is False


# ── "latest" sentinel (follow-up fix, live design review) ─────────────────
async def test_get_report_pack_latest_resolves_newest_complete_pack(monkeypatch) -> None:
    """Real mode: `report_week="latest"` must resolve via `report_packs`
    itself (newest `status='complete'` row), not `_report_weeks()` — a pack
    can lag the newest advisor week, or that week's own build can still be
    `building`/`failed`."""
    captured: list[object] = []

    class _FakeLatestResult:
        def __init__(self, row: ReportPack | None) -> None:
            self._row = row

        def scalar_one_or_none(self) -> ReportPack | None:
            return self._row

    class _FakeLatestDB:
        def __init__(self, row: ReportPack | None) -> None:
            self._row = row

        async def execute(self, stmt: object) -> _FakeLatestResult:
            captured.append(stmt)
            return _FakeLatestResult(self._row)

    newest = _pack_row(report_week="2026-W35")

    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        yield _FakeLatestDB(newest)

    monkeypatch.setattr(reports_router, "session_scope", _fake_session_scope)
    pack = await get_report_pack("latest", _settings(demo_mode=False))
    assert pack["report_week"] == "2026-W35"
    compiled = str(captured[0])
    assert "status" in compiled.lower()  # the resolver filtered on status='complete'


async def test_get_report_pack_latest_404_when_no_complete_packs(monkeypatch) -> None:
    _wire_scalar_db(monkeypatch, None)
    with pytest.raises(NotFound):
        await get_report_pack("latest", _settings(demo_mode=False))


async def test_get_report_pack_demo_latest_resolves_to_demo_pack(monkeypatch) -> None:
    monkeypatch.setattr(reports_router, "session_scope", _boom_session_scope)
    weeks = (await list_weeks(_settings(demo_mode=True)))["weeks"]
    pack = await get_report_pack("latest", _settings(demo_mode=True))
    assert pack["report_week"] == weeks[0]  # most-recent-first, per `_demo_weeks_and_dates`


async def test_get_report_pdf_latest_resolves_consistently_with_pack(monkeypatch) -> None:
    """pdf's "latest" must resolve the SAME row `/pack` would (shared
    `_resolve_pack_row`) — asserted here via a pack with no `pdf_path` set
    (still 404, proving the resolver ran, not a hardcoded pass-through)."""
    _wire_scalar_db(monkeypatch, _pack_row(report_week="2026-W35", pdf_path=None))
    with pytest.raises(NotFound):
        await get_report_pdf("latest", _settings(demo_mode=False))


async def test_get_report_pdf_demo_latest_still_404s_before_any_db_read(monkeypatch) -> None:
    monkeypatch.setattr(reports_router, "session_scope", _boom_session_scope)
    with pytest.raises(NotFound):
        await get_report_pdf("latest", _settings(demo_mode=True))


# ── /weeks: additive `availability` ────────────────────────────────────────
class _FakeAvailabilityResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def all(self) -> list:
        return self._rows


class _FakeAvailabilityDB:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    async def execute(self, stmt: object) -> _FakeAvailabilityResult:
        return _FakeAvailabilityResult(self._rows)


async def test_pack_availability_straight_row_read(monkeypatch) -> None:
    """`pdf_available` is stubbed here (it has its own dedicated tests above)
    so this test isolates the row-shaping logic: `pack` requires
    `status == "complete"` (SEV3 — a `building`/`failed` row must
    not overclaim `pack: true`), independent of whether a `pdf_path` is set."""
    monkeypatch.setattr(reports_router, "_pdf_available", lambda p: p is not None)
    db = _FakeAvailabilityDB([
        ("2026-W35", "building", None),
        ("2026-W34", "complete", "/data/reports/x.pdf"),
        ("2026-W33", "complete", None),
        ("2026-W32", "failed", "/data/reports/stale.pdf"),
    ])

    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        yield db

    monkeypatch.setattr(reports_router, "session_scope", _fake_session_scope)
    result = await reports_router._pack_availability(
        ["2026-W35", "2026-W34", "2026-W33", "2026-W32", "2026-W31"]
    )
    assert result == {
        "2026-W35": {"pack": False, "pdf": False},  # building — not complete
        "2026-W34": {"pack": True, "pdf": True},
        "2026-W33": {"pack": True, "pdf": False},
        "2026-W32": {"pack": False, "pdf": True},  # failed build, but a pdf_path still resolves
        "2026-W31": {"pack": False, "pdf": False},  # no row at all
    }


async def test_pack_availability_empty_weeks_skips_db(monkeypatch) -> None:
    monkeypatch.setattr(reports_router, "session_scope", _boom_session_scope)
    assert await reports_router._pack_availability([]) == {}


async def test_list_weeks_real_mode_weeks_key_byte_identical_plus_additive_availability(monkeypatch) -> None:
    """Review SEV1 regression guard: a per-week shape change to `weeks`
    broke `ReportArchive.tsx`/`client.ts` consumers in rev 1 — `weeks` must
    stay EXACTLY `{weeks: [str]}`'s old value, `availability` is additive
    only. `_report_weeks`/`_pack_availability` are exercised on their own
    (above); this test only proves `list_weeks`'s wiring/shape."""
    async def _fake_report_weeks() -> list[str]:
        return ["2026-W34", "2026-W33"]

    async def _fake_pack_availability(weeks: list[str]) -> dict[str, dict[str, bool]]:
        assert weeks == ["2026-W34", "2026-W33"]
        return {"2026-W34": {"pack": True, "pdf": True}, "2026-W33": {"pack": False, "pdf": False}}

    monkeypatch.setattr(reports_router, "_report_weeks", _fake_report_weeks)
    monkeypatch.setattr(reports_router, "_pack_availability", _fake_pack_availability)
    result = await list_weeks(_settings(demo_mode=False))
    assert result["weeks"] == ["2026-W34", "2026-W33"]  # OLD shape, byte-identical
    assert result["availability"] == {
        "2026-W34": {"pack": True, "pdf": True}, "2026-W33": {"pack": False, "pdf": False},
    }


async def test_list_weeks_demo_mode_availability_is_synthetic_never_a_pack_read(monkeypatch) -> None:
    monkeypatch.setattr(reports_router, "session_scope", _boom_session_scope)
    result = await list_weeks(_settings(demo_mode=True))
    assert result["weeks"]  # unchanged from the existing demo test above
    assert result["availability"] == {w: {"pack": True, "pdf": False} for w in result["weeks"]}


async def test_accept_rejected_in_demo_mode(monkeypatch) -> None:
    monkeypatch.setattr(reports_router, "session_scope", _boom_session_scope)
    with pytest.raises(DemoModeReadOnly):
        await accept_recommendation(RecommendationAction(note="ok"), 1, _settings(demo_mode=True))


async def test_override_rejected_in_demo_mode(monkeypatch) -> None:
    monkeypatch.setattr(reports_router, "session_scope", _boom_session_scope)
    with pytest.raises(DemoModeReadOnly):
        await override_recommendation(RecommendationAction(note="ok"), 1, _settings(demo_mode=True))


# ── B1: read-route narration_absent_reason (real path, DB rows faked) ────
class _FakeQueryResult:
    def __init__(self, rows: list) -> None:
        self._rows = rows

    def scalars(self) -> "_FakeQueryResult":
        return self

    def all(self) -> list:
        return self._rows

    def scalar_one_or_none(self):
        return self._rows[0] if self._rows else None


class _FakeAssemblyDB:
    """Answers `_assemble_for_week`'s FOUR queries, in the fixed order it
    issues them within one `session_scope()` block: snap_rows, rec_rows,
    history_weeks, history_rows."""

    def __init__(self, snap_rows: list, rec_rows: list, history_weeks: list, history_rows: list) -> None:
        self._answers = [snap_rows, rec_rows, history_weeks, history_rows]
        self._n = 0

    async def execute(self, stmt: object) -> _FakeQueryResult:
        result = _FakeQueryResult(self._answers[self._n])
        self._n += 1
        return result


class _FakeNarrationDB:
    """Answers `_load_narration`'s ONE query, on its OWN separate
    `session_scope()` call."""

    def __init__(self, ai_run_row: AiRun | None) -> None:
        self._row = ai_run_row

    async def execute(self, stmt: object) -> _FakeQueryResult:
        return _FakeQueryResult([self._row] if self._row is not None else [])


def _snapshot_row(report_week: str = "2026-W40") -> PortfolioSnapshot:
    return PortfolioSnapshot(
        report_week=report_week, date=date(2026, 10, 5), symbol="BTC",
        qty=Decimal("1"), value_eur=Decimal("1000"), cost_basis=None,
        cash=Decimal("1000"), nav=Decimal("2000"), regime_composite=None,
        regime_component_set_id=None, created_at=datetime(2026, 10, 5, tzinfo=timezone.utc),
    )


def _wire_assembly(monkeypatch, report_week: str, ai_run_row: AiRun | None) -> None:
    """`session_scope()` is called twice per `_assemble_for_week` run — once
    for its own four-query block, once (separately) inside `_load_narration`
    — so this hands out the two fakes in that fixed call order."""
    snap = _snapshot_row(report_week)
    dbs = [_FakeAssemblyDB([snap], [], [report_week], [snap]), _FakeNarrationDB(ai_run_row)]
    call = {"n": 0}

    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        db = dbs[call["n"]]
        call["n"] += 1
        yield db

    monkeypatch.setattr(reports_router, "session_scope", _fake_session_scope)


async def test_assemble_for_week_narration_absent_reason_not_configured_when_noop(monkeypatch) -> None:
    """B1: the read route must derive the SAME two-state
    reason the live tick uses, not a single hardcoded string — `llm_provider
    == "noop"` and no narration row -> "not configured..."."""
    _wire_assembly(monkeypatch, "2026-W40", ai_run_row=None)
    report = await _assemble_for_week("2026-W40", object(), _settings(llm_provider="noop"))
    assert report["narrated"] is False
    assert report["narration_absent_reason"] == "not configured in this deployment (ai profile not enabled)"


async def test_assemble_for_week_narration_absent_reason_probe_failed_when_configured(monkeypatch) -> None:
    """`llm_provider` configured (not noop) but no `ai_runs` row exists for
    this week -> "probe failed...", distinct from "not configured"."""
    _wire_assembly(monkeypatch, "2026-W40", ai_run_row=None)
    report = await _assemble_for_week("2026-W40", object(), _settings(llm_provider="claude-code"))
    assert report["narrated"] is False
    assert report["narration_absent_reason"] == "narration probe failed (see backend logs)"


# ── demo_mode=False: the real path reaches the DB seam ─────────────────
async def test_list_weeks_reaches_db_when_demo_off(monkeypatch) -> None:
    monkeypatch.setattr(reports_router, "session_scope", _boom_session_scope)
    with pytest.raises(_DBTouched):
        await list_weeks(_settings(demo_mode=False))


async def test_latest_report_reaches_db_when_demo_off(monkeypatch) -> None:
    monkeypatch.setattr(reports_router, "session_scope", _boom_session_scope)
    with pytest.raises(_DBTouched):
        await latest_report(object(), _settings(demo_mode=False))


async def test_get_report_reaches_db_when_demo_off(monkeypatch) -> None:
    monkeypatch.setattr(reports_router, "session_scope", _boom_session_scope)
    with pytest.raises(_DBTouched):
        await get_report("2026-W33", object(), _settings(demo_mode=False))


async def test_accept_reaches_db_when_demo_off(monkeypatch) -> None:
    # the DB session now opens inside `recommendation_lifecycle.transition`,
    # not this router module — patch the seam it actually calls.
    monkeypatch.setattr(recommendation_lifecycle, "session_scope", _boom_session_scope)
    with pytest.raises(_DBTouched):
        await accept_recommendation(RecommendationAction(note="ok"), 1, _settings(demo_mode=False))


# ── append-only transition ──────────────────────────────────────────────
def _proposed_row(**kw: object) -> Recommendation:
    base = dict(
        id=42, rule_id="weight_band_trim", as_of=date(2026, 8, 17), report_week="2026-W34",
        action="sell", sizing=1000.0, input_snapshot={"symbol": "BTC"},
        regime_component_set_id="abc123", status="proposed",
        tax_cost_arithmetic={"benefit_eur": 50.0}, note="fires", prior_id=None,
        created_at=datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc),
    )
    base.update(kw)
    return Recommendation(**base)


class _FakeResult:
    def __init__(self, row: Recommendation | None) -> None:
        self._row = row

    def scalar_one_or_none(self) -> Recommendation | None:
        return self._row


class _FakeTransitionDB:
    def __init__(self, prior: Recommendation | None) -> None:
        self.prior = prior
        self.added: list[Recommendation] = []
        self.last_stmt: object = None

    async def execute(self, stmt: object) -> _FakeResult:
        # fix (recommendation_lifecycle.transition): a second query now
        # probes for an existing child (`prior_id == ...`) before the legal-
        # transition check — none of these fixtures have one, so answer None.
        # `last_stmt` stays the prior-row SELECT (the FOR UPDATE lock) that
        # `_assert_for_update` below checks, not this later probe.
        if stmt.whereclause is not None and stmt.whereclause.left.key == "prior_id":
            return _FakeResult(None)
        self.last_stmt = stmt
        return _FakeResult(self.prior)

    def add(self, obj: Recommendation) -> None:
        self.added.append(obj)

    async def commit(self) -> None:
        pass

    async def refresh(self, obj: Recommendation) -> None:
        pass


def _wire_transition_db(monkeypatch, prior: Recommendation | None) -> _FakeTransitionDB:
    db = _FakeTransitionDB(prior)

    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        yield db

    monkeypatch.setattr(recommendation_lifecycle, "session_scope", _fake_session_scope)
    return db


def _column_snapshot(row: Recommendation) -> dict[str, object]:
    """Every mapped column's current value — item 3: a test that only checks
    `.status` cannot detect a stray write to some OTHER column."""
    return {c.name: getattr(row, c.name) for c in Recommendation.__table__.columns}


def _assert_for_update(stmt: object) -> None:
    compiled = str(stmt.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))
    assert "FOR UPDATE" in compiled.upper()


async def test_accept_inserts_new_row_and_leaves_prior_untouched(monkeypatch) -> None:
    prior = _proposed_row()
    pre = _column_snapshot(prior)  # item 3: full-column snapshot BEFORE the transition
    db = _wire_transition_db(monkeypatch, prior)

    result = await accept_recommendation(RecommendationAction(note="approved, trimming BTC"), 42, _settings())

    _assert_for_update(db.last_stmt)  # item 5: the prior-row SELECT locks the row
    assert len(db.added) == 1
    new_row = db.added[0]
    assert new_row.status == "accepted"
    assert new_row.prior_id == 42
    # item 6: the rule's firing arithmetic (prior.note) carries forward
    # unchanged; the human's reason lands under a distinct input_snapshot key
    assert new_row.note == "fires"
    assert new_row.input_snapshot["human_note"] == "approved, trimming BTC"
    assert new_row.input_snapshot["symbol"] == "BTC"  # the rest of the snapshot survives too
    # copied straight from the prior row — a transition row, not a re-evaluation
    assert new_row.rule_id == "weight_band_trim"
    assert new_row.report_week == "2026-W34"
    assert new_row.action == "sell"
    # item 3: EVERY column on the prior row, not just `.status`, is unchanged
    assert _column_snapshot(prior) == pre
    assert prior.input_snapshot == {"symbol": "BTC"}  # not mutated in place either
    assert result["prior"]["status"] == "proposed"
    assert result["new"]["status"] == "accepted"
    assert result["new"]["prior_id"] == 42


async def test_override_inserts_new_row_with_overridden_status(monkeypatch) -> None:
    prior = _proposed_row(id=7)
    pre = _column_snapshot(prior)
    db = _wire_transition_db(monkeypatch, prior)

    result = await override_recommendation(RecommendationAction(note="not now, cooldown"), 7, _settings())
    _assert_for_update(db.last_stmt)
    assert result["new"]["status"] == "overridden"
    assert result["new"]["prior_id"] == 7
    assert result["new"]["note"] == "fires"  # carried forward, not overwritten
    assert result["new"]["input_snapshot"]["human_note"] == "not now, cooldown"
    assert _column_snapshot(prior) == pre


async def test_accept_not_found_raises(monkeypatch) -> None:
    _wire_transition_db(monkeypatch, None)
    with pytest.raises(NotFound):
        await accept_recommendation(RecommendationAction(note="x"), 999, _settings())


async def test_accept_on_already_accepted_row_rejected(monkeypatch) -> None:
    prior = _proposed_row(status="accepted")
    _wire_transition_db(monkeypatch, prior)
    with pytest.raises(InvalidRequest):
        await accept_recommendation(RecommendationAction(note="x"), 42, _settings())


async def test_accept_on_bare_evaluated_row_rejected(monkeypatch) -> None:
    """Only a 'proposed' row is actionable — a bare audit ('evaluated') row
    never fired, so there's nothing for a human to accept or override."""
    prior = _proposed_row(status="evaluated")
    _wire_transition_db(monkeypatch, prior)
    with pytest.raises(InvalidRequest):
        await override_recommendation(RecommendationAction(note="x"), 42, _settings())


# ── modify / reject / execute + illegal edges ─────────────
@pytest.mark.parametrize("bad_sizing", [0.0, -1.0, float("inf"), float("-inf"), float("nan")])
def test_recommendation_modify_rejects_non_positive_and_non_finite_sizing(bad_sizing) -> None:
    """Security fix: `inf`/`nan`/`<=0` used to pass straight through pydantic
    and get persisted forever into the append-only `input_snapshot`,
    propagating into downstream risk math."""
    with pytest.raises(ValidationError):
        RecommendationModify(note="x", sizing_override=bad_sizing)


@pytest.mark.parametrize("bad_value", [0.0, -1.0, float("inf"), float("nan")])
def test_recommendation_execute_rejects_non_positive_and_non_finite_fill_price_and_quantity(bad_value) -> None:
    with pytest.raises(ValidationError):
        RecommendationExecute(
            note="x", executed_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
            fill_price=bad_value, quantity=1.0, venue="ibkr",
        )
    with pytest.raises(ValidationError):
        RecommendationExecute(
            note="x", executed_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
            fill_price=1.0, quantity=bad_value, venue="ibkr",
        )


async def test_modify_requires_sizing_override_and_preserves_original(monkeypatch) -> None:
    prior = _proposed_row(id=11, sizing=1000.0)
    db = _wire_transition_db(monkeypatch, prior)

    result = await modify_recommendation(
        RecommendationModify(note="trimming less than the rule proposed", sizing_override=600.0), 11, _settings(),
    )

    _assert_for_update(db.last_stmt)
    new_row = db.added[0]
    assert new_row.status == "modified"
    assert new_row.sizing == 600.0
    assert new_row.input_snapshot["sizing_proposed"] == 1000.0  # original preserved
    assert result["new"]["sizing"] == 600.0


async def test_reject_persists_overridden_with_human_action_reject_and_feeds_cooldown(monkeypatch) -> None:
    """F1, proven not assumed: `/reject` writes `status == 'overridden'` with
    `human_action == 'reject'`, and a subsequent read through the frozen
    `advisor_report._load_overrides` sees that row and returns the exact
    cooldown key `advisor._cooldown_check` consumes — the loop is fed, not
    silently disabled by some other status string."""
    prior = _proposed_row(
        id=55, rule_id="weight_band_trim", input_snapshot={"symbol": "ETH"},
        tax_cost_arithmetic={"benefit_eur": 77.0}, report_week="2026-W35", as_of=date(2026, 8, 24),
    )
    db = _wire_transition_db(monkeypatch, prior)

    result = await reject_recommendation(RecommendationAction(note="not yet"), 55, _settings())

    new_row = db.added[0]
    assert new_row.status == "overridden"
    assert new_row.input_snapshot["human_action"] == "reject"
    assert result["new"]["status"] == "overridden"

    class _OverridesResult:
        def __init__(self, rows: list) -> None:
            self._rows = rows

        def scalars(self):
            return self

        def all(self):
            return self._rows

    class _OverridesDB:
        def __init__(self, rows: list) -> None:
            self._rows = rows

        async def execute(self, stmt):
            return _OverridesResult(self._rows)

    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        yield _OverridesDB([new_row])

    monkeypatch.setattr(advisor_report_module, "session_scope", _fake_session_scope)
    overrides = await advisor_report_module._load_overrides(date(2026, 8, 31))
    assert overrides == {"weight_band_trim:ETH": ("2026-W35", 77.0)}


async def test_execute_requires_all_four_fields_and_stores_under_execution_key(monkeypatch) -> None:
    prior = _proposed_row(id=61, status="accepted")
    db = _wire_transition_db(monkeypatch, prior)

    result = await execute_recommendation(
        RecommendationExecute(
            note="filled", executed_at=datetime(2026, 8, 25, 14, 30, tzinfo=timezone.utc),
            fill_price=101.5, quantity=3.0, venue="ibkr",
        ),
        61, _settings(),
    )

    new_row = db.added[0]
    assert new_row.status == "executed"
    assert new_row.input_snapshot["execution"] == {
        "executed_at": "2026-08-25T14:30:00+00:00", "fill_price": 101.5, "quantity": 3.0, "venue": "ibkr",
    }
    assert result["new"]["status"] == "executed"


async def test_accepted_to_accepted_is_illegal(monkeypatch) -> None:
    prior = _proposed_row(id=71, status="accepted")
    _wire_transition_db(monkeypatch, prior)
    with pytest.raises(InvalidRequest) as exc:
        await accept_recommendation(RecommendationAction(note="x"), 71, _settings())
    assert "accepted" in str(exc.value)


async def test_executed_to_anything_is_illegal(monkeypatch) -> None:
    prior = _proposed_row(id=72, status="executed")
    _wire_transition_db(monkeypatch, prior)
    with pytest.raises(InvalidRequest) as exc:
        await accept_recommendation(RecommendationAction(note="x"), 72, _settings())
    assert "executed" in str(exc.value)


async def test_overridden_to_executed_is_illegal(monkeypatch) -> None:
    prior = _proposed_row(id=73, status="overridden")
    _wire_transition_db(monkeypatch, prior)
    with pytest.raises(InvalidRequest) as exc:
        await execute_recommendation(
            RecommendationExecute(
                note="x", executed_at=datetime(2026, 8, 25, tzinfo=timezone.utc),
                fill_price=1.0, quantity=1.0, venue="x",
            ),
            73, _settings(),
        )
    assert "overridden" in str(exc.value) and "executed" in str(exc.value)


# ── `_current_recommendations`: collapse a transition chain to its leaf ──
def test_current_recommendations_drops_superseded_rows() -> None:
    proposed = _proposed_row(id=1, status="proposed")
    accepted = _proposed_row(id=2, status="accepted", prior_id=1)
    other_evaluated = _proposed_row(id=3, status="evaluated", prior_id=None, rule_id="cash_floor_band")
    current = _current_recommendations([proposed, accepted, other_evaluated])
    assert {r.id for r in current} == {2, 3}  # row 1 is row 2's prior -> dropped


# ── scorecard: pure math ────────────────────────────────────────────────
def test_modified_dietz_simple_no_cash_flow() -> None:
    assert modified_dietz_return(100.0, 110.0) == pytest.approx(0.10)


def test_modified_dietz_zero_or_negative_denominator_is_none() -> None:
    assert modified_dietz_return(0.0, 50.0) is None
    assert modified_dietz_return(-10.0, 50.0, cash_flow=0.0) is None


def test_chain_link_compounds_geometrically() -> None:
    assert chain_link([0.10, -0.05]) == pytest.approx(1.10 * 0.95 - 1.0)
    assert chain_link([]) is None


def test_sign_test_floor_matches_2_pow_1_minus_n() -> None:
    assert sign_test_two_sided_p_floor(0) is None
    assert sign_test_two_sided_p_floor(1) == pytest.approx(1.0)
    assert sign_test_two_sided_p_floor(2) == pytest.approx(0.5)
    assert sign_test_two_sided_p_floor(5) == pytest.approx(2 ** -4)


def _snap(week: str, symbol: str, qty: float, value_eur: float, nav: float, cash: float) -> dict:
    return {"report_week": week, "symbol": symbol, "qty": qty, "value_eur": value_eur, "nav": nav, "cash": cash}


def test_compute_scorecard_depth_zero_is_explicit_empty_state() -> None:
    sc = compute_scorecard([])
    assert sc["empty"] is True
    assert sc["depth_weeks"] == 0
    assert sc["n_effective"] == 0
    assert sc["p_floor_two_sided"] is None
    assert sc["actual_twr"] is None
    assert "convention" in sc


def test_compute_scorecard_depth_one_insufficient_history() -> None:
    history = [_snap("2026-W33", "BTC", 1.0, 90_000.0, 100_000.0, 10_000.0)]
    sc = compute_scorecard(history)
    assert sc["empty"] is False
    assert sc["depth_weeks"] == 1
    assert sc["n_effective"] == 0
    assert sc["p_floor_two_sided"] is None
    assert sc["actual_twr"] is None


def test_compute_scorecard_no_trades_arms_coincide() -> None:
    """Quantities unchanged across the whole window -> the do-nothing arm
    IS the actual arm (same nav path), n_effective=0, and the payload says so."""
    history = [
        _snap("2026-W33", "BTC", 1.0, 90_000.0, 100_000.0, 10_000.0),
        _snap("2026-W34", "BTC", 1.0, 99_000.0, 109_000.0, 10_000.0),
    ]
    sc = compute_scorecard(history)
    assert sc["depth_weeks"] == 2
    assert sc["n_effective"] == 0
    assert sc["p_floor_two_sided"] is None  # item 1: no divergence -> no floor to report
    assert sc["arms_coincide"] is True
    assert sc["actual_twr"] == pytest.approx(sc["do_nothing_twr"])
    assert sc["actual_twr"] == pytest.approx((109_000.0 - 100_000.0) / 100_000.0)
    assert sc["note"] is not None
    assert sc["meta"]["n_actual"] == 1
    assert sc["meta"]["n_donothing"] == 1


def test_compute_scorecard_trade_diverges_the_arms() -> None:
    """Week 2 trims BTC down (a trade) while price is flat -> actual nav
    drops (proceeds not modeled as reinvested here) but the frozen
    do-nothing basket (still 1.0 BTC) revalues at the same flat price -> the
    arms diverge, `arms_coincide` is False, and the one divergent sub-period
    counts toward n_effective."""
    history = [
        _snap("2026-W33", "BTC", 1.0, 90_000.0, 100_000.0, 10_000.0),
        _snap("2026-W34", "BTC", 0.5, 45_000.0, 55_000.0, 10_000.0),
    ]
    sc = compute_scorecard(history)
    assert sc["arms_coincide"] is False
    assert sc["n_effective"] == 1
    assert sc["p_floor_two_sided"] == pytest.approx(1.0)  # 2**(1-1)
    assert sc["actual_twr"] != pytest.approx(sc["do_nothing_twr"])
    # do-nothing arm: still 1.0 BTC at week 2's implied price (45_000/0.5=90_000, unchanged)
    assert sc["do_nothing_twr"] == pytest.approx(0.0)


def test_compute_scorecard_cash_deposit_with_unchanged_quantities_diverges_arms() -> None:
    """CRITICAL 2 regression: quantities are IDENTICAL across both weeks (no
    trade at all) but week 2's cash jumps by a pure deposit -> actual nav
    rises by the deposit on top of the (flat) price move, while the frozen
    do-nothing arm (frozen cash AND quantities) doesn't see the deposit at
    all -> the two TWRs differ, so a quantity-only coincidence check would
    have wrongly reported `arms_coincide=True` here."""
    history = [
        _snap("2026-W33", "BTC", 1.0, 90_000.0, 100_000.0, 10_000.0),
        # price flat (still 90_000 for 1.0 BTC) but a 5_000 EUR cash deposit
        # landed -> actual nav = 90_000 + 15_000 = 105_000
        _snap("2026-W34", "BTC", 1.0, 90_000.0, 105_000.0, 15_000.0),
    ]
    sc = compute_scorecard(history)
    assert sc["arms_coincide"] is False
    assert sc["n_effective"] == 1
    assert sc["do_nothing_twr"] == pytest.approx(0.0)  # frozen basket, flat price -> no return
    assert sc["actual_twr"] == pytest.approx((105_000.0 - 100_000.0) / 100_000.0)  # deposit read as "return"
    assert sc["note"] is None  # not claimed identical


def test_compute_scorecard_long_dormant_run_never_inflates_n_effective() -> None:
    """CRITICAL 1 regression: 30 consecutive identical weeks (nothing ever
    diverges) must print n_effective=0 / p_floor=None, NOT n=29 paired with
    a vanishingly small p_floor as if 29 independent comparisons had fired."""
    history = []
    for i in range(30):
        history.append(_snap(f"2026-W{i + 1:02d}", "BTC", 1.0, 90_000.0, 100_000.0, 10_000.0))
    sc = compute_scorecard(history)
    assert sc["depth_weeks"] == 30
    assert sc["n_effective"] == 0
    assert sc["p_floor_two_sided"] is None
    assert sc["arms_coincide"] is True
