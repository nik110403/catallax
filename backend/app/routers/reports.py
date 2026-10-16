"""Weekly Advisor reports router. All under /api/v1/reports.

Three surfaces:
  1. List/get — reassembles a report on read via `advisor_report.assemble_report`
     over rows re-read from `portfolio_snapshots`/`recommendations` for the
     requested week; there is no `reports` table. The two-arm performance
     scorecard (`services/scorecard.compute_scorecard`) is computed fresh each
     read over the full snapshot history up to and including that week.
  2. Act on a recommendation — accept/override NEVER updates a row: it INSERTs
     a new `Recommendation` whose `prior_id` points at the row it supersedes,
     leaving the prior row byte-for-byte unchanged (append-only, proven in
     `tests/test_reports_router.py`). Only a still-`proposed` row is actionable.
     The prior row's `note` (the rule's firing arithmetic, e.g. "cash short of
     the €X target by €Y") is carried forward onto the new row unchanged; the
     human's reason for the decision is stored separately under
     `input_snapshot["human_note"]` so neither is lost. The prior-row SELECT takes `FOR UPDATE` — this is a
     single-writer deployment today, but two concurrent accept/override calls
     on the same recommendation would otherwise both read `status='proposed'`
     and each insert a row pointing at the same `prior_id`, forking the chain.
  3. DEMO_MODE: reads never touch the real DB (`_demo_weeks`/`_demo_report` —
     the `make_wealthfolio`-style factory seam; a full four-section synthetic
     report, reassembled fresh on every read from `services/demo.py`
     fixtures); mutations are rejected outright
     (`DemoModeReadOnly`, the same 403 shape as signals/backtests/lab/algo).
"""
from __future__ import annotations

import os
import statistics
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, Path
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import select

from app.config import Settings, get_settings
from app.db import session_scope
from app.errors import DemoModeReadOnly, InvalidRequest, NotFound
from app.models import AiRun, PortfolioSnapshot, ReportPack, Recommendation
from app.redis import get_redis
from app.services import recommendation_lifecycle
from app.services.advisor import make_advisor
from app.services.advisor_report import assemble_report, iso_week, narration_absent_reason_for_reassembly
from app.services.decisions import DecisionProjection
from app.services.demo import WF_ASSETS, WF_INVESTMENTS, wf_networth_series, wf_position_history
from app.services.report_pdf import resolve_pdf_path
from app.services.scorecard import HISTORY_MAX_WEEKS, compute_scorecard
from app.services.shadow import NATURE, _verdict_forensic, _verdict_patterns, _verdict_technicals
from app.services.wealthfolio import Holdings, HoldingAsset

router = APIRouter(prefix="/reports", tags=["reports"])


# ── DEMO_MODE seam — the `make_wealthfolio` factory pattern: every read
# handler below routes through here FIRST, never falling through to a real
# DB read. A full FOUR-section synthetic report, reassembled
# fresh on every read (there is no persisted demo state — the tick is a
# total no-op under DEMO_MODE, see `advisor_report.generate_weekly_report`),
# reusing the real `assemble_report`/`compute_scorecard`/`AdvisorEngine`
# code paths over synthetic-fixture inputs rather than hand-writing a
# parallel payload shape. ───────────────────────────────────────────────
_DEMO_HISTORY_WEEKS = 8  # short, deterministic trailing window — a demo needs a shape, not 2yrs of history
_DEMO_ASSET_CLASS = {a.symbol: a.asset_class for a in WF_ASSETS}  # fixed demo persona metadata, never varies by week


def _demo_weeks_and_dates() -> list[tuple[str, date]]:
    """(report_week, date) pairs, most-recent-first, most-recent = today's
    ISO week — deterministic given `date.today()`, mirroring
    `services/demo.wf_networth_series`'s own date-relative-to-today
    convention so the demo always looks current."""
    today = date.today()
    return [(iso_week(today - timedelta(weeks=i)), today - timedelta(weeks=i)) for i in range(_DEMO_HISTORY_WEEKS)]


def _demo_weeks() -> list[str]:
    return [wk for wk, _ in _demo_weeks_and_dates()]


def _demo_snapshot_history() -> list[dict[str, Any]]:
    """One row per symbol per demo week, shaped exactly like `_snapshot_dict`
    output — sampled from `wf_networth_series` (the seeded random walk,
    pinned to `WF_TOTAL` today) at each week's date, with every asset's
    CONSTANT share of that week's investments figure (no per-symbol daily
    price series exists in the fixture). Fed straight into the real
    `compute_scorecard()`: since asset weights never move week to week here
    (no trim has "happened" against this synthetic history), the two arms
    stay close/identical — an honest demo of the near-zero-firing-rate
    steady state `scorecard.py` already documents, not a fabricated
    divergence."""
    weeks = _demo_weeks_and_dates()
    series = {d: (total, cash, investments) for d, total, cash, investments in wf_networth_series(months=3)}
    rows: list[dict[str, Any]] = []
    for wk, d in weeks:
        point = series.get(d)
        if point is None:  # outside the fixture's generated window — shouldn't happen at 3 months
            continue
        total, cash, investments = point
        for a in WF_ASSETS:
            frac = float(a.value / WF_INVESTMENTS)
            rows.append({
                "report_week": wk, "symbol": a.symbol, "qty": float(a.quantity),
                "value_eur": round(float(investments) * frac, 2),
                "nav": float(total), "cash": float(cash),
            })
    return rows


