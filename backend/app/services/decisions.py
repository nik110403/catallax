"""Decision projection — `class DecisionProjection`.

Computes the "actionability contract" for each CURRENT LEAF recommendation
of a week: where the money for it would come from (`funding_source`), what
it does to portfolio risk (`risk_budget_impact`), when it stops being true
(`invalidation`), and when a human should look at it again (`review_at`/
`expires_at`). This is a PROJECTION, never persisted — it is recomputed on
every read from `recommendations` + `portfolio_snapshots` rows (plus, for
`risk_budget_impact` only, LIVE current holdings). Leaves are selected with
the exact same superseded-set rule `routers.reports._current_recommendations`
uses (imported lazily inside `chapter_decisions` — a module-level import
would create routers.reports -> services.decisions -> routers.reports
cycle, since `reports.py` imports `DecisionProjection` to expose the new
`/reports/{week}/decisions` route).

── What "current" means, field by field ────────────────────────────────
`funding_source` and `invalidation` deliberately read the WEEK'S OWN
persisted `portfolio_snapshots` rows (`report_week == report_week` — the
same rows the advisor evaluated against that week), not live holdings: this
keeps those two fields reproducible — re-reading the same week later shows
the same numbers a human reviewing that week's report actually saw.
`risk_budget_impact` is the one field that genuinely needs LIVE current
holdings (the spec's own opening line: "a projection … re-computable from
persisted rows + CURRENT holdings") — projecting "if this recommendation
executes now, what happens to risk" only makes sense against the book as
it stands today, so it fetches holdings fresh via `make_wealthfolio` and
calls `RiskEngine.bundle()` once per `chapter_decisions()` call (F8 —
`recompute_with_weights` requires a prior `bundle()` on the same instance,
exactly the call-order contract `AdvisorEngine._sell_gate` observes). The
weight arithmetic (post-action weight vector from a trim/sell leg) is
COPIED from that sell-gate pattern, read-only — `advisor.py` is frozen and
is never imported here.

DEBT: `invalidation`'s "current" figures come from the SAME week's snapshot
that produced the recommendation, so a freshly-proposed row always reads as
`still_valid=True` (nothing has changed yet, by construction) — this only
becomes informative once a leaf survives into a LATER week's read of this
same chapter. A live-holdings re-check would be more accurate for a
genuinely stale, still-`proposed` leaf. Revive trigger: a recommendation is
observed sitting `proposed` across more than one `report_week` in
production (today's `expire_and_supersede` rotates most leaves weekly).

── Snapshot keys read (documented once, per the ground rules) ──────────
`weight_band_trim` rows carry `input_snapshot["symbol"]`,
`["trim_value_eur"]`, `["band_upper"]` (no lower edge is tracked — treated
as 0 for the invalidation band). `cash_floor_band` rows carry a sell list
under `input_snapshot["legs"]` (`[{"symbol", "trim_value_eur", ...}]`) plus
`["regime_band_pct"]`/`["cash_equivalents_eur"]`; its "refuse" branch (the
clearing-accounts guard) carries none of those — its invalidation degrades
to `still_valid: None`. `dca_modulation` carries `["contribution_eur"]`,
no symbol. Tax lots are Parked (roadmap QUESTIONS) — `funding_source`
says so explicitly (`"lots": "not modelled (Parked)"`) rather than omitting
the concept silently.

`forecast`/`outcome` are `None` in v1 (M9 — advisor-issued forecasts are
out of scope; a rule's "did this work?" question is `invalidation.
still_valid` instead). Keys are always present so the row shape is stable
for a future Phase-19 `advisor:<rule_id>` issuer to fill in.
"""
from __future__ import annotations

import logging
from datetime import timedelta
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select

from app.config import Settings
from app.db import session_scope
from app.models import PortfolioSnapshot, Recommendation
from app.services.risk import RiskEngine
from app.services.wealthfolio import make_wealthfolio

