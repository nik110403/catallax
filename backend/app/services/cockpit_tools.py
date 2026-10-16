"""Cockpit typed-tool layer.

A read-only tool registry the cockpit worker calls to ground an
analyst's question in real data. **Transport-agnostic by design** — no LLM
or provider code lives here; this is the same surface the deferred Phase-20+
"MCP ask-my-terminal" item would expose later.

Every tool wraps an EXISTING service call (`services/portfolio.py`,
`services/risk.py`, `services/markets.py`, `services/decisions.py`,
`services/forecasts.py`, plus `ReportPack.sources`/the `JournalEntry`/`Note`
models) — no new queries invented. `journal_entries` and `notes`
query `JournalEntry`/`Note` directly rather than through `services/journal.py`
/`services/notes.py`: the journal and notes services are separate modules and
are intentionally not depended on here.

Hard invariants (each with its own test in `test_cockpit_tools.py`):
1. Read-only — no tool takes a write path (grep gate over this file: an ORM
   mutation call, a transaction commit, or a raw write-DML keyword must
   never appear here — this docstring deliberately doesn't spell it out so the
   gate's own grep can't match its own documentation).
2. Every `ToolResult` carries `record_ids`; a tool that cannot produce ids
   returns `[]` explicitly and sets `note`.
3. Bounded — every handler caps rows (default 50, hard max 200) and reports
   `truncated`.
4. DEMO_MODE-aware — mirrors whatever the matching REST route does in demo
   mode: `portfolio_exposure`/`risk_metrics` are demo-transparent (the
   ledger/wealthfolio bridge itself swaps to fixtures); `market_regime` has
   no demo swap (public, non money-bearing data, same as its REST route);
   `recommendations`/`forecasts`/`report_packs` mirror their route's demo
   fixture; `journal_entries`/`notes` return empty rows in demo mode, same
   as their own routes.
5. No prose — tools return data, never interpretation.
"""
from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import select

from app.config import Settings
from app.db import session_scope
from app.errors import NotFound
from app.models import JournalEntry, Note
from app.redis import get_redis
from app.services.decisions import DecisionProjection
from app.services.forecasts import ForecastLedger
from app.services.markets import get_overview
from app.services.portfolio import PortfolioService
from app.services.risk import RiskEngine

DEFAULT_ROW_CAP = 50
MAX_ROW_CAP = 200


@dataclass
class ToolResult:
    rows: list[dict[str, Any]]
    record_ids: list[dict[str, Any]]
    truncated: bool
    note: str | None = None


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    params: dict[str, Any]  # JSON-schema fragment
    handler: Callable[[dict[str, Any], Settings], Awaitable[ToolResult]]


def _cap(rows: list[dict[str, Any]], limit: object) -> tuple[list[dict[str, Any]], bool]:
    """Clamp `rows` to a bounded window (invariant 3). `limit` is caller-
    supplied and untrusted — clamped into `[1, MAX_ROW_CAP]`, falling back
    to `DEFAULT_ROW_CAP` when absent/invalid."""
    try:
        n = int(limit) if limit else DEFAULT_ROW_CAP
    except (TypeError, ValueError):
        n = DEFAULT_ROW_CAP
    n = max(1, min(n, MAX_ROW_CAP))
    if len(rows) > n:
        return rows[:n], True
    return rows, False


def _schema(properties: dict[str, Any], required: list[str] | None = None) -> dict[str, Any]:
    schema: dict[str, Any] = {"type": "object", "properties": properties, "additionalProperties": False}
    if required:
        schema["required"] = required
    return schema


_LIMIT_PROP = {"type": "integer", "minimum": 1, "maximum": MAX_ROW_CAP,
                "description": f"row cap, default {DEFAULT_ROW_CAP}, hard max {MAX_ROW_CAP}"}


# ── portfolio_exposure ──────────────────────────────────────────────────
async def _portfolio_exposure(args: dict[str, Any], settings: Settings) -> ToolResult:
    svc = PortfolioService(get_redis(), settings)
    holdings = await svc.holdings()
    rows = [h.model_dump() for h in holdings]
    rows, truncated = _cap(rows, args.get("limit"))
    return ToolResult(
        rows=rows, record_ids=[], truncated=truncated,
        note="positions/weights/currency+asset-class breakdown, computed live from the "
             "ledger query — no persisted row id to cite",
    )


