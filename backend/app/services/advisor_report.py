"""Weekly Advisor report generation — the lifespan tick's
worker + deterministic report assembly.

`generate_weekly_report()` is the single entry point: called on every tick
(`main.py`'s `_advisor_weekly_tick_loop`) AND directly as the manual
acceptance test hook (pass a fixed `now=` to force a run without touching the
schedule). It:

  1. Gates on `report_due()` — an ISO-week calendar-boundary check that
     mirrors `strategy_runner.rebalance_due` exactly (`last=None` -> due), so
     a container down over a week boundary produces exactly ONE catch-up
     report on the next tick, and a re-run inside the same ISO week is a
     total no-op (no DB write of any kind — the gate fires before any bridge
     is even called).
  2. Snapshots every held symbol AND runs `AdvisorEngine.evaluate()` (,
     cooldown context sourced from recently-overridden `recommendations`
     rows), then persists `portfolio_snapshots` + `recommendations` together
     in ONE transaction (a week is all-or-nothing — no crash window where one
     table's write for a week lands and the other's doesn't). Snapshots are
     additionally idempotent via the `(report_week, symbol)` unique
     constraint (`ON CONFLICT DO NOTHING`, belt-and-suspenders under the
     `report_due` gate above); every recommendation row (fired AND
     evaluated) is persisted append-only, `status` re-validated at the write
     site since the DB doesn't constrain it — the service layer is the enum
     authority.
  3. Persists the regime composite + a derived component-set id (the engine
     itself has no such id yet — `component_set_id()` hashes the sorted
     `(key, weight)` pairs of the current component set so identical inputs
     always hash the same). Regime is best-effort: a `macro` profile outage
     degrades to `regime_state=None`/absent, never crashes the tick.
  4. Attempts narration: ONE real `LLMProviderAdapter.complete()` call is the
     probe (the `ai_probe.py` pattern — `health()` only proves the binary
     runs, not that the OAuth session is still valid). Any warning on the
     response (no-op provider, missing binary, non-zero exit, timeout,
     unparseable output) means "probe failed" -> the report ships un-narrated
     in the same run, visibly marked (`assemble_report(...)["narrated"]`, with
     `["narration_absent_reason"]` distinguishing "not_configured" — no probe
     attempted, `llm_provider == "noop"` — from a genuine "probe_failed").
     A successful narration is persisted onto the existing `ai_runs` table
     (`kind="narrate"`, no new table) for audit, keyed via `config.report_week`
     since `ai_runs` has no `report_week` column of its own.

Every DB touch is its own module-level `async def` (never nested in
`generate_weekly_report`) so a test can monkeypatch just the persistence seam
— this codebase's established convention for testing DB-bound service code
without a real Postgres connection (see `test_demo_domains.py`'s
`session_scope` monkeypatches).

There is no `reports` table (migration 0011 only adds `portfolio_snapshots` +
`recommendations`): `assemble_report()` builds the four sections (What
happened / Where we stand / What to do next / Scorecard) purely from rows
already in hand — the SAME function a later read route can call again
over rows re-read from the DB, so the shape only needs to be defined once.
"""
from __future__ import annotations

import hashlib
import json
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.llm.base import LLMMessage, LLMProviderAdapter, Role
from app.adapters.llm.factory import build_llm_adapter
from app.config import Settings
from app.db import session_scope
from app.models import AiRun, PortfolioSnapshot, Recommendation
from app.services.advisor import make_advisor
from app.services.market_research import MarketResearchEngine
from app.services.strategy_runner import rebalance_due
from app.services.wealthfolio import make_wealthfolio

logger = logging.getLogger("catallax.advisor_report")

# Mirrors `advisor.py`'s `_VALID_STATUSES` — re-checked here because the
# service layer (not the DB) is the enum authority on every write, not just
# the one inside `AdvisorEngine._row()`.
_VALID_STATUSES = {"evaluated", "proposed", "accepted", "overridden", "executed"}