class _WeekWF:
    """DEMO_MODE-only wf bridge for exactly ONE `evaluate()` call — wraps a
    `Holdings` snapshot ALREADY sampled for a specific demo week (see
    `_week_holdings` below), so `AdvisorEngine.evaluate()`'s own
    `input_snapshot["nav_eur"]`/`["cash_eur"]` figures agree with that same
    week's `what_happened` header instead of always reading today's fixture
    — no live egress, `.holdings()` returns the object it
    was constructed with."""

    def __init__(self, holdings: Holdings) -> None:
        self._holdings = holdings

    async def holdings(self) -> Holdings:
        return self._holdings


def _week_holdings(snapshots: list[dict[str, Any]], nav: float, cash: float) -> Holdings:
    """Builds a `Holdings` object for one demo week straight from that
    week's sampled snapshot rows (symbol/qty/value_eur) plus the fixed demo
    persona's asset-class metadata (`_DEMO_ASSET_CLASS`, never varies by
    week) — the same shape `AdvisorEngine` reads off the real bridge."""
    by_asset = [
        HoldingAsset(
            symbol=s["symbol"], name=s["symbol"],
            asset_class=_DEMO_ASSET_CLASS.get(s["symbol"], "other"),
            quantity=Decimal(str(s["qty"])), value=Decimal(str(s["value_eur"])),
        )
        for s in snapshots
    ]
    investments = sum((a.value for a in by_asset), Decimal("0"))
    return Holdings(
        base="EUR", investments=investments, cash=Decimal(str(cash)),
        total=Decimal(str(nav)), by_asset=by_asset, by_account=[],
    )


async def _demo_report(report_week: str, redis: Redis, settings: Settings) -> dict[str, Any] | None:
    weeks = _demo_weeks_and_dates()
    if report_week == "latest":
        target_week, as_of = weeks[0]
    else:
        match = next((w for w in weeks if w[0] == report_week), None)
        if match is None:
            return None
        target_week, as_of = match

    history = _demo_snapshot_history()
    snapshots = [r for r in history if r["report_week"] == target_week]
    if not snapshots:
        return None
    nav, cash = snapshots[0]["nav"], snapshots[0]["cash"]

    # `make_advisor` routes demo mode to zero-I/O synthetic risk/regime
    # bridges — this evaluate() call performs no live egress at all.
    # `wf` is THIS week's sampled holdings, not the demo
    # bridge's always-current fixture, so evaluate()'s own nav/cash figures
    # agree with the header above for every archived week, not just "latest".
    advisor = make_advisor(redis, settings, wf=_WeekWF(_week_holdings(snapshots, nav, cash)))
    rec_rows = await advisor.evaluate(as_of, target_week, overrides=None)
    recommendations = [
        {**r, "id": i + 1, "prior_id": None, "as_of": r["as_of"].isoformat()}
        for i, r in enumerate(rec_rows)
    ]
    # Bounded to weeks up to and including the one being viewed (review item
    # 2) — the real read route (`_assemble_for_week`) never scores weeks
    # that "hadn't happened yet" relative to the requested week either.
    scored_history = [r for r in history if r["report_week"] <= target_week]
    performance = compute_scorecard(scored_history)

    return assemble_report(
        target_week, nav, cash, snapshots, recommendations, advisor.rule_status(),
        None,  # regime_state's human label isn't persisted in the real path either
        None, "demo mode: no narration probe is attempted (synthetic report, no real LLM call)",
        performance=performance,
    )