logger = logging.getLogger("catallax.decisions")

_HOW_TO_READ = (
    "Each row is one CURRENT leaf recommendation (superseded/expired history "
    "collapses to its latest state, exactly like the weekly report). `symbol` "
    "is `input_snapshot`'s own symbol verbatim (null for rule_ids that don't "
    "key off one, e.g. `dca_modulation`, or that carry more than one under "
    "`legs`, e.g. `cash_floor_band` — see `funding_source.symbols` there), so "
    "a consumer can join a decision to an instrument without re-reading the "
    "recommendation row. "
    "`funding_source` names where the money comes from — clearing cash for a "
    "DCA buy, sale proceeds for a trim (tax lots are not modelled yet, "
    "Parked). `risk_budget_impact` projects the CVaR95 delta of executing "
    "the action against TODAY's live holdings (an `unavailable` reason "
    "replaces it when there's no action, no symbol-level leg, or the risk "
    "engine itself failed — never a silently missing key). `invalidation` "
    "states the measurable condition that would make the recommendation "
    "moot, evaluated against the same week's own portfolio snapshot — a "
    "freshly-proposed row reads `still_valid: true` by construction; it "
    "only becomes informative once a leaf survives into a later week. "
    "`review_at`/`expires_at` are the advisor's own review/expiry horizons "
    "applied to this row's `as_of` date. `forecast`/`outcome` are null in "
    "v1 — advisor recommendations are risk actions, not directional views; "
    "Phase 19 may add a projection-resolved `advisor:<rule_id>` claim."
)


# ── pure helpers (no I/O) ────────────────────────────────────────────────
def _funding_source(rec: Recommendation, *, cash_eur: float, clearing_account: str) -> dict[str, Any]:
    snap = rec.input_snapshot or {}
    if rec.sizing is None:
        return {"kind": "none", "reason": f"row status '{rec.status}' — no action, nothing to fund"}
    if rec.rule_id == "dca_modulation":
        amount = snap.get("contribution_eur")
        return {
            "kind": "clearing_cash", "account": clearing_account, "amount_eur": amount,
            "available_eur": round(cash_eur, 2),
            "covers": amount is not None and cash_eur >= amount,
        }
    if rec.rule_id == "weight_band_trim":
        symbol = snap.get("symbol")
        return {
            "kind": "proceeds", "symbols": [symbol] if symbol else [],
            "est_proceeds_eur": snap.get("trim_value_eur"), "to": "cash",
            "lots": "not modelled (Parked)",
        }
    if rec.rule_id == "cash_floor_band":
        legs = snap.get("legs") or []
        return {
            "kind": "proceeds", "symbols": [leg["symbol"] for leg in legs if "symbol" in leg],
            "est_proceeds_eur": round(sum(leg.get("trim_value_eur", 0.0) for leg in legs), 2),
            "to": "cash", "lots": "not modelled (Parked)",
        }
    return {"kind": "unknown", "reason": f"no funding-source rule for rule_id={rec.rule_id!r}"}


def _invalidation(rec: Recommendation, *, value_by_symbol: dict[str, float], nav: float, cash: float) -> dict[str, Any]:
    snap = rec.input_snapshot or {}
    if rec.rule_id == "weight_band_trim":
        symbol = snap.get("symbol")
        band_upper = snap.get("band_upper")
        current_weight = (value_by_symbol.get(symbol, 0.0) / nav) if nav else 0.0
        band_label = f"{band_upper:.4f}" if band_upper is not None else "n/a"
        return {
            "text": f"weight of {symbol} back inside band [0, {band_label}]",
            "current_weight": round(current_weight, 6),
            "still_valid": (current_weight > band_upper) if band_upper is not None else None,
        }
    if rec.rule_id == "cash_floor_band":
        band_pct = snap.get("regime_band_pct")
        liquid = cash + (snap.get("cash_equivalents_eur") or 0.0)
        current_ratio = (liquid / nav) if nav else 0.0
        band_label = f"{band_pct:.4f}" if band_pct is not None else "n/a"
        return {
            "text": f"cash >= {band_label} x NAV",
            "current": round(current_ratio, 6),
            "still_valid": (current_ratio < band_pct) if band_pct is not None else None,
        }
    if rec.rule_id == "dca_modulation":
        return {"text": "contribution already made this period", "still_valid": rec.status != "executed"}
    return {"text": f"no invalidation rule for {rec.rule_id}", "still_valid": None}