# ── pure functions (unit-tested without a DB or Redis) ──────────────────
def iso_week(d: date | datetime) -> str:
    """`"YYYY-Www"`, zero-padded — matches `AdvisorEngine._weeks_since`'s
    expected format and the `test_advisor.py` fixture's own `REPORT_WEEK`."""
    dd = d.date() if isinstance(d, datetime) else d
    year, week, _ = dd.isocalendar()
    return f"{year}-W{week:02d}"


def report_due(last_report_week: str | None, now: datetime) -> bool:
    """`last_report_week=None` (or unparsable) -> always due — reuses
    `strategy_runner.rebalance_due`'s exact ISO-week-boundary semantics so a
    container that missed one or several week boundaries still generates
    exactly ONE catch-up report on the next check, and a same-week re-run is
    never due."""
    if not last_report_week:
        return True
    try:
        year_s, week_s = last_report_week.split("-W")
        last_dt = datetime.combine(
            date.fromisocalendar(int(year_s), int(week_s), 1), datetime.min.time(), tzinfo=timezone.utc
        )
    except (ValueError, IndexError):
        return True  # malformed record must never wrongly suppress a report
    return rebalance_due(last_dt, "weekly", now)


def narration_absent_reason_for_reassembly(
    settings: Settings, narration: dict[str, Any] | None,
) -> str | None:
    """The two-state narration-absence reason,
    reusable by a READ-time reassembly of an already-persisted week
    (`routers/reports.py`) — the live tick (`generate_weekly_report` below)
    computes its own version of this inline, since it directly observes
    whether a probe was attempted this run; reassembly has no such record,
    only whether an `ai_runs` row was ever persisted for that week. Derives
    the SAME two states from what reassembly CAN see: today's
    `settings.llm_provider` (a reasonable proxy for "was narration even
    configured") and whether a narration row was found.

    `None` iff `narration is not None` — never surfaced once narrated,
    exactly like the tick path. Otherwise: `llm_provider == "noop"` means
    "not configured" (no probe would even be attempted today); any other
    provider with no persisted row means the probe was attempted for that
    week and failed (or nothing has run there yet) — "probe failed" is the
    accurate default rather than silently implying nothing was ever tried.
    """
    if narration is not None:
        return None
    if settings.llm_provider == "noop":
        return "not configured in this deployment (ai profile not enabled)"
    return "narration probe failed (see backend logs)"


def component_set_id(components: list[Any]) -> str | None:
    """A stable id for the regime's current component set — the engine
    doesn't mint one itself, so this hashes the sorted `(key, weight)` pairs;
    identical component sets always hash identically, any change (a
    component added/removed/reweighted) changes the id."""
    if not components:
        return None
    key = json.dumps(sorted((c.key, round(c.weight, 6)) for c in components))
    return hashlib.sha256(key.encode()).hexdigest()[:16]