# ── risk_metrics ─────────────────────────────────────────────────────────
async def _risk_metrics(args: dict[str, Any], settings: Settings) -> ToolResult:
    engine = RiskEngine(get_redis(), settings)
    var_report, _ = await engine.var()
    stress_report = await engine.stress()
    xray_report, _ = await engine.xray()
    rows: list[dict[str, Any]] = [{
        "kind": "var", "as_of": var_report.as_of, "confidence": var_report.confidence,
        "var_eur": var_report.var_eur, "var_pct": var_report.var_pct,
        "cvar_eur": var_report.cvar_eur, "cvar_pct": var_report.cvar_pct,
        "var99_eur": var_report.var99_eur, "cvar99_eur": var_report.cvar99_eur,
    }]
    rows += [
        {"kind": "stress", "scenario": s.name, "portfolio_return_pct": s.portfolio_return_pct,
         "portfolio_pnl_eur": s.portfolio_pnl_eur}
        for s in stress_report.scenarios
    ]
    rows += [
        {"kind": "concentration", "symbol": c.symbol, "contribution_pct": c.contribution_pct,
         "weight_pct": c.weight_pct}
        for c in xray_report.risk_contributors
    ]
    rows, truncated = _cap(rows, args.get("limit"))
    return ToolResult(
        rows=rows, record_ids=[], truncated=truncated,
        note="VaR/CVaR, stress scenarios and concentration are computed live — no persisted row id",
    )


# ── market_regime ────────────────────────────────────────────────────────
async def _market_regime(args: dict[str, Any], settings: Settings) -> ToolResult:
    overview = await get_overview(get_redis(), settings)
    rows: list[dict[str, Any]] = [{"kind": "composite", **overview.composite.model_dump()}]
    rows += [
        {"kind": "component", "group": g.key, "label": g.label, "items": [q.model_dump() for q in g.items]}
        for g in overview.groups
    ]
    rows, truncated = _cap(rows, args.get("limit"))
    return ToolResult(
        rows=rows, record_ids=[], truncated=truncated,
        note=f"as_of={overview.as_of.isoformat()} — computed live, no persisted row id",
    )


# ── recommendations ──────────────────────────────────────────────────────
async def _recommendations(args: dict[str, Any], settings: Settings) -> ToolResult:
    from app.models import ReportPack  # lazy — avoid a module-load-time router import
    from app.routers.reports import _demo_decisions, _resolve_pack_row  # lazy, see module docstring

    report_week = args.get("report_week") or "latest"
    if settings.demo_mode:
        data = _demo_decisions()
    else:
        async with session_scope() as db:
            row: ReportPack | None = await _resolve_pack_row(db, report_week)
        if row is None:
            return ToolResult(rows=[], record_ids=[], truncated=False,
                               note=f"no report pack available for week {report_week!r}")
        data = await DecisionProjection(get_redis(), settings).chapter_decisions(row.report_week)
    rows, truncated = _cap(data["rows"], args.get("limit"))
    record_ids = [{"kind": "recommendation", "id": r["id"]} for r in rows]
    return ToolResult(rows=rows, record_ids=record_ids, truncated=truncated)


# ── forecasts ─────────────────────────────────────────────────────────────
_DEFAULT_STATS_HORIZON_DAYS = 28  # the ledger's common shadow-forecast horizon


async def _forecasts(args: dict[str, Any], settings: Settings) -> ToolResult:
    from app.routers.forecasts import _demo_forecasts_sample  # lazy, see module docstring

    horizon_days = int(args.get("horizon_days") or _DEFAULT_STATS_HORIZON_DAYS)
    if settings.demo_mode:
        forecast_rows = _demo_forecasts_sample()
        status = args.get("status")
        if status:
            forecast_rows = [r for r in forecast_rows if r["status"] == status]
        stats: dict[str, dict[str, Any]] = {}
        note = "demo fixtures — Brier-by-issuer stats not computed in demo mode"
    else:
        ledger = ForecastLedger(get_redis(), settings)
        forecast_rows = await ledger.list(
            status=args.get("status"), week=args.get("week"), issued_by=args.get("issued_by"),
            limit=MAX_ROW_CAP,
        )
        stats = await ledger.stats_by_issuer(horizon_days)
        note = None
    rows = [dict(r, kind="forecast") for r in forecast_rows]
    rows += [{"kind": "issuer_stats", "issued_by": issuer, "horizon_days": horizon_days, **s}
             for issuer, s in stats.items()]
    rows, truncated = _cap(rows, args.get("limit"))
    record_ids = [{"kind": "forecast", "id": r["id"]} for r in rows if r.get("kind") == "forecast"]
    return ToolResult(rows=rows, record_ids=record_ids, truncated=truncated, note=note)