def _lifecycle_chain(rec: Recommendation, by_id: dict[int, Recommendation]) -> list[dict[str, Any]]:
    """Root -> leaf order. `report_week` never changes across a chain
    (`recommendation_lifecycle._child_row` always copies the prior's), so
    the whole chain is already inside the rows fetched for THIS week."""
    chain: list[Recommendation] = []
    node: Recommendation | None = rec
    while node is not None:
        chain.append(node)
        node = by_id.get(node.prior_id) if node.prior_id is not None else None
    chain.reverse()
    return [
        {
            "id": r.id, "status": r.status,
            "human_action": (r.input_snapshot or {}).get("human_action"),
            "created_at": r.created_at.isoformat(),
            "human_note": (r.input_snapshot or {}).get("human_note"),
        }
        for r in chain
    ]


def _legs_for(rec: Recommendation) -> list[tuple[str, float]]:
    """Symbol/trim-value legs a fired sell-side action would raise. Empty
    for a no-fire row, and always empty for `dca_modulation` (no symbol,
    F3) — the caller distinguishes those two `unavailable` reasons."""
    if rec.sizing is None:
        return []
    snap = rec.input_snapshot or {}
    if rec.rule_id == "weight_band_trim":
        symbol, value = snap.get("symbol"), snap.get("trim_value_eur")
        return [(symbol, value)] if symbol and value is not None else []
    if rec.rule_id == "cash_floor_band":
        return [
            (leg["symbol"], leg["trim_value_eur"])
            for leg in (snap.get("legs") or []) if "symbol" in leg and "trim_value_eur" in leg
        ]
    return []