def _rule_arithmetic(r: dict[str, Any]) -> dict[str, Any]:
    """Structured would-fire-at figures for one `rule_evaluations` entry
    (follow-up, design review finding B1): the legacy
    `{rule_id, action, status, note}` projection strips `input_snapshot` and
    `tax_cost_arithmetic` entirely, so a SUPPRESSED row (below materiality or
    on cooldown — `status="evaluated"`, `action="hold"`) had no reachable
    would-fire-at figures on the client except by string-parsing `note`.
    Named fields only (never the full `input_snapshot` — `cash_floor_band`'s
    carries a `legs` list that can be large) and absent-tolerant: a key is
    included only when the row actually has it, since not every rule's
    `input_snapshot`/`tax_cost_arithmetic` carries every field (e.g.
    `cash_floor_band` has no top-level `symbol`; its `tax_cost_arithmetic`
    has no `benefit_eur`/`threshold_eur`/`fires` — those are the sell-gate's
    own fields, present only on `weight_band_trim` rows). `material` (both
    rules set it on `input_snapshot`) is the UI's near-miss SIGNAL — a
    suppressed row can be in-band-but-close (materiality gate never even
    cleared) vs already material-and-only-cooldown/sell-gate-blocked; those
    read very differently to a human deciding whether to watch a row."""
    arithmetic: dict[str, Any] = {}
    snap = r.get("input_snapshot") or {}
    for key in (
        "symbol", "trim_value_eur", "shortfall_eur", "material",
        "materiality_eur_threshold", "materiality_nav_pct_threshold",
    ):
        if key in snap:
            arithmetic[key] = snap[key]
    cost = r.get("tax_cost_arithmetic") or {}
    # `tax_label`/`cost_basis_available` were
    # computed but never projected, so a row "fires at €X" without the
    # binding "indicative estimate, not tax advice" label unless the client
    # dug into the full (undisclosed-by-default) `tax_cost_arithmetic`.
    for key in ("benefit_eur", "threshold_eur", "cost_eur", "fires", "tax_label", "cost_basis_available"):
        if key in cost:
            arithmetic[key] = cost[key]
    return arithmetic