# ── DEMO_MODE — report-PACK read surface. Binding: the ONLY
# engines with demo seams are wealthfolio/ledger/advisor — RiskEngine,
# MarketResearchEngine, ResearchEngine, IntelService have NONE, so a demo pack
# may never call them. Chapters 3 (market) / 4 (holdings) / 5 (quant) are
# therefore always absent; chapter 1 (exec_summary) is likewise left absent —
# only chapters 2/6/7 can be constructed from existing demo
# seams, and building an exec-summary shape would mean either reaching into
# frozen advisor_report internals or fabricating a narrative the spec does not
# define (left absent rather than guessed).
# Chapter 2 (portfolio) copies report_pack.py's own weight/WoW arithmetic
# pattern over the demo fixture history rather than importing RiskEngine (no
# demo seam) or touching that frozen-by-ownership module. Chapter 6 (shadow)
# reuses ShadowEngine's pure, DB-free verdict-mapping functions over
# synthetic per-asset readings derived from the existing demo fixtures — no
# scoreboard/grading (that needs shadow_verdicts history + live price
# fetches, neither available in demo). ──────────────────────────────────
def _demo_pack_portfolio(target_week: str, history: list[dict[str, Any]], snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    weeks_sorted = sorted({r["report_week"] for r in history})
    idx = weeks_sorted.index(target_week)
    prior_week = weeks_sorted[idx - 1] if idx > 0 else None
    prior_by_symbol = {r["symbol"]: r for r in history if prior_week and r["report_week"] == prior_week}
    prior_nav = next((r["nav"] for r in history if prior_week and r["report_week"] == prior_week), None)
    nav, cash = snapshots[0]["nav"], snapshots[0]["cash"]

    positions = []
    for s in snapshots:
        weight_pct = round(s["value_eur"] / nav * 100, 4) if nav else 0.0
        p = prior_by_symbol.get(s["symbol"])
        wow_value_delta_eur = round(s["value_eur"] - p["value_eur"], 2) if p else None
        wow_weight_delta_pct = None
        if p and prior_nav:
            wow_weight_delta_pct = round(weight_pct - (p["value_eur"] / prior_nav * 100), 4)
        positions.append({
            "symbol": s["symbol"], "qty": s["qty"], "value_eur": s["value_eur"],
            "weight_pct": weight_pct, "wow_value_delta_eur": wow_value_delta_eur,
            "wow_weight_delta_pct": wow_weight_delta_pct,
        })

    scored_history = [r for r in history if r["report_week"] <= target_week]
    return {
        "engine": "demo fixtures (RiskEngine has no demo seam — var_cvar/coverage absent)",
        "how_to_read": (
            "Positions/weights/WoW deltas straight from the demo persona's synthetic "
            "snapshot history, same arithmetic as the live portfolio chapter. VaR/CVaR "
            "and risk coverage are absent in demo mode: RiskEngine has no demo seam "
            "and must never be called with live Settings under "
            "DEMO_MODE. Scorecard is the same real two-arm TWR comparison."
        ),
        "report_week": target_week, "prior_report_week": prior_week,
        "nav_eur": nav, "cash_eur": cash,
        "positions": positions,
        "var_cvar": {"absent": True, "reason": "demo mode: RiskEngine has no demo seam"},
        "coverage": None,
        "scorecard": compute_scorecard(scored_history),
    }


def _demo_shadow_readings() -> dict[str, dict[str, dict[str, Any]]]:
    """Synthetic per-asset technicals/patterns/forensic readings, deterministic
    from the existing `WF_ASSETS`/`wf_position_history` demo fixtures (the
    demo seam) — pure in-process computation, no DB read, no history/price
    fetch of any kind."""
    readings: dict[str, dict[str, dict[str, Any]]] = {"technicals": {}, "patterns": {}, "forensic": {}}
    for asset in WF_ASSETS:
        history = wf_position_history(asset.symbol, months=13) or []
        values = [float(v) for _, _, v, _ in history]
        sma50 = statistics.fmean(values[-50:]) if len(values) >= 50 else None
        sma200 = statistics.fmean(values[-200:]) if len(values) >= 200 else None
        window = values[-15:]
        deltas = [window[i] - window[i - 1] for i in range(1, len(window))]
        gains = sum(d for d in deltas if d > 0)
        losses = sum(-d for d in deltas if d < 0)
        rsi = 50.0 if not deltas else (100.0 if losses == 0 else 100.0 - 100.0 / (1 + gains / losses))
        readings["technicals"][asset.symbol] = {"sma50": sma50, "sma200": sma200, "rsi_14": rsi}
        pnl = float(asset.value - asset.cost_basis)
        readings["patterns"][asset.symbol] = {"score": 1 if pnl > 0 else (-1 if pnl < 0 else 0)}
        piotroski = 3.0 + (sum(ord(c) for c in asset.symbol) % 7)  # deterministic 3..9, no live filings
        readings["forensic"][asset.symbol] = {
            "altman_verdict": None, "beneish_verdict": None, "piotroski_score": piotroski,
        }
    return readings


def _verdict_dict(verdict: tuple[str, int | None, str]) -> dict[str, Any]:
    action, direction, rationale = verdict
    return {"verdict": action, "direction": direction, "rationale": rationale}


def _demo_pack_shadow(settings: Settings) -> dict[str, Any]:
    readings = _demo_shadow_readings()
    verdicts: dict[str, dict[str, Any]] = {}
    for symbol in readings["technicals"]:
        verdicts[symbol] = {
            "technicals": _verdict_dict(_verdict_technicals(
                readings["technicals"][symbol],
                settings.report_pack_shadow_rsi_overbought, settings.report_pack_shadow_rsi_oversold,
            )),
            "patterns": _verdict_dict(_verdict_patterns(readings["patterns"][symbol])),
            "forensic": _verdict_dict(_verdict_forensic(
                readings["forensic"][symbol],
                settings.report_pack_shadow_piotroski_weak, settings.report_pack_shadow_piotroski_strong,
            )),
        }
    ungraded_reason = "demo mode: no shadow_verdicts history and no live price feed for grading"
    return {
        "engine": "shadow-verdict-engine-v1 (demo: pure verdict mapping only, no grading)",
        "how_to_read": (
            "Deterministic tool -> direction mapping over the demo persona's own fixture "
            "readings, reusing the real ShadowEngine per-tool mapping functions verbatim. "
            "Never a graded scoreboard: grading needs shadow_verdicts history and live "
            "forward price fetches, neither of which exist under DEMO_MODE."
        ),
        "nature": NATURE,
        "verdicts": verdicts,
        "tools": {
            tool: {"graded": False, "reason": ungraded_reason}
            for tool in ("technicals", "patterns", "forensic", "factor", "signals")
        },
    }


_DEMO_METHODOLOGY_TOOLS = [
    {"tool": "risk.RiskEngine", "status": "not exercised in demo mode — no demo seam"},
    {"tool": "market_research.MarketResearchEngine", "status": "not exercised in demo mode — no demo seam"},
    {"tool": "research.ResearchEngine (forensic scores)", "status": "not exercised in demo mode — no demo seam"},
    {"tool": "signals.SignalEngine", "status": "not exercised in demo mode — no demo seam"},
    {
        "tool": "shadow.ShadowEngine",
        "status": (
            "verdict mapping only in demo mode (pure functions over synthetic fixture "
            "readings); grading needs shadow_verdicts history + live prices, neither "
            "available in demo"
        ),
    },
]


def _demo_pack_methodology() -> dict[str, Any]:
    return {
        "engine": "static",
        "how_to_read": (
            "Reference appendix: which analytical tools this pack normally draws on, and "
            "why each is unavailable (or partially available) under DEMO_MODE."
        ),
        "version": "demo-v1",
        "tools": _DEMO_METHODOLOGY_TOOLS,
    }


async def _demo_pack(report_week: str, settings: Settings) -> dict[str, Any] | None:
    weeks = _demo_weeks_and_dates()
    if report_week == "latest":
        target_week, _ = weeks[0]
    else:
        match = next((w for w in weeks if w[0] == report_week), None)
        if match is None:
            return None
        target_week, _ = match

    history = _demo_snapshot_history()
    snapshots = [r for r in history if r["report_week"] == target_week]
    if not snapshots:
        return None

    absent = lambda reason: {"absent": True, "reason": reason}  # noqa: E731
    sections = {
        "exec_summary": absent("demo mode"),
        "portfolio": _demo_pack_portfolio(target_week, history, snapshots),
        "market": absent("demo mode"),
        "holdings": absent("demo mode"),
        "quant": absent("demo mode"),
        "shadow": _demo_pack_shadow(settings),
        "methodology": _demo_pack_methodology(),
    }
    sources = {
        key: {"engine": "demo fixtures" if not v.get("absent") else "n/a", "stale": False, "error": None}
        for key, v in sections.items()
    }
    return {
        "report_week": target_week, "status": "complete",
        "sections": sections, "sources": sources,
        "narrated": False, "pdf": False,
    }


# ── row -> dict shaping ────────────────────────────────────────────────
def _snapshot_dict(row: PortfolioSnapshot) -> dict[str, Any]:
    return {
        "report_week": row.report_week, "symbol": row.symbol,
        "qty": float(row.qty), "value_eur": float(row.value_eur),
        "nav": float(row.nav), "cash": float(row.cash),
    }


def _rec_dict(row: Recommendation) -> dict[str, Any]:
    return {
        "id": row.id, "rule_id": row.rule_id, "as_of": row.as_of.isoformat(),
        "report_week": row.report_week, "action": row.action, "sizing": row.sizing,
        "status": row.status, "note": row.note, "prior_id": row.prior_id,
        "input_snapshot": row.input_snapshot, "tax_cost_arithmetic": row.tax_cost_arithmetic,
    }


def _current_recommendations(rows: list[Recommendation]) -> list[Recommendation]:
    """Collapse an append-only chain down to its current state per rule: a
    row superseded by a later accept/override transition (i.e. it IS some
    other row's `prior_id`) is dropped, keeping only each chain's leaf."""
    superseded = {r.prior_id for r in rows if r.prior_id is not None}
    return [r for r in rows if r.id not in superseded]


# ── DB reads ─────────────────────────────────────────────────────────────
async def _report_weeks() -> list[str]:
    async with session_scope() as db:
        rec = (await db.execute(select(Recommendation.report_week).distinct())).scalars().all()
        snap = (await db.execute(select(PortfolioSnapshot.report_week).distinct())).scalars().all()
    return sorted({*rec, *snap}, reverse=True)


# /data/reports: the named volume the PDF pipeline writes into and the
# fastapi-backend mounts read-only — the one and only base directory a
# `report_packs.pdf_path` value may ever resolve under.
_REPORT_PDF_BASE = "/data/reports"


def _resolve_and_check_pdf(pdf_path: str | None) -> str | None:
    """Resolves + validates a stored `pdf_path` before it's ever handed to
    `FileResponse` — returns the real path if (and only if) it's usable
    right now, else `None`. Two SEV3 rulings enforced here, at the
    call site (report_pdf.py's `resolve_pdf_path` is owned by a separate module):
      1. Containment edge: `resolve_pdf_path` alone ALLOWS the resolved path
         to equal the base directory ITSELF (not a traversal escape by its
         own definition, but never a valid PDF either) — rejected here.
      2. Staleness: the worker-written value can go stale (file deleted,
         volume not mounted, ...) — checked with a cheap `os.path.isfile`,
         not assumed from the DB row alone.
    A genuine containment violation (traversal/symlink escape) still raises
    `ValueError` and propagates uncaught to the caller — that's an invariant
    violation worth a loud 500, never silently downgraded to "not found"."""
    if not pdf_path:
        return None
    real_path = resolve_pdf_path(pdf_path, _REPORT_PDF_BASE)
    if real_path == os.path.realpath(_REPORT_PDF_BASE):
        return None
    if not os.path.isfile(real_path):
        return None
    return real_path


def _pdf_available(pdf_path: str | None) -> bool:
    """Same checks as `_resolve_and_check_pdf`, but for `availability`/`pack`
    read surfaces that must never 500 on one bad row — a stray `ValueError`
    (traversal/symlink escape on an already-persisted row) degrades to
    "not available" here rather than sinking the whole `/weeks` response."""
    try:
        return _resolve_and_check_pdf(pdf_path) is not None
    except ValueError:
        return False


async def _pack_availability(weeks: list[str]) -> dict[str, dict[str, bool]]:
    """Real-mode `availability` for `/weeks`: a straight
    `report_packs` read, scoped to the byte-identical `weeks` list so the
    existing key's shape/ordering is never touched by this addition.
    `pack` is true only for a `status == "complete"` row (SEV3 a
    `building`/`failed` row previously overclaimed pack availability);
    `pdf` additionally requires the file to actually exist on disk right now."""
    if not weeks:
        return {}
    async with session_scope() as db:
        rows = (await db.execute(
            select(ReportPack.report_week, ReportPack.status, ReportPack.pdf_path)
            .where(ReportPack.report_week.in_(weeks))
        )).all()
    by_week = {week: (status, pdf_path) for week, status, pdf_path in rows}
    return {
        w: {
            "pack": by_week.get(w, (None, None))[0] == "complete",
            "pdf": _pdf_available(by_week.get(w, (None, None))[1]),
        }
        for w in weeks
    }


async def _load_narration(report_week: str) -> dict[str, Any] | None:
    """Reads the `ai_runs` row the report worker persisted for this week (`kind="narrate"`,
    `config={"report_week": ...}`) — no separate narration store exists."""
    async with session_scope() as db:
        row = (await db.execute(
            select(AiRun)
            .where(AiRun.kind == "narrate", AiRun.config["report_week"].as_string() == report_week)
            .order_by(AiRun.created_at.desc())
            .limit(1)
        )).scalar_one_or_none()
    if row is None or not row.result:
        return None
    narration = dict(row.result)
    narration.pop("facts", None)  # facts are the LLM input, not the narration output
    return narration


async def _assemble_for_week(report_week: str, redis: Redis, settings: Settings) -> dict[str, Any] | None:
    async with session_scope() as db:
        snap_rows = (await db.execute(
            select(PortfolioSnapshot).where(PortfolioSnapshot.report_week == report_week)
            .order_by(PortfolioSnapshot.symbol)
        )).scalars().all()
        rec_rows = (await db.execute(
            select(Recommendation).where(Recommendation.report_week == report_week)
            .order_by(Recommendation.created_at)
        )).scalars().all()
        # Bounded to the trailing `HISTORY_MAX_WEEKS` distinct weeks — an unbounded `<=` scan would grow without limit as
        # snapshot history accumulates; the cap is stated on the scorecard's
        # own `CONVENTION` string so the payload documents its own scope.
        history_weeks = (await db.execute(
            select(PortfolioSnapshot.report_week).distinct()
            .where(PortfolioSnapshot.report_week <= report_week)
            .order_by(PortfolioSnapshot.report_week.desc())
            .limit(HISTORY_MAX_WEEKS)
        )).scalars().all()
        history_rows = (await db.execute(
            select(PortfolioSnapshot).where(PortfolioSnapshot.report_week.in_(history_weeks))
            .order_by(PortfolioSnapshot.report_week, PortfolioSnapshot.symbol)
        )).scalars().all()
    if not snap_rows and not rec_rows:
        return None

    snapshots = [_snapshot_dict(r) for r in snap_rows]
    recommendations = [_rec_dict(r) for r in _current_recommendations(rec_rows)]
    # nav/cash are denormalized onto every symbol row of a week (all written
    # from the same `Holdings` read) — any row carries the account figures.
    # An all-cash week left NO snapshot rows at all (the advisor never persists a
    # symbol-less week — the snapshots INSERT is a no-op on an empty
    # `by_asset`), so nav/cash for that edge case are unrecoverable here.
    # DEBT: the exporter has no account-level "week nav/cash" row independent
    # of a held symbol. Revive trigger: an all-cash week's report visibly
    # showing nav_eur=0 gets flagged by a real user.
    nav = float(snap_rows[0].nav) if snap_rows else 0.0
    cash = float(snap_rows[0].cash) if snap_rows else 0.0

    rule_status = make_advisor(redis, settings).rule_status()
    narration = await _load_narration(report_week)
    performance = compute_scorecard([_snapshot_dict(r) for r in history_rows])

    return assemble_report(
        report_week, nav, cash, snapshots, recommendations, rule_status,
        None,  # regime_state's human label isn't persisted (only the composite score is)
        narration,
        narration_absent_reason_for_reassembly(settings, narration),
        performance=performance,
    )


# ── read routes (declared before the /{report_week} catch-all) ────────────
@router.get("/weeks")
async def list_weeks(settings: Settings = Depends(get_settings)) -> dict[str, Any]:
    # `weeks` stays byte-identical — `ReportArchive.tsx`/`client.ts`
    # consume it as `{weeks: [str]}` today (an earlier
    # per-week shape change here broke those consumers). This route adds
    # ONLY the new `availability` key alongside it, never touching `weeks`.
    if settings.demo_mode:
        weeks = _demo_weeks()
        # Synthetic — never a `report_packs` read under DEMO_MODE: every demo
        # week has a pack (`_demo_pack` builds one for any week in the
        # deterministic trailing window), no week ever has a PDF.
        return {"weeks": weeks, "availability": {w: {"pack": True, "pdf": False} for w in weeks}}
    weeks = await _report_weeks()
    return {"weeks": weeks, "availability": await _pack_availability(weeks)}


@router.get("/latest")
async def latest_report(
    redis: Redis = Depends(get_redis), settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    if settings.demo_mode:
        report = await _demo_report("latest", redis, settings)
        if report is None:
            raise NotFound("no demo report available")
        return report
    weeks = await _report_weeks()
    if not weeks:
        raise NotFound("no report has been generated yet")
    report = await _assemble_for_week(weeks[0], redis, settings)
    if report is None:  # pragma: no cover — weeks[0] came from these same tables
        raise NotFound("no report has been generated yet")
    return report


@router.get("/{report_week}")
async def get_report(
    report_week: str = Path(..., min_length=1, max_length=16),
    redis: Redis = Depends(get_redis), settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    if settings.demo_mode:
        report = await _demo_report(report_week, redis, settings)
        if report is None:
            raise NotFound(f"no demo report for week '{report_week}'")
        return report
    report = await _assemble_for_week(report_week, redis, settings)
    if report is None:
        raise NotFound(f"no report for week '{report_week}'")
    return report


# `"latest"` sentinel (follow-up fix, live design review): the pre-existing
# `/latest` route resolves it for the four-section report; `get_report`
# itself only resolves it in demo mode (`_demo_report`'s own `"latest"`
# branch) — real mode's `/{report_week}` never special-cased it because
# `/latest` already exists as a dedicated route for that surface. The new
# pack/pdf routes have no such dedicated `/latest` sibling and the frontend
# addresses them AS `/{report_week}/pack` with `report_week="latest"`, so
# both routes resolve it here directly: "latest" = the newest COMPLETE
# `report_packs` row (not `_report_weeks()`'s snapshot/recommendation-backed
# notion of "latest week" — a pack can lag the newest advisor week, or that
# week's build can still be `building`/`failed`).
async def _resolve_pack_row(db: Any, report_week: str) -> ReportPack | None:
    if report_week == "latest":
        stmt = (
            select(ReportPack).where(ReportPack.status == "complete")
            .order_by(ReportPack.report_week.desc()).limit(1)
        )
    else:
        stmt = select(ReportPack).where(ReportPack.report_week == report_week)
    return (await db.execute(stmt)).scalar_one_or_none()


@router.get("/{report_week}/pack")
async def get_report_pack(
    report_week: str = Path(..., min_length=1, max_length=16),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    if settings.demo_mode:
        pack = await _demo_pack(report_week, settings)
        if pack is None:
            raise NotFound(f"no demo report pack for week '{report_week}'")
        return pack
    async with session_scope() as db:
        row = await _resolve_pack_row(db, report_week)
    if row is None:
        raise NotFound(f"no report pack for week '{report_week}'")
    # Straight row read, no reassembly — a frozen snapshot of whatever the
    # worker last wrote, not recomputed from the live engines. `pdf`
    # uses the same on-disk check as `/weeks`' `availability.pdf` (SEV3
    # `pdf_path is not None` alone overclaims a file that may no
    # longer exist).
    return {
        "report_week": row.report_week, "status": row.status,
        "sections": row.sections, "sources": row.sources,
        "narrated": row.narrated, "pdf": _pdf_available(row.pdf_path),
    }


@router.get("/{report_week}/pdf")
async def get_report_pdf(
    report_week: str = Path(..., min_length=1, max_length=16),
    settings: Settings = Depends(get_settings),
) -> FileResponse:
    # DEMO_MODE branch FIRST: a demo viewer must
    # never reach a real `report_packs` row, not even to learn "no PDF this
    # week" — unconditional 404, before any DB read. Applies to "latest" too:
    # there is never a demo PDF, so this stays a flat 404 regardless of the
    # requested week.
    if settings.demo_mode:
        raise NotFound(f"no PDF for week '{report_week}'")
    async with session_scope() as db:
        row = await _resolve_pack_row(db, report_week)
    pdf_path = row.pdf_path if row is not None else None
    # Defense-in-depth + SEV3 es: containment (rejecting the
    # base dir itself, not just traversal escapes) and on-disk existence are
    # both checked before ever handing anything to FileResponse. A missing/
    # stale file (row has a path, but nothing lives there) 404s, same as an
    # absent row — never a 500. A genuine containment violation (an actual
    # `ValueError` from `resolve_pdf_path`) is NOT caught here and propagates
    # as a loud 500 — that's an invariant violation on an already-persisted
    # row, not a normal "not found".
    real_path = _resolve_and_check_pdf(pdf_path)
    if real_path is None:
        raise NotFound(f"no PDF for week '{report_week}'")
    return FileResponse(
        real_path, media_type="application/pdf", filename=f"catallax-weekly-{row.report_week}.pdf",
    )


# ── act on a recommendation ────────────────────────────────────────────
# Phase 18: the FOR UPDATE / append-only insert / prior.note-carried
# mechanics that used to live in this module's own `_transition` moved to
# `services/recommendation_lifecycle.transition` (also called by the report
# worker's maintenance tick, , for `expired`/`superseded`) — this module
# now only shapes the HTTP surface: body validation + `_rec_dict` responses.
class RecommendationAction(BaseModel):
    note: str = Field(min_length=1, max_length=2000, description="the human's reason for this decision")


class RecommendationModify(RecommendationAction):
    sizing_override: float = Field(
        gt=0, allow_inf_nan=False,
        description="the human-chosen sizing that replaces the rule's proposed sizing",
    )


class RecommendationExecute(RecommendationAction):
    executed_at: datetime
    fill_price: float = Field(gt=0, allow_inf_nan=False)
    quantity: float = Field(gt=0, allow_inf_nan=False)
    venue: str = Field(min_length=1, max_length=64)


async def _transition(
    recommendation_id: int, new_status: str, note: str, settings: Settings,
    extra_snapshot: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result = await recommendation_lifecycle.transition(
        recommendation_id, new_status, note=note, extra_snapshot=extra_snapshot, settings=settings,
    )
    return {"new": _rec_dict(result["new"]), "prior": _rec_dict(result["prior"])}


@router.post("/recommendations/{recommendation_id}/accept")
async def accept_recommendation(
    body: RecommendationAction,
    recommendation_id: int = Path(..., ge=1),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    return await _transition(recommendation_id, "accepted", body.note, settings)


@router.post("/recommendations/{recommendation_id}/override")
async def override_recommendation(
    body: RecommendationAction,
    recommendation_id: int = Path(..., ge=1),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    return await _transition(
        recommendation_id, "overridden", body.note, settings, extra_snapshot={"human_action": "override"},
    )


@router.post("/recommendations/{recommendation_id}/modify")
async def modify_recommendation(
    body: RecommendationModify,
    recommendation_id: int = Path(..., ge=1),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    return await _transition(
        recommendation_id, "modified", body.note, settings,
        extra_snapshot={"sizing_override": body.sizing_override},
    )


@router.post("/recommendations/{recommendation_id}/reject")
async def reject_recommendation(
    body: RecommendationAction,
    recommendation_id: int = Path(..., ge=1),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    # F1: persists as `overridden`, not a distinct `rejected` status — the
    # frozen `advisor_report._load_overrides` cooldown loop reads exactly
    # that status string. `human_action` distinguishes it from `/override`.
    return await _transition(
        recommendation_id, "overridden", body.note, settings, extra_snapshot={"human_action": "reject"},
    )


@router.post("/recommendations/{recommendation_id}/execute")
async def execute_recommendation(
    body: RecommendationExecute,
    recommendation_id: int = Path(..., ge=1),
    settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    return await _transition(
        recommendation_id, "executed", body.note, settings,
        extra_snapshot={
            "executed_at": body.executed_at.isoformat(), "fill_price": body.fill_price,
            "quantity": body.quantity, "venue": body.venue,
        },
    )


# ── decisions projection (Phase 18, additive) ──────────────────────
def _demo_decisions() -> dict[str, Any]:
    """fixed, deterministic — never a DB session, never a
    `RiskEngine`/`DecisionProjection` construction."""
    rows: list[dict[str, Any]] = [
        {
            "id": 1, "rule_id": "weight_band_trim", "action": "trim", "status": "proposed",
            "sizing": 1500.0, "as_of": "2026-08-31", "report_week": "2026-W36",
            "funding_source": {
                "kind": "proceeds", "symbols": ["DEMO1"], "est_proceeds_eur": 1500.0,
                "to": "cash", "lots": "not modelled (Parked)",
            },
            "risk_budget_impact": {"cvar95_now": 2.1, "cvar95_post": 1.9, "delta_pct": -0.2},
            "invalidation": {
                "text": "weight of DEMO1 back inside band [0, 0.2000]",
                "current_weight": 0.24, "still_valid": True,
            },
            "review_at": "2026-11-23", "expires_at": "2027-02-22", "forecast": None, "outcome": None,
            "lifecycle": [{
                "id": 1, "status": "proposed", "human_action": None,
                "created_at": "2026-08-31T09:00:00+00:00", "human_note": None,
            }],
        },
        {
            "id": 2, "rule_id": "dca_modulation", "action": "buy", "status": "accepted",
            "sizing": 400.0, "as_of": "2026-08-31", "report_week": "2026-W36",
            "funding_source": {
                "kind": "clearing_cash", "account": "Assets:Demo:Clearing", "amount_eur": 400.0,
                "available_eur": 2000.0, "covers": True,
            },
            "risk_budget_impact": {
                "unavailable": "dca_modulation has no symbol-level leg — no post-action weight vector",
            },
            "invalidation": {"text": "contribution already made this period", "still_valid": True},
            "review_at": "2026-11-23", "expires_at": "2027-02-22", "forecast": None, "outcome": None,
            "lifecycle": [
                {
                    "id": 2, "status": "proposed", "human_action": None,
                    "created_at": "2026-08-31T09:00:00+00:00", "human_note": None,
                },
                {
                    "id": 3, "status": "accepted", "human_action": None,
                    "created_at": "2026-08-31T10:00:00+00:00", "human_note": "looks fine",
                },
            ],
        },
        {
            "id": 4, "rule_id": "cash_floor_band", "action": "hold", "status": "evaluated",
            "sizing": None, "as_of": "2026-08-31", "report_week": "2026-W36",
            "funding_source": {"kind": "none", "reason": "row status 'evaluated' — no action, nothing to fund"},
            "risk_budget_impact": {"unavailable": "row status 'evaluated' — no action, nothing to project"},
            "invalidation": {"text": "cash >= 0.1000 x NAV", "current": 0.14, "still_valid": False},
            "review_at": "2026-11-23", "expires_at": "2027-02-22", "forecast": None, "outcome": None,
            "lifecycle": [{
                "id": 4, "status": "evaluated", "human_action": None,
                "created_at": "2026-08-31T09:00:00+00:00", "human_note": None,
            }],
        },
    ]
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    return {
        "engine": "decision-projection-v1", "rows": rows, "counts": counts,
        "no_action_rate": counts.get("evaluated", 0) / len(rows),
        "how_to_read": "Demo sample — a fixed 3-row projection, never touching the DB or a live RiskEngine.",
    }


@router.get("/{report_week}/decisions")
async def get_report_decisions(
    report_week: str = Path(..., min_length=1, max_length=16),
    redis: Redis = Depends(get_redis), settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    if settings.demo_mode:
        return _demo_decisions()
    resolved_week = report_week
    if report_week == "latest":
        # F13: copy the pack handler's "latest" resolution (newest COMPLETE
        # `report_packs` row) — the chain moves after the pack is built, so
        # this stays its own tiny inline query rather than reusing
        # `_resolve_pack_row` (kept independent per-handler, per spec).
        async with session_scope() as db:
            row = (await db.execute(
                select(ReportPack).where(ReportPack.status == "complete")
                .order_by(ReportPack.report_week.desc()).limit(1)
            )).scalar_one_or_none()
        if row is None:
            raise NotFound("no report pack available yet")
        resolved_week = row.report_week
    return await DecisionProjection(redis, settings).chapter_decisions(resolved_week)