class DecisionProjection:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings

    async def chapter_decisions(self, report_week: str) -> dict[str, Any]:
        from app.routers.reports import _current_recommendations  # lazy — see module docstring

        async with session_scope() as db:
            rec_rows = (await db.execute(
                select(Recommendation).where(Recommendation.report_week == report_week)
                .order_by(Recommendation.created_at)
            )).scalars().all()
            snap_rows = (await db.execute(
                select(PortfolioSnapshot).where(PortfolioSnapshot.report_week == report_week)
            )).scalars().all()

        by_id = {r.id: r for r in rec_rows}
        leaves = _current_recommendations(rec_rows)
        nav = float(snap_rows[0].nav) if snap_rows else 0.0
        cash = float(snap_rows[0].cash) if snap_rows else 0.0
        value_by_symbol = {r.symbol: float(r.value_eur) for r in snap_rows}
        clearing_account = (self._settings.advisor_clearing_accounts_list or ["unspecified"])[0]

        risk_ctx, risk_error = await self._risk_context(leaves)

        expiry_weeks = self._settings.recommendation_expiry_weeks or self._settings.advisor_review_horizon_weeks
        rows: list[dict[str, Any]] = []
        for rec in leaves:
            rows.append({
                "id": rec.id, "rule_id": rec.rule_id, "action": rec.action, "status": rec.status,
                "sizing": rec.sizing, "as_of": rec.as_of.isoformat(), "report_week": rec.report_week,
                "symbol": (rec.input_snapshot or {}).get("symbol"),
                "funding_source": _funding_source(rec, cash_eur=cash, clearing_account=clearing_account),
                "risk_budget_impact": await self._risk_budget_impact(rec, risk_ctx, risk_error),
                "invalidation": _invalidation(rec, value_by_symbol=value_by_symbol, nav=nav, cash=cash),
                "review_at": (rec.as_of + timedelta(weeks=self._settings.advisor_review_horizon_weeks)).isoformat(),
                "expires_at": (rec.as_of + timedelta(weeks=expiry_weeks)).isoformat(),
                "forecast": None, "outcome": None,
                "lifecycle": _lifecycle_chain(rec, by_id),
            })

        counts: dict[str, int] = {}
        for r in rows:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        no_action_rate = (counts.get("evaluated", 0) / len(rows)) if rows else 0.0

        return {
            "engine": "decision-projection-v1", "rows": rows, "counts": counts,
            "no_action_rate": no_action_rate, "how_to_read": _HOW_TO_READ,
        }

    # ── risk_budget_impact plumbing (F8: one RiskEngine, one bundle() call) ──
    async def _risk_context(self, leaves: list[Recommendation]) -> tuple[dict[str, Any] | None, str | None]:
        needs_risk = any(
            r.rule_id in ("weight_band_trim", "cash_floor_band") and r.sizing is not None for r in leaves
        )
        if not needs_risk:
            return None, None
        try:
            holdings = await make_wealthfolio(self._redis, self._settings).holdings()
            live_nav = float(holdings.total)
            ce_symbols = self._settings.advisor_cash_equivalent_symbols_set
            current_weights = (
                {
                    a.symbol.upper(): float(a.value) / live_nav
                    for a in holdings.by_asset
                    if float(a.value) > 0 and a.symbol.upper() not in ce_symbols
                }
                if live_nav > 0 else {}
            )
            engine = RiskEngine(self._redis, self._settings)
            await engine.bundle()  # exactly once per chapter call, before any recompute_with_weights
            return {"engine": engine, "current_weights": current_weights, "nav": live_nav, "cache": {}}, None
        except Exception as exc:  # noqa: BLE001 — a risk-engine outage degrades this field only
            logger.warning("decisions: risk context unavailable: %s", exc, exc_info=True)
            return None, "risk engine unavailable"

    async def _risk_budget_impact(
        self, rec: Recommendation, risk_ctx: dict[str, Any] | None, risk_error: str | None,
    ) -> dict[str, Any]:
        legs = _legs_for(rec)
        if not legs:
            if rec.sizing is None:
                return {"unavailable": f"row status '{rec.status}' — no action, nothing to project"}
            return {"unavailable": f"{rec.rule_id} has no symbol-level leg — no post-action weight vector"}
        if risk_ctx is None:
            return {"unavailable": risk_error or "risk engine unavailable"}
        engine, current_weights, nav, cache = (
            risk_ctx["engine"], risk_ctx["current_weights"], risk_ctx["nav"], risk_ctx["cache"],
        )
        try:
            post_weights = dict(current_weights)
            for symbol, trim_eur in legs:
                delta = (trim_eur / nav) if nav else 0.0
                post_weights[symbol] = max(post_weights.get(symbol, 0.0) - delta, 0.0)

            async def _weighted(weights: dict[str, float]):
                sig = tuple(sorted(weights.items()))
                if sig not in cache:
                    cache[sig] = await engine.recompute_with_weights(weights, frequency="weekly")
                return cache[sig]

            current_var, _ = await _weighted(current_weights)
            post_var, _ = await _weighted(post_weights)
            now_pct, post_pct = current_var.cvar_pct or 0.0, post_var.cvar_pct or 0.0
            return {"cvar95_now": now_pct, "cvar95_post": post_pct, "delta_pct": round(post_pct - now_pct, 6)}
        except Exception as exc:  # noqa: BLE001 — the row still ships, just without this field
            logger.warning("decisions: risk_budget_impact computation failed: %s", exc, exc_info=True)
            return {"unavailable": "risk engine unavailable"}