def assemble_report(
    report_week: str,
    nav: float,
    cash: float,
    snapshots: list[dict[str, Any]],
    recommendations: list[dict[str, Any]],
    rule_status: dict[str, str],
    regime_state: str | None,
    narration: dict[str, Any] | None,
    narration_absent_reason: str | None = None,
    performance: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Deterministic report assembly from already-fetched rows — no I/O, so
    the SAME set of inputs always produces the SAME output (byte-for-byte
    once JSON-dumped with `default=str`, matching
    `test_advisor_report.py::test_assemble_report_report_week_output_is_deterministic`).
    Callable again later over rows re-read from the DB — there is no
    separate `reports` table to keep in sync. Correction:
    that determinism claim is per-call, not cross-path — the
    tick-time report and the read-time reassembly of the SAME week are NOT
    byte-identical to each other, because `performance` (see below) differs
    between them; the rest of the shape (rule-evaluation counters etc.) is.

    `nav`/`cash` are passed in explicitly (not re-derived from `snapshots[0]`)
    — an all-cash portfolio has an empty `snapshots` list (no held symbols)
    but a real nav/cash, which `snapshots[0]` would silently render as 0.0.

    Each `where_we_stand.rule_evaluations` entry additionally carries
    `arithmetic` (`_rule_arithmetic()`) — an additive fifth key alongside the original four
    (`rule_id`/`action`/`status`/`note`), so a SUPPRESSED row's would-fire-at
    figures (materiality thresholds, shortfall/trim values) reach the client
    without string-parsing `note`.

    `performance` is the read route's real two-arm scorecard
    (`services/scorecard.compute_scorecard()`), merged in under
    `scorecard.performance`. The weekly tick never computes it — it
    lacks the full snapshot history at persist time and doesn't need it —
    so it stays `None` on that write path; every OTHER key under `scorecard`
    (the rule-evaluation counters) is unaffected by `performance` either way.
    """
    positions = [
        {"symbol": s["symbol"], "value_eur": float(s["value_eur"]), "qty": float(s["qty"])}
        for s in snapshots
    ]
    proposed = [r for r in recommendations if r["status"] == "proposed"]
    refused = [r for r in recommendations if r["action"] == "refuse"]
    return {
        "report_week": report_week,
        "what_happened": {
            "nav_eur": round(nav, 2), "cash_eur": round(cash, 2),
            "positions": positions, "regime_state": regime_state,
        },
        "where_we_stand": {
            "positions": positions,
            "rule_evaluations": [
                {
                    "rule_id": r["rule_id"], "action": r["action"], "status": r["status"], "note": r["note"],
                    "arithmetic": _rule_arithmetic(r),
                }
                for r in recommendations
            ],
        },
        "what_to_do_next": {"proposed": proposed, "refused": refused, "rule_status": rule_status},
        "scorecard": {
            "n_evaluated": len(recommendations), "n_proposed": len(proposed),
            "n_refused": len(refused), "rule_status": rule_status,
            "performance": performance,
        },
        "narration": narration,
        "narrated": narration is not None,
        # Two distinct absence reasons: a
        # deployment with the `ai` profile never enabled never attempts a
        # probe at all ("not_configured"); a deployment that DID attempt one
        # but got warnings/empty text back is a genuine failure
        # ("probe_failed"). `None` iff `narrated` is True.
        "narration_absent_reason": None if narration is not None else narration_absent_reason,
    }


def _build_facts(
    report_week: str, nav: float, cash: float, regime_state: str | None,
    regime_composite: float | None, rows: list[dict[str, Any]], rule_status: dict[str, str],
) -> dict[str, Any]:
    """The ONLY input the LLM ever sees — numbers are always computed in
    Python first; the model narrates this dict and nothing else.

    SECURITY (2026-08-16): with `settings.llm_provider == "noop"` (the
    default) this dict is never sent anywhere. Enabling any non-noop provider
    means `_narrate()` below hands `nav_eur`/`cash_eur`/`proposed` sizing off
    to that provider's process/API — off-box for a hosted provider, or to a
    locally-run binary/OAuth session for a CLI-shelling one. That's the
    tradeoff of turning narration on at all, not a bug in this function."""
    proposed = [r for r in rows if r["status"] == "proposed"]
    return {
        "report_week": report_week, "nav_eur": round(nav, 2), "cash_eur": round(cash, 2),
        "regime_state": regime_state, "regime_composite": regime_composite,
        "n_recommendations_evaluated": len(rows),
        "proposed": [
            {"rule_id": r["rule_id"], "action": r["action"], "sizing_eur": r["sizing"], "note": r["note"]}
            for r in proposed
        ],
        "rule_status": rule_status,
    }


async def _narrate(adapter: LLMProviderAdapter, facts: dict[str, Any]) -> dict[str, Any] | None:
    """The real probe IS the narration call (`ai_probe.py`'s pattern) — a
    bare `health()` only proves the `claude` binary runs, not that the
    mounted OAuth session is still valid. Any warning on the response (no-op
    provider, missing binary, non-zero exit, timeout, unparseable output) is
    a failed probe: returns `None`, and the caller ships un-narrated."""
    resp = await adapter.complete(
        [LLMMessage(role=Role.user, content=(
            "Write a concise, plain-English weekly portfolio narration from the FACTS "
            "block: two short paragraphs — what happened, and what (if anything) needs "
            "a decision this week."
        ))],
        facts=facts,
    )
    if resp.warnings or not resp.text.strip():
        return None
    return {"text": resp.text, "grounding": resp.grounding.value, "model": resp.model}


# ── DB touches — each its own function so a test can monkeypatch just this
# seam (mirrors `test_demo_domains.py`'s `session_scope` monkeypatch style) ──
async def _last_report_week() -> str | None:
    """The idempotency gate's ONLY source of truth — MUST be content-
    independent, not "newest row by `created_at`": an append-only
    accept/override INSERT for an OLD `report_week` would otherwise look
    like the newest row and reopen the gate for that old week, duplicating
    the audit log. `max()` over the zero-padded
    `"YYYY-Www"` label sorts correctly lexicographically and is unaffected by
    insertion order.

    Also takes the max across BOTH `recommendations` AND `portfolio_snapshots`
   : a week whose rules all evaluated to zero `proposed`/fired rows
    can still leave `recommendations` empty for that week in edge cases
    (the engine never actually does this today — every rule leaves at least one audit
    row — but the gate must not depend on that holding forever), and a purely
    empty-recommendations week must still close the gate or the tick
    re-runs the full pipeline (WF fetch + regime + CVaR + BQL) every poll
    for the rest of the week. `portfolio_snapshots` always gets rows as long
    as there is at least one held symbol, so it's the more reliable half of
    this pair.
    DEBT: a genuinely empty book (nav=0, zero holdings AND zero recommendation
    rows) still wouldn't close the gate — no row of either kind would exist
    to record it. Revive trigger: this becomes reachable once someone spins
    up a fresh, fully-empty portfolio and complains about hourly re-runs.
    """
    async with session_scope() as db:
        rec_week = (await db.execute(select(func.max(Recommendation.report_week)))).scalar_one_or_none()
        snap_week = (await db.execute(select(func.max(PortfolioSnapshot.report_week)))).scalar_one_or_none()
    weeks = [w for w in (rec_week, snap_week) if w]
    return max(weeks) if weeks else None


async def _load_overrides(as_of: date) -> dict[str, tuple[str, float]]:
    """Cooldown context for `AdvisorEngine.evaluate()` — sourced from stored
    `recommendations` rows. Keyed exactly like
    `AdvisorEngine._cooldown_key`: `"rule_id:SYMBOL"` for rule 1's
    per-position rows (symbol read back from `input_snapshot`), bare
    `rule_id` for rule 2/3's single row."""
    cutoff = as_of - timedelta(weeks=52)  # generous bound; advisor.py's own cooldown_weeks governs suppression
    async with session_scope() as db:
        rows = (await db.execute(
            select(Recommendation).where(
                Recommendation.status == "overridden", Recommendation.as_of >= cutoff,
            ).order_by(Recommendation.created_at.desc())
        )).scalars().all()
    overrides: dict[str, tuple[str, float]] = {}
    for r in rows:
        symbol = r.input_snapshot.get("symbol") if r.rule_id == "weight_band_trim" else ""
        key = f"{r.rule_id}:{symbol}" if symbol else r.rule_id
        if key in overrides:
            continue  # newest wins — rows are ordered created_at desc
        metric = (r.tax_cost_arithmetic or {}).get("benefit_eur")
        if metric is None:
            metric = r.input_snapshot.get("shortfall_eur", 0.0)
        overrides[key] = (r.report_week, float(metric))
    return overrides


async def _persist_snapshots(db: AsyncSession, rows: list[dict[str, Any]]) -> None:
    """Takes an already-open session so the caller
    can persist snapshots + recommendations in ONE transaction — a week is
    all-or-nothing, no crash window where snapshots commit but the
    recommendations computed from them are lost (or vice versa). A single
    multi-row `INSERT … VALUES (...), (...), ...` rather than one
    statement per row."""
    if not rows:
        return
    stmt = pg_insert(PortfolioSnapshot).values(rows).on_conflict_do_nothing(
        index_elements=["report_week", "symbol"],
    )
    await db.execute(stmt)


async def _persist_recommendations(
    db: AsyncSession, rows: list[dict[str, Any]], regime_component_set_id: str | None,
) -> None:
    """Same shared-session contract as `_persist_snapshots`."""
    if not rows:
        return
    for r in rows:
        if r["status"] not in _VALID_STATUSES:
            raise ValueError(f"refusing to persist recommendation with invalid status: {r['status']!r}")
    for r in rows:
        db.add(Recommendation(**{**r, "regime_component_set_id": regime_component_set_id}))


async def _persist_narration(report_week: str, facts: dict[str, Any], narration: dict[str, Any]) -> None:
    """Reuses `ai_runs` (no new table) — `kind="narrate"`, `config` carries
    `report_week` since the table has no column of its own for it."""
    async with session_scope() as db:
        db.add(AiRun(
            job_id=str(uuid.uuid4()), kind="narrate", config={"report_week": report_week},
            status="done", progress=1.0, phase="done", result={"facts": facts, **narration},
        ))
        await db.commit()


# ── orchestration (the tick's + the manual test hook's single entry point) ──
async def generate_weekly_report(
    redis: Redis, settings: Settings, *, now: datetime | None = None,
) -> dict[str, Any] | None:
    """Returns the assembled report on a due run, or `None` on a skip (the
    current ISO week's report already exists — no DB write happens at all in
    that case, not even a read past `_last_report_week()`).

    DEMO_MODE: a total no-op, before any DB read or live fetch. The
    weekly tick (`main.py`'s `_advisor_weekly_tick_loop`, this function's
    only caller) must never persist synthetic-derived rows into the real
    `portfolio_snapshots`/`recommendations` tables, nor fetch live holdings,
    nor probe the real regime/LLM — DEMO_MODE reports are served entirely
    from the read route's own seam (`routers/reports.py::_demo_report`),
    which reassembles fresh from fixtures on every read and needs no
    persisted state at all."""
    if settings.demo_mode:
        return None
    now = now or datetime.now(timezone.utc)
    report_week = iso_week(now)
    if not report_due(await _last_report_week(), now):
        return None

    wf = make_wealthfolio(redis, settings)
    holdings = await wf.holdings()
    nav = float(holdings.total)
    cash = float(holdings.cash)
    as_of = now.date()

    regime_state: str | None = None
    regime_composite: float | None = None
    regime_component_set_id: str | None = None
    try:
        regime_report, _ = await MarketResearchEngine(redis, settings).regime()
        regime_state = regime_report.state
        regime_composite = regime_report.score
        regime_component_set_id = component_set_id(regime_report.components)
    except Exception as exc:  # noqa: BLE001 — macro profile down must not crash the tick
        logger.warning("advisor report %s: regime unavailable, section marked absent: %s", report_week, exc)

    snapshot_rows = [
        {
            "report_week": report_week, "date": as_of, "symbol": a.symbol.upper(),
            "qty": a.quantity, "value_eur": a.value,
            # SE4: real per-symbol cost basis when the exporter carries it
            # (nullable column, migration 0011, exactly for this) — `None`
            # for an old/cached payload or an unclassified holding.
            "cost_basis": a.cost_basis,
            "cash": holdings.cash, "nav": holdings.total,
            "regime_composite": regime_composite, "regime_component_set_id": regime_component_set_id,
        }
        for a in holdings.by_asset
    ]

    overrides = await _load_overrides(as_of)
    advisor = make_advisor(redis, settings)
    rows = await advisor.evaluate(as_of, report_week, overrides)
    rule_status = advisor.rule_status()

    # Snapshots + recommendations commit together
    # — a week is all-or-nothing, no crash window between the two writes.
    async with session_scope() as db:
        await _persist_snapshots(db, snapshot_rows)
        await _persist_recommendations(db, rows, regime_component_set_id)
        await db.commit()

    facts = _build_facts(report_week, nav, cash, regime_state, regime_composite, rows, rule_status)
    narration: dict[str, Any] | None = None
    # Two distinct absence reasons: "not_configured" means no probe
    # was even attempted (the `ai` profile isn't up for THIS container — see
    # the module docstring on why that's usually the case today);
    # "probe_failed" means an attempt was made and it came back empty/warned.
    narration_absent_reason: str | None = "not configured in this deployment (ai profile not enabled)"
    if settings.llm_provider != "noop":
        try:
            narration = await _narrate(build_llm_adapter(settings.llm_provider, settings), facts)
        except Exception as exc:  # noqa: BLE001 — a narration failure must never sink the report
            logger.warning("advisor report %s: narration probe raised, shipping un-narrated: %s", report_week, exc)
            narration = None
        narration_absent_reason = None if narration is not None else "narration probe failed (see backend logs)"
    if narration is not None:
        await _persist_narration(report_week, facts, narration)

    report = assemble_report(
        report_week, nav, cash, snapshot_rows, rows, rule_status, regime_state,
        narration, narration_absent_reason,
    )
    logger.info(
        "advisor report %s generated: %d holdings snapshotted, %d recommendation rows, narrated=%s",
        report_week, len(snapshot_rows), len(rows), narration is not None,
    )
    return report