# ── report_packs ──────────────────────────────────────────────────────────
async def _report_packs(args: dict[str, Any], settings: Settings) -> ToolResult:
    from app.routers.reports import _demo_pack, _demo_weeks, _report_weeks, _resolve_pack_row  # lazy

    report_week = args.get("report_week")
    if settings.demo_mode:
        weeks = _demo_weeks()
        pack = await _demo_pack(report_week, settings) if report_week else None
    else:
        weeks = await _report_weeks()
        if report_week:
            async with session_scope() as db:
                row = await _resolve_pack_row(db, report_week)
            pack = None if row is None else {"report_week": row.report_week, "sections": row.sections or {}}
        else:
            pack = None
    rows: list[dict[str, Any]] = [{"kind": "week", "report_week": w} for w in weeks]
    record_ids = [{"kind": "report_pack", "id": w} for w in weeks]
    if pack is not None:
        rows.append({
            "kind": "sections", "report_week": pack["report_week"],
            "section_keys": sorted(pack["sections"].keys()),
        })
    rows, truncated = _cap(rows, args.get("limit"))
    note = None if weeks else "no report packs available yet"
    return ToolResult(rows=rows, record_ids=record_ids, truncated=truncated, note=note)


# ── journal_entries ───────────────────────────────────────────────────────
async def _journal_entries(args: dict[str, Any], settings: Settings) -> ToolResult:
    if settings.demo_mode:
        return ToolResult(rows=[], record_ids=[], truncated=False,
                           note="journal reads are empty in demo mode (S2 route contract)")
    status = args.get("status")
    due_before = args.get("due_before")
    limit = int(args.get("limit") or DEFAULT_ROW_CAP)
    limit = max(1, min(limit, MAX_ROW_CAP))
    async with session_scope() as db:
        stmt = select(JournalEntry).order_by(JournalEntry.review_date).limit(limit + 1)
        if status is not None:
            stmt = stmt.where(JournalEntry.status == status)
        if due_before is not None:
            stmt = stmt.where(JournalEntry.review_date <= date.fromisoformat(due_before))
        rows_orm = (await db.execute(stmt)).scalars().all()
    truncated = len(rows_orm) > limit
    rows_orm = rows_orm[:limit]
    rows = [{
        "id": r.id, "entry_date": r.entry_date.isoformat(), "title": r.title, "subject": r.subject,
        "probability": r.probability, "review_date": r.review_date.isoformat(), "status": r.status,
        "source": r.source, "forecast_id": r.forecast_id,
    } for r in rows_orm]
    record_ids = [{"kind": "journal_entry", "id": r.id} for r in rows_orm]
    return ToolResult(rows=rows, record_ids=record_ids, truncated=truncated)


# ── notes ─────────────────────────────────────────────────────────────────
async def _notes(args: dict[str, Any], settings: Settings) -> ToolResult:
    if settings.demo_mode:
        return ToolResult(rows=[], record_ids=[], truncated=False,
                           note="notes reads are empty in demo mode (S3 route contract)")
    kind = args.get("kind")
    status = args.get("status")
    limit = int(args.get("limit") or DEFAULT_ROW_CAP)
    limit = max(1, min(limit, MAX_ROW_CAP))
    async with session_scope() as db:
        stmt = select(Note).order_by(Note.updated_at.desc()).limit(limit + 1)
        if kind is not None:
            stmt = stmt.where(Note.kind == kind)
        if status is not None:
            stmt = stmt.where(Note.status == status)
        rows_orm = (await db.execute(stmt)).scalars().all()
    truncated = len(rows_orm) > limit
    rows_orm = rows_orm[:limit]
    rows = [{
        "id": r.id, "slug": r.slug, "kind": r.kind, "title": r.title, "status": r.status,
        "one_sentence": r.one_sentence, "subjects": r.subjects or [], "evidence_refs": r.evidence_refs or [],
    } for r in rows_orm]
    record_ids = [{"kind": "note", "id": r.id} for r in rows_orm]
    return ToolResult(rows=rows, record_ids=record_ids, truncated=truncated)


# ── provenance ────────────────────────────────────────────────────────────
async def _provenance(args: dict[str, Any], settings: Settings) -> ToolResult:
    """Source + as-of for a report pack's chapters — `ReportPack.sources`
    (built by `ReportPackEngine.build_chapters`) IS the existing provenance
    surface: `{engine, fetched_at, stale, error}` per chapter key."""
    from app.routers.reports import _demo_pack, _resolve_pack_row  # lazy, see module docstring

    report_week = args.get("report_week")
    if not report_week:
        return ToolResult(rows=[], record_ids=[], truncated=False,
                           note="provenance requires a report_week to look up chapter sources")
    key = args.get("key")
    if settings.demo_mode:
        pack = await _demo_pack(report_week, settings)
        sources: dict[str, Any] = pack["sources"] if pack else {}
        resolved_week = pack["report_week"] if pack else report_week
    else:
        async with session_scope() as db:
            row = await _resolve_pack_row(db, report_week)
        sources = (row.sources or {}) if row is not None else {}
        resolved_week = row.report_week if row is not None else report_week
    if key:
        sources = {k: v for k, v in sources.items() if k == key}
    rows = [{"report_week": resolved_week, "section": k, **v} for k, v in sources.items()]
    rows, truncated = _cap(rows, args.get("limit"))
    record_ids = [{"kind": "report_pack", "id": resolved_week}] if sources else []
    note = None if sources else f"no chapter sources recorded for week {report_week!r}"
    return ToolResult(rows=rows, record_ids=record_ids, truncated=truncated, note=note)


# ── registry ──────────────────────────────────────────────────────────────
TOOLS: dict[str, Tool] = {
    "portfolio_exposure": Tool(
        name="portfolio_exposure", handler=_portfolio_exposure,
        description="Current positions: weights, currency and asset-class breakdown.",
        params=_schema({"limit": _LIMIT_PROP}),
    ),
    "risk_metrics": Tool(
        name="risk_metrics", handler=_risk_metrics,
        description="VaR/CVaR, stress-scenario rows and concentration (top risk contributors).",
        params=_schema({"limit": _LIMIT_PROP}),
    ),
    "market_regime": Tool(
        name="market_regime", handler=_market_regime,
        description="Cross-asset market regime composite (Risk-On/Neutral/Risk-Off) and its component groups.",
        params=_schema({"limit": _LIMIT_PROP}),
    ),
    "recommendations": Tool(
        name="recommendations", handler=_recommendations,
        description="Advisor recommendation chain rows for a report week: status, action, sizing, invalidation.",
        params=_schema({
            "report_week": {"type": "string", "description": "ISO week (e.g. 2026-W37) or 'latest'"},
            "limit": _LIMIT_PROP,
        }),
    ),
    "forecasts": Tool(
        name="forecasts", handler=_forecasts,
        description="Open and resolved forecasts, plus Brier/hit-rate stats by issuer at a given horizon.",
        params=_schema({
            "status": {"type": "string", "enum": ["pending", "resolved", "abstained", "void"]},
            "week": {"type": "string", "description": "ISO report week filter"},
            "issued_by": {"type": "string"},
            "horizon_days": {"type": "integer", "minimum": 1},
            "limit": _LIMIT_PROP,
        }),
    ),
    "report_packs": Tool(
        name="report_packs", handler=_report_packs,
        description="Available report-pack weeks, and one week's section keys.",
        params=_schema({
            "report_week": {"type": "string", "description": "ISO week or 'latest'"},
            "limit": _LIMIT_PROP,
        }),
    ),
    "journal_entries": Tool(
        name="journal_entries", handler=_journal_entries,
        description="Analyst-journal entries and reviews due.",
        params=_schema({
            "status": {"type": "string", "enum": ["open", "reviewed", "superseded"]},
            "due_before": {"type": "string", "description": "ISO date, filters review_date <="},
            "limit": _LIMIT_PROP,
        }),
    ),
    "notes": Tool(
        name="notes", handler=_notes,
        description="Research notes / theses with their evidence references.",
        params=_schema({
            "kind": {"type": "string", "enum": ["research_note", "thesis"]},
            "status": {"type": "string", "enum": ["draft", "signed"]},
            "limit": _LIMIT_PROP,
        }),
    ),
    "provenance": Tool(
        name="provenance", handler=_provenance,
        description="Source and as-of timestamp for a report pack's chapters (all, or one by key).",
        params=_schema({
            "report_week": {"type": "string", "description": "ISO week or 'latest'"},
            "key": {"type": "string", "description": "chapter key, e.g. 'portfolio'/'market'/'holdings'"},
            "limit": _LIMIT_PROP,
        }, required=["report_week"]),
    ),
}


async def call(name: str, args: dict[str, Any], *, settings: Settings) -> ToolResult:
    tool = TOOLS.get(name)
    if tool is None:
        raise NotFound(f"unknown cockpit tool: {name!r}")
    return await tool.handler(args or {}, settings)


def schemas() -> list[dict[str, Any]]:
    """Provider-shaped (Anthropic tool-use) definitions for every registered tool."""
    return [{"name": t.name, "description": t.description, "input_schema": t.params} for t in TOOLS.values()]
