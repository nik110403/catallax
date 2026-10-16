"""Portfolio Advisor — deterministic, config-driven rule engine
over the portfolio. Never places a trade: every evaluation produces a
`recommendations`-row-shaped dict (see `app.models.Recommendation`) that a
human accepts/overrides and a later task persists. Rules never reference
the DB directly — cooldown state (which rules the user recently overrode) is
supplied by the caller via `overrides`, not queried here.

Each row's `as_of` is a native `datetime.date` (matches `Recommendation.as_of`'s
Date column — asyncpg rejects a plain ISO string there); every other field is
JSON-safe (`report_week` stays a str, `input_snapshot`/`tax_cost_arithmetic`
are plain dicts) since those columns are String/JSONB.

Three rules (design-draft.md §1):
  1. weight_band_trim — every position beyond its asset-class weight band is
     proposed for a trim back to the band edge (hysteresis: never to some
     lower target); every position evaluated (in-band or not) gets its own
     audit row. Crypto gets its own (wider) band — a volatile asset class
     gets a wider band, else one global band would fire on it every week. Cash-equivalent holdings
     (ADVISOR_CASH_EQUIVALENT_SYMBOLS, e.g. stablecoins) are excluded from
     this rule entirely — not trimmable, no per-symbol audit row, no
     "unclassified asset class" row either — collapsed into ONE summary row
     instead, so "every evaluation is a row" still holds without one row per
     stablecoin. The gain-trigger variant
     (real cost basis) is LIVE (SE4): the Holdings bridge carries `cost_basis`
     when the exporter provides it, and the sell gate's tax leg uses the real
     taxable gain (`value - cost_basis`, floored at 0) instead of the full
     trim value. Absent-tolerant per leg — a holding without basis (old/cached
     exporter payload) falls back to the conservative upper bound for that
     leg only; `tax_cost_arithmetic.cost_basis_available` is `True` only when
     every trimmed leg had real basis. ΔCVaR-gated (see "Sell gate" below).
  2. cash_floor_band — **WF-only, investment-only** (design decision,
     supersedes an earlier review fix): target liquid € = max(life floor,
     regime band). Liquid measure = Wealthfolio cash + cash-equivalent
     holdings (ADVISOR_CASH_EQUIVALENT_SYMBOLS, e.g. stablecoins) ONLY —
     ledger/bank cash is excluded from advisor input and is NOT
     folded in. Life floor = ADVISOR_LIFE_FLOOR_MULTIPLIER ×
     trailing-12mo avg monthly Expenses (LedgerBridge BQL aggregate) —
     **dormant by default** (`ADVISOR_LIFE_FLOOR_MULTIPLIER=0`): the rule then
     targets the regime band only, and makes NO ledger call at all — the
     expenses BQL, the ledger-staleness computation, AND the
     ADVISOR_CLEARING_ACCOUNTS refusal guard below all exist solely to protect
     this ledger-derived leg, so all three go dormant together with it (no
     ledger inputs -> no guard needed). Setting the multiplier > 0 re-enables
     the whole leg (expenses/staleness/guard included) with no code change.
     When active: refuses to size anything unless every
     ADVISOR_CLEARING_ACCOUNTS account (and its sub-accounts) sums to 0 (the
     nonzero clearing balance) — the refusal itself is an output row with the
     reason. **NOT ΔCVaR-gated**: a pro-rata liquidity
     raise barely moves portfolio CVaR, so gating it on the sell gate
     recreates a "dead rule" — a floor breach that clears
     materiality + cooldown PROPOSES the raise directly; its row still carries
     the tax/venue cost arithmetic, but as labeled information for the human
     veto (the actual safety layer here), never as a firing condition.
  3. dca_modulation — DORMANT while ADVISOR_DCA_BASE=0 (no investable income
     by default): evaluates to a pure no-op and emits
     NOT EVEN an `evaluated` row. `rule_status()` reports it to callers as
     "inactive (no contribution schedule set)" for report assembly.
     Setting the base > 0 later activates it with no code change; a future
     sell-side deployment (selling above the cash band) would route through
     the same ΔCVaR sell gate as rule 1 — the buy-side path implemented today
     never needs it (no realized gain on a buy).

Sell gate (design-draft.md, signed off 2026-08-15) — rule 1 (and any future
sell-side DCA) only: benefit = Δ weekly CVaR95 (via
`RiskEngine.recompute_with_weights`, post-trim vs current weights), scaled to
`ADVISOR_REVIEW_HORIZON_WEEKS` via sqrt-time (i.i.d. weekly-return assumption,
printed in every gate's arithmetic), minus the expected-return give-up of the
trimmed exposure; cost = indicative tax (tax rate × gain × (1 − loss-offset
factor), labeled "indicative estimate, not tax advice") + a per-asset-class
venue transaction cost proxy. Fires iff benefit > cost ×
ADVISOR_BENEFIT_COST_RATIO, subject to a materiality floor (€ AND % NAV, both
required) and a per-rule, per-scope cooldown on recently-overridden
recommendations (scope = symbol for rule 1's per-position rows, unscoped for
rule 2/3's single row per evaluation — an override on one symbol's trim must
never suppress another symbol's).

`RiskEngine.bundle()` is called at most once per `AdvisorEngine` instance,
before the first `recompute_with_weights()` call, per the documented
call-order contract (avoids a second rate-limited fetch during the weekly run).
"""
from __future__ import annotations

import math
import re
from datetime import date, timedelta
from typing import Any

from redis.asyncio import Redis

from app.config import Settings
from app.errors import InvalidRequest
from app.services.ledger import LedgerBridge, make_ledger
from app.services.market_research import MarketResearchEngine
from app.services.risk import Coverage, RiskEngine, VarReport
from app.services.wealthfolio import Holdings, WealthfolioBridge, make_wealthfolio

# Service layer is the enum authority for `Recommendation.status` (the DB
# column is an unconstrained String(16)).
_VALID_STATUSES = {"evaluated", "proposed", "accepted", "overridden", "executed"}
RULE_IDS = ("weight_band_trim", "cash_floor_band", "dca_modulation")

_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9:_-]{1,128}$")


def _weeks_since(report_week: str, as_of: date) -> int:
    """`report_week` is an ISO-week label ("YYYY-Www"). Unparsable input is
    treated as "long ago" so a malformed cooldown record never wrongly
    suppresses a live recommendation."""
    try:
        year_s, week_s = report_week.split("-W")
        d = date.fromisocalendar(int(year_s), int(week_s), 1)
    except (ValueError, IndexError):
        return 10**6
    return max((as_of - d).days // 7, 0)


def _cooldown_key(rule_id: str, scope: str) -> str:
    return f"{rule_id}:{scope}" if scope else rule_id


class AdvisorEngine:
    def __init__(
        self,
        redis: Redis,
        settings: Settings,
        wf: WealthfolioBridge | None = None,
        ledger: LedgerBridge | None = None,
        risk: RiskEngine | None = None,
        regime: MarketResearchEngine | None = None,
    ) -> None:
        self._settings = settings
        self._wf = wf if wf is not None else make_wealthfolio(redis, settings)
        self._ledger = ledger if ledger is not None else make_ledger(redis, settings)
        self._risk = risk if risk is not None else RiskEngine(redis, settings)
        self._regime = regime if regime is not None else MarketResearchEngine(redis, settings)
        # Set on the first `_sell_gate()` call on this instance so `bundle()`
        # fires at most once, ahead of any `recompute_with_weights()` — the documented
        # call-order contract.
        self._bundle_fetched = False

    # ── public API ───────────────────────────────────────────
    def rule_status(self) -> dict[str, str]:
        """Static per-rule status labels for report assembly —
        independent of any `evaluate()` run, since a dormant DCA rule emits
        no rows at all for `evaluate()` to describe."""
        dca = "active" if self._settings.advisor_dca_base > 0 else "inactive (no contribution schedule set)"
        return {"weight_band_trim": "active", "cash_floor_band": "active", "dca_modulation": dca}

    async def evaluate(
        self,
        as_of: date,
        report_week: str,
        overrides: dict[str, tuple[str, float]] | None = None,
    ) -> list[dict[str, Any]]:
        """Run all rules once and return recommendation-row-shaped dicts
        (fired AND non-fired — every evaluation is an audit row, except a
        dormant DCA which emits nothing). `overrides` maps a cooldown key ->
        (report_week the user last overrode it, metric_eur at that time). The
        key is `rule_id` for rule 2/3's single row, or `"rule_id:SYMBOL"` for
        rule 1's per-position rows — the caller sources it from stored
        `recommendations` rows, never queried here."""
        holdings = await self._wf.holdings()
        nav = float(holdings.total)
        cash = float(holdings.cash)
        ce_symbols = self._settings.advisor_cash_equivalent_symbols_set
        # Cash-equivalent holdings (stablecoins) are treated as cash throughout:
        # excluded from rule 1's weight bands, the sell gate's weight vector,
        # AND `current_weights` below (their value simply isn't in the vector —
        # same effect as `recompute_with_weights` imputing the leftover as cash).
        cash_equiv_assets = [a for a in holdings.by_asset if a.symbol.upper() in ce_symbols]
        current_weights = (
            {
                a.symbol.upper(): float(a.value) / nav
                for a in holdings.by_asset
                if float(a.value) > 0 and a.symbol.upper() not in ce_symbols
            }
            if nav > 0
            else {}
        )
        regime_report, _ = await self._regime.regime()
        regime_state = regime_report.state

        rows: list[dict[str, Any]] = []
        rows += await self._rule_weight_band_trim(
            holdings, nav, current_weights, as_of, report_week, overrides, cash_equiv_assets,
        )
        rows += await self._rule_cash_floor(
            holdings, nav, cash, cash_equiv_assets, as_of, report_week, regime_state, overrides,
        )
        rows += self._rule_dca(nav, cash, regime_state, as_of, report_week)
        return rows

    # ── rule 1: weight-band trim (ΔCVaR-gated) ──────────────────────────
    async def _rule_weight_band_trim(
        self,
        holdings: Holdings,
        nav: float,
        current_weights: dict[str, float],
        as_of: date,
        report_week: str,
        overrides: dict[str, tuple[str, float]] | None,
        cash_equiv_assets: list[Any],
    ) -> list[dict[str, Any]]:
        if nav <= 0:
            return []
        bands = self._settings.advisor_weight_band_map
        s = self._settings
        ce_symbols = {a.symbol.upper() for a in cash_equiv_assets}

        rows: list[dict[str, Any]] = []
        for a in holdings.by_asset:
            symbol = a.symbol.upper()
            if symbol in ce_symbols:
                continue  # treated as cash — no per-symbol row (summary row below)
            cls = a.asset_class.upper()
            upper = bands.get(cls)
            value = float(a.value)
            if value <= 0:
                continue
            if upper is None:
                # Controller review (latent finding): an unclassified asset
                # class used to be silently skipped (`continue`, no row at
                # all), contradicting "every evaluation is a row" — every
                # OTHER branch below emits an audit row even when nothing
                # fires, so this one must too.
                rows.append(self._row(
                    rule_id="weight_band_trim", as_of=as_of, report_week=report_week,
                    action="hold", sizing=None, status="evaluated",
                    input_snapshot={
                        "asset_class": cls, "symbol": symbol, "value_eur": round(value, 2),
                        "nav_eur": round(nav, 2),
                    },
                    note=f"unclassified asset class {cls} — no band configured",
                ))
                continue
            weight = value / nav
            if weight <= upper:
                rows.append(self._row(
                    rule_id="weight_band_trim", as_of=as_of, report_week=report_week,
                    action="hold", sizing=None, status="evaluated",
                    input_snapshot={
                        "asset_class": cls, "symbol": symbol, "weight": weight,
                        "band_upper": upper, "nav_eur": round(nav, 2),
                    },
                    note=(
                        f"{cls} band OK: {symbol} at {weight * 100:.2f}% of NAV "
                        f"(band edge {upper * 100:.2f}%)"
                    ),
                ))
                continue

            trim_value = (weight - upper) * nav
            # Proportional share of this position's real cost basis for the
            # trimmed amount (SE4) — `None` (conservative fallback) when the
            # exporter didn't carry basis for this asset.
            cost_basis_eur = (
                float(a.cost_basis) * (trim_value / value)
                if a.cost_basis is not None and value > 0
                else None
            )
            gate = await self._sell_gate(
                [{
                    "symbol": symbol, "asset_class": cls, "trim_value_eur": trim_value,
                    "cost_basis_eur": cost_basis_eur,
                }],
                current_weights, nav,
            )
            material = _is_material(trim_value, nav, s)
            suppressed, cooldown_note = self._cooldown_check(
                "weight_band_trim", symbol, as_of, gate["benefit_eur"], overrides
            )
            fires = gate["fires"] and material and not suppressed
            notes = [
                f"{symbol} at {weight * 100:.2f}% of NAV, band edge {upper * 100:.2f}% "
                f"-> propose trimming €{trim_value:.2f} back to the edge"
            ]
            if not material:
                notes.append("below materiality floor")
            if suppressed:
                notes.append(cooldown_note or "cooldown")
            rows.append(self._row(
                rule_id="weight_band_trim", as_of=as_of, report_week=report_week,
                action="trim" if fires else "hold", sizing=trim_value if fires else None,
                status="proposed" if fires else "evaluated",
                input_snapshot={
                    "asset_class": cls, "symbol": symbol, "weight": weight, "band_upper": upper,
                    "trim_value_eur": round(trim_value, 2), "nav_eur": round(nav, 2), "material": material,
                    "materiality_eur_threshold": s.advisor_materiality_eur,
                    "materiality_nav_pct_threshold": s.advisor_materiality_nav_pct,
                },
                tax_cost_arithmetic=gate, note="; ".join(notes),
            ))

        if cash_equiv_assets:
            # One summary row for every cash-equivalent holding combined —
            # not per-symbol, not "unclassified" — so "every evaluation is a
            # row" still holds without spamming one row per stablecoin.
            ce_value = sum(float(a.value) for a in cash_equiv_assets)
            rows.append(self._row(
                rule_id="weight_band_trim", as_of=as_of, report_week=report_week,
                action="hold", sizing=None, status="evaluated",
                input_snapshot={
                    "cash_equivalent_symbols": sorted(ce_symbols),
                    "n_cash_equivalents": len(cash_equiv_assets),
                    "cash_equivalents_eur": round(ce_value, 2), "nav_eur": round(nav, 2),
                },
                note=f"cash-equivalents: {len(cash_equiv_assets)} symbols, €{ce_value:.2f} — treated as cash",
            ))
        return rows

    # ── rule 2: cash floor + band (NOT ΔCVaR-gated — controller ruling) ──
    async def _rule_cash_floor(
        self,
        holdings: Holdings,
        nav: float,
        cash: float,
        cash_equiv_assets: list[Any],
        as_of: date,
        report_week: str,
        regime_state: str,
        overrides: dict[str, tuple[str, float]] | None,
    ) -> list[dict[str, Any]]:
        s = self._settings
        life_floor_active = s.advisor_life_floor_multiplier > 0
        ce_symbols = {a.symbol.upper() for a in cash_equiv_assets}
        cash_equiv_eur = sum(float(a.value) for a in cash_equiv_assets)

        def _note(msg: str) -> str:
            # Life floor off = the whole rule sizes from WF data alone; state
            # that as honestly as DCA states its own dormancy.
            if life_floor_active:
                return msg
            return f"life floor inactive (multiplier 0) — targeting the regime cash band only; {msg}"

        # The expenses BQL, ledger staleness, AND the clearing-accounts guard
        # below all exist solely to protect the ledger-derived life-floor leg
        # — dormant together with it (design decision: no ledger
        # inputs -> no guard needed; WF-only, investment-only advisor).
        staleness = await self._ledger_staleness_days(as_of) if life_floor_active else None
        if life_floor_active:
            clearing_accounts = s.advisor_clearing_accounts_list
            if clearing_accounts:
                clearing_sum = 0.0
                for acct in clearing_accounts:
                    clearing_sum += await self._clearing_balance(acct)
                if abs(clearing_sum) > 1e-6:
                    # Demo scrub (security review, 2026-08-16): DEMO_MODE runs
                    # this real evaluate() over real Settings — the refusal
                    # row must never echo real ledger account paths into a
                    # payload a demo viewer can read. Scrub at the source
                    # (never the real list under demo_mode), not downstream.
                    snap_accounts = ["Assets:Demo:Clearing"] if s.demo_mode else clearing_accounts
                    return [self._row(
                        rule_id="cash_floor_band", as_of=as_of, report_week=report_week,
                        action="refuse", sizing=None, status="evaluated",
                        input_snapshot={
                            "clearing_accounts": snap_accounts, "clearing_sum_eur": round(clearing_sum, 2),
                            "ledger_staleness_days": staleness,
                        },
                        note=(
                            f"refusing to size the cash-floor rule: clearing accounts sum to "
                            f"€{clearing_sum:.2f} (expected 0)"
                        ),
                    )]

        # Liquid measure = Wealthfolio cash + cash-equivalent holdings ONLY
        # (design decision — supersedes an earlier ledger-cash
        # fix): bank/ledger cash is excluded from advisor input.
        # NAV stays the investment book, UNCHANGED — rule 1's weights must not
        # move off this measure; only the liquid comparison below is affected.
        cash_wf_eur = cash
        liquid_eur = cash_wf_eur + cash_equiv_eur

        if life_floor_active:
            expenses = await self._trailing_12mo_avg_monthly_expenses(as_of)
            life_floor_eur: float | None = s.advisor_life_floor_multiplier * expenses
        else:
            expenses = None
            life_floor_eur = None
        band_pct = s.advisor_cash_band_map.get(
            regime_state, max(s.advisor_cash_band_map.values(), default=0.0)
        )
        band_eur = band_pct * nav
        target_eur = max(life_floor_eur, band_eur) if life_floor_eur is not None else band_eur
        shortfall = target_eur - liquid_eur
        liquid_desc = f"€{liquid_eur:.2f} (WF €{cash_wf_eur:.2f} + cash-equivalents €{cash_equiv_eur:.2f})"
        snap = {
            "cash_eur": round(cash_wf_eur, 2), "cash_wf_eur": round(cash_wf_eur, 2),
            "cash_equivalents_eur": round(cash_equiv_eur, 2),
            "liquid_eur": round(liquid_eur, 2), "nav_eur": round(nav, 2),
            "avg_monthly_expenses_eur": round(expenses, 2) if expenses is not None else None,
            "life_floor_eur": round(life_floor_eur, 2) if life_floor_eur is not None else None,
            "life_floor_active": life_floor_active,
            "regime_state": regime_state, "regime_band_pct": band_pct, "regime_band_eur": round(band_eur, 2),
            "target_liquid_eur": round(target_eur, 2), "ledger_staleness_days": staleness,
        }
        if shortfall <= 0:
            return [self._row(
                rule_id="cash_floor_band", as_of=as_of, report_week=report_week,
                action="hold", sizing=None, status="evaluated", input_snapshot=snap,
                note=_note(f"liquid cash {liquid_desc} already meets the €{target_eur:.2f} target; no action"),
            )]

        # Cash-equivalents are already counted as liquid above — they aren't
        # investment exposure to trim (selling "cash" to raise cash is a
        # no-op), so both the trimmable base and the pro-rata legs below
        # exclude them, mirroring rule 1's treatment.
        investable = nav - cash_wf_eur - cash_equiv_eur
        if investable <= 0:
            return [self._row(
                rule_id="cash_floor_band", as_of=as_of, report_week=report_week,
                action="hold", sizing=None, status="evaluated", input_snapshot=snap,
                note=_note(f"€{shortfall:.2f} short of the cash target but no investable positions to trim"),
            )]

        fraction = min(shortfall / investable, 1.0)
        raw_legs = [
            {"symbol": a.symbol.upper(), "asset_class": a.asset_class.upper(),
             "trim_value_eur": float(a.value) * fraction,
             # Same proportional share (SE4) — the pro-rata trim fraction
             # applies equally to a position's cost basis as to its value.
             "cost_basis_eur": float(a.cost_basis) * fraction if a.cost_basis is not None else None}
            for a in holdings.by_asset if float(a.value) > 0 and a.symbol.upper() not in ce_symbols
        ]
        # Cost gate item 1a: drop any leg too small to be
        # worth trimming on its own — the existing per-row materiality floor
        # (`_is_material`, €/NAV% both required) applied leg-by-leg, not just
        # to the shortfall total. A dropped leg's contribution simply isn't
        # raised this week; the gap it leaves is stated as a residual.
        trimmed: list[dict[str, Any]] = []
        dropped_legs: list[dict[str, Any]] = []
        for t in raw_legs:
            (trimmed if _is_material(t["trim_value_eur"], nav, s) else dropped_legs).append(t)
        achievable_eur = sum(t["trim_value_eur"] for t in trimmed)
        residual_eur = max(shortfall - achievable_eur, 0.0)

        material = _is_material(shortfall, nav, s)
        legs = [
            {
                "symbol": t["symbol"], "asset_class": t["asset_class"],
                "trim_value_eur": round(t["trim_value_eur"], 2),
                # M3: persisted so a mixed-basis row (some legs real, some
                # conservative) is auditable per leg, not just via the row's
                # single top-level `cost_basis_available` flag.
                "cost_basis_eur": round(t["cost_basis_eur"], 2) if t.get("cost_basis_eur") is not None else None,
            }
            for t in trimmed
        ]
        base_snap = {
            **snap, "shortfall_eur": round(shortfall, 2), "material": material, "legs": legs,
            "materiality_eur_threshold": s.advisor_materiality_eur,
            "materiality_nav_pct_threshold": s.advisor_materiality_nav_pct,
            "raise_eur": round(achievable_eur, 2), "residual_eur": round(residual_eur, 2),
            "n_legs_dropped_sub_materiality": len(dropped_legs),
        }

        if not trimmed:
            # Every candidate leg was below the per-leg materiality floor —
            # nothing achievable this week, same posture as "no investable
            # positions" above, just discovered one step later.
            return [self._row(
                rule_id="cash_floor_band", as_of=as_of, report_week=report_week,
                action="hold", sizing=None, status="evaluated", input_snapshot=base_snap,
                note=_note(
                    f"€{shortfall:.2f} short of the €{target_eur:.2f} cash target, but every candidate "
                    f"trim leg is below the per-leg materiality floor (€{s.advisor_materiality_eur:.2f} "
                    f"or {s.advisor_materiality_nav_pct:.2f}% NAV) — nothing achievable this week"
                ),
            )]

        cost = self._tax_and_venue(trimmed)
        cost_fraction = cost["cost_eur"] / achievable_eur if achievable_eur else 0.0
        cost_exceeds = cost_fraction > s.advisor_cost_fraction_max
        suppressed, cooldown_note = self._cooldown_check(
            "cash_floor_band", "", as_of, shortfall, overrides
        )
        fires = material and not suppressed and not cost_exceeds
        notes = [
            f"liquid cash {liquid_desc} short of the €{target_eur:.2f} target by €{shortfall:.2f}; "
            "propose a pro-rata trim across holdings to raise it — NOT risk-gated (a liquidity "
            "raise barely moves CVaR); tax/venue cost below is informational, the accept/override "
            "decision is the safety layer"
        ]
        if dropped_legs:
            notes.append(
                f"{len(dropped_legs)} leg(s) below the per-leg materiality floor excluded; "
                f"€{achievable_eur:.2f} achievable, €{residual_eur:.2f} residual unaddressed"
            )
        if not material:
            notes.append("below materiality floor")
        if cost_exceeds:
            # Cost gate item 1b: benefit isn't ΔCVaR here, but cost
            # still gates emission — e.g. a fixed venue cost that dwarfs a small raise.
            notes.append(
                f"cash-floor raise suppressed: cost €{cost['cost_eur']:.2f} = {cost_fraction * 100:.1f}% of "
                f"€{achievable_eur:.2f} raise exceeds the {s.advisor_cost_fraction_max * 100:.0f}% floor"
            )
        if suppressed:
            notes.append(cooldown_note or "cooldown")
        return [self._row(
            rule_id="cash_floor_band", as_of=as_of, report_week=report_week,
            action="trim" if fires else "hold", sizing=achievable_eur if fires else None,
            status="proposed" if fires else "evaluated",
            input_snapshot={
                **base_snap, "cost_fraction": round(cost_fraction, 4),
                "cost_fraction_max": s.advisor_cost_fraction_max,
            },
            tax_cost_arithmetic=cost, note=_note("; ".join(notes)),
        )]

    # ── rule 3: DCA modulation (dormant while ADVISOR_DCA_BASE=0) ────────
    def _rule_dca(
        self, nav: float, cash: float, regime_state: str, as_of: date, report_week: str,
    ) -> list[dict[str, Any]]:
        s = self._settings
        if s.advisor_dca_base <= 0 or nav <= 0:
            return []
        scale = s.advisor_dca_regime_scale_map.get(regime_state, 1.0)
        contribution = s.advisor_dca_base * scale
        band_pct = s.advisor_cash_band_map.get(
            regime_state, max(s.advisor_cash_band_map.values(), default=0.0)
        )
        cash_pct = cash / nav
        snap = {
            "cash_pct": cash_pct, "band_pct": band_pct, "contribution_eur": round(contribution, 2),
            "regime_state": regime_state, "regime_scale": scale,
        }
        if cash_pct <= band_pct:
            return [self._row(
                rule_id="dca_modulation", as_of=as_of, report_week=report_week,
                action="hold", sizing=None, status="evaluated", input_snapshot=snap,
                note="cash at/below the target band; DCA deployment held back to protect the floor",
            )]
        return [self._row(
            rule_id="dca_modulation", as_of=as_of, report_week=report_week,
            action="buy", sizing=contribution, status="proposed", input_snapshot=snap,
            note=(
                f"cash surplus above the {band_pct * 100:.1f}% band; deploy €{contribution:.2f} "
                f"(regime scale {scale:.2f})"
            ),
        )]

    # ── tax + venue cost (shared: rule 1's gate + rule 2's informational row) ──
    def _tax_and_venue(self, trimmed: list[dict[str, Any]]) -> dict[str, Any]:
        """`trimmed` legs may carry an optional `cost_basis_eur` (SE4) — the
        caller's proportional share of the position's Wealthfolio cost basis
        for the trimmed amount. Per leg: taxable gain = `trim_value_eur -
        cost_basis_eur`, floored at 0, when present; a leg without it
        (old/cached exporter payload, or an unclassified/basis-less holding)
        falls back to the prior conservative upper bound — the full trimmed
        value as taxable gain — for THAT leg only, never blanking the real
        figure on the rest. `cost_basis_available` is `True` only when EVERY
        leg had real basis: a mixed trim's total tax figure is still partly
        conservative, so the row must not claim to be fully live."""
        s = self._settings
        tax_rate = s.advisor_tax_rate * (1.0 - s.advisor_loss_offset_factor)
        total_gain_eur = 0.0
        cost_basis_available = bool(trimmed)
        for t in trimmed:
            cb = t.get("cost_basis_eur")
            if cb is None:
                total_gain_eur += t["trim_value_eur"]
                cost_basis_available = False
            else:
                total_gain_eur += max(t["trim_value_eur"] - cb, 0.0)
        tax_eur = total_gain_eur * tax_rate

        venue_map = s.advisor_venue_cost_class_map
        venue_cost_eur = 0.0
        for t in trimmed:
            mode, value = venue_map.get(t["asset_class"], ("flat", 0.0))
            venue_cost_eur += value if mode == "flat" else t["trim_value_eur"] * value

        return {
            "tax_eur": round(tax_eur, 2), "venue_cost_eur": round(venue_cost_eur, 2),
            "cost_eur": round(tax_eur + venue_cost_eur, 2),
            "cost_basis_available": cost_basis_available,
            "tax_label": "indicative estimate, not tax advice",
        }

    # ── sell gate (rule 1 / future sell-side DCA only — controller ruling) ──
    async def _sell_gate(
        self, trimmed: list[dict[str, Any]], current_weights: dict[str, float], nav: float,
    ) -> dict[str, Any]:
        s = self._settings
        horizon = s.advisor_review_horizon_weeks

        post_weights = dict(current_weights)
        for t in trimmed:
            delta = t["trim_value_eur"] / nav if nav else 0.0
            post_weights[t["symbol"]] = max(post_weights.get(t["symbol"], 0.0) - delta, 0.0)

        if not self._bundle_fetched:
            # Call-order contract: bundle() must run before the first
            # recompute_with_weights() on this instance, and at most once.
            await self._risk.bundle()
            self._bundle_fetched = True
        current_var, current_meta = await self._risk.recompute_with_weights(current_weights, frequency="weekly")
        post_var, post_meta = await self._risk.recompute_with_weights(post_weights, frequency="weekly")
        current_cvar_pct = current_var.cvar_pct or 0.0
        post_cvar_pct = post_var.cvar_pct or 0.0
        delta_cvar_pct = current_cvar_pct - post_cvar_pct

        scaling_assumption = (
            f"weekly CVaR95 delta scaled to the {horizon}-week review horizon via sqrt(time) "
            "(i.i.d. weekly-return assumption)"
        )
        benefit_risk_eur = delta_cvar_pct / 100.0 * nav * math.sqrt(horizon)

        er_map = s.advisor_expected_return_class_map
        cash_er = er_map.get("CASH", 0.0)
        giveup_eur = sum(
            t["trim_value_eur"] * (er_map.get(t["asset_class"], 0.0) - cash_er) * (horizon / 52.0)
            for t in trimmed
        )
        benefit_eur = benefit_risk_eur - giveup_eur

        cost = self._tax_and_venue(trimmed)
        threshold_eur = cost["cost_eur"] * s.advisor_benefit_cost_ratio
        # Round once, then compare — a published row can't otherwise show
        # benefit_eur == threshold_eur with fires:true from unrounded dust.
        benefit_r = round(benefit_eur, 2)
        threshold_r = round(threshold_eur, 2)
        warnings = list(current_meta.get("warnings", [])) + list(post_meta.get("warnings", []))
        # Item 2: risk_lookback_days=504 -> ~71 weekly
        # windows < risk_var_min_scenarios (120), so `_var_block` sets
        # `low_confidence=True` on both VarReports — that flag used to be
        # dropped on the floor here instead of reaching the gate row, so a
        # fired sell gate could show up looking like it ran on full-strength
        # CVaR when it didn't. `or` across current/post (either leg being
        # thin is enough to caveat the whole gate); n_scenarios is carried
        # from the current-weights side (post-trim reuses the same resampled
        # series length under `recompute_with_weights`, so they agree).
        low_confidence = bool(current_var.low_confidence or post_var.low_confidence)
        n_scenarios = current_var.n_scenarios

        return {
            "benefit_eur": benefit_r, "cost_eur": cost["cost_eur"], "tax_eur": cost["tax_eur"],
            "venue_cost_eur": cost["venue_cost_eur"], "ratio": s.advisor_benefit_cost_ratio,
            "threshold_eur": threshold_r, "fires": benefit_r > threshold_r,
            "review_horizon_weeks": horizon,
            "current_cvar_pct": current_cvar_pct, "post_cvar_pct": post_cvar_pct,
            "delta_cvar_pct": round(delta_cvar_pct, 6),
            "expected_return_giveup_eur": round(giveup_eur, 2),
            "scaling_assumption": scaling_assumption,
            "cost_basis_available": cost["cost_basis_available"],
            "tax_label": cost["tax_label"],
            "low_confidence": low_confidence, "n_scenarios": n_scenarios,
            "warnings": warnings,
        }

    # ── cooldown (state supplied by caller — this module makes no DB reads) ───────
    def _cooldown_check(
        self, rule_id: str, scope: str, as_of: date, metric_eur: float,
        overrides: dict[str, tuple[str, float]] | None,
    ) -> tuple[bool, str | None]:
        if not overrides:
            return False, None
        key = _cooldown_key(rule_id, scope)
        entry = overrides.get(key)
        if not entry:
            return False, None
        overridden_week, metric_at_override = entry
        weeks = _weeks_since(overridden_week, as_of)
        s = self._settings
        if weeks >= s.advisor_cooldown_weeks:
            return False, None
        if abs(metric_eur - metric_at_override) >= s.advisor_materiality_eur:
            return False, None
        return True, (
            f"cooldown: overridden {weeks}w ago ({overridden_week}); arithmetic hasn't moved "
            f"materially since (€{metric_at_override:.2f} -> €{metric_eur:.2f})"
        )

    # ── ledger helpers ────────────────────────────────────────
    async def _clearing_balance(self, account: str) -> float:
        if not _ACCOUNT_RE.fullmatch(account):
            raise InvalidRequest(f"Invalid ADVISOR_CLEARING_ACCOUNTS entry: {account!r}")
        rc = self._settings.reporting_currency.upper()
        # Prefix match (like ledger.py/portfolio.py's own account filters) so a
        # parent account (e.g. "Assets:Clearing") also catches its sub-accounts
        # instead of silently summing 0 and passing the guard on an exact match.
        rows = await self._ledger.query_rows(
            f"SELECT convert(sum(position), '{rc}') AS v WHERE account ~ '^{account}'"
        )
        return _inv_float(rows, rc)

    async def _trailing_12mo_avg_monthly_expenses(self, as_of: date) -> float:
        rc = self._settings.reporting_currency.upper()
        start = as_of - timedelta(days=365)
        rows = await self._ledger.query_rows(
            f"SELECT convert(sum(position), '{rc}') AS v WHERE account ~ '^Expenses' "
            f"AND date >= {start.isoformat()} AND date <= {as_of.isoformat()}"
        )
        return abs(_inv_float(rows, rc)) / 12.0

    async def _ledger_staleness_days(self, as_of: date) -> int | None:
        rows = await self._ledger.query_rows("SELECT max(date) AS d")
        if rows and rows[0] and rows[0][0] is not None:
            try:
                last = date.fromisoformat(str(rows[0][0])[:10])
            except ValueError:
                return None
            return (as_of - last).days
        return None

    # ── row builder ───────────────────────────────────────────
    def _row(
        self, *, rule_id: str, as_of: date, report_week: str, action: str,
        sizing: float | None, status: str, input_snapshot: dict[str, Any],
        tax_cost_arithmetic: dict[str, Any] | None = None, note: str | None = None,
    ) -> dict[str, Any]:
        if status not in _VALID_STATUSES:
            raise ValueError(f"invalid recommendation status: {status!r}")
        return {
            "rule_id": rule_id, "as_of": as_of, "report_week": report_week,
            "action": action, "sizing": sizing, "input_snapshot": input_snapshot,
            "regime_component_set_id": None, "status": status,
            "tax_cost_arithmetic": tax_cost_arithmetic, "note": note,
        }


def _inv_float(rows: list, ccy: str) -> float:
    if rows and rows[0] and isinstance(rows[0][0], dict):
        try:
            return float(rows[0][0].get(ccy, 0) or 0)
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _is_material(amount_eur: float, nav: float, settings: Settings) -> bool:
    nav_pct = (amount_eur / nav * 100.0) if nav else 0.0
    return amount_eur >= settings.advisor_materiality_eur and nav_pct >= settings.advisor_materiality_nav_pct


_DEMO_COVERAGE = Coverage(
    n_assets_total=0, n_covered=0, n_skipped=0, covered_weight_pct=100.0,
    n_observations=0, window_start=None, window_end=None, by_reason={},
    cash_weight_pct=0.0, assumption="synthetic demo fixture — no scenario history",
)


class _DemoRisk:
    """DEMO_MODE stand-in for `RiskEngine`'s two methods the sell gate calls
    (`bundle`/`recompute_with_weights`) — deterministic, zero I/O, mirrors
    `tests/test_advisor.py`'s hand-rolled `FakeRisk`: CVaR grows with
    concentration (HHI-like), so a trim that de-concentrates the book always
    lowers CVaR — the same monotone property the real engine guarantees.
    `make_advisor()` injects this under DEMO_MODE so `AdvisorEngine.evaluate()`
    (and therefore the demo reports route) never triggers a live risk-engine
    fetch — no advisor code path may perform live egress under DEMO_MODE."""

    def __init__(self, base_pct: float = 1.0, concentration_scale: float = 20.0) -> None:
        self._base_pct = base_pct
        self._scale = concentration_scale

    async def bundle(self, refresh: bool = False) -> dict[str, Any]:
        return {}

    async def recompute_with_weights(
        self, weights: dict[str, float], frequency: str = "weekly",
    ) -> tuple[VarReport, dict[str, Any]]:
        hhi = sum(w * w for w in weights.values())
        cvar_pct = self._base_pct + self._scale * hhi
        report = VarReport(
            as_of=date.today().isoformat(), confidence=0.95, n_scenarios=0, total_value_eur=0.0,
            var_eur=None, var_pct=None, cvar_eur=None, cvar_pct=cvar_pct, var99_eur=None,
            cvar99_eur=None, gaussian_var_eur=None, fat_tail_ratio=None, hist_vs_gaussian=None,
            worst_day=None, worst_day_pct=None, low_confidence=False, coverage=_DEMO_COVERAGE,
        )
        return report, {"warnings": [], "frequency": frequency}


class _DemoRegimeReport:
    state = "neutral"
    score = 0.0
    components: list[Any] = []


class _DemoRegime:
    """DEMO_MODE stand-in for `MarketResearchEngine.regime()` — a fixed
    neutral state, zero I/O (no live macro-profile fetch under DEMO_MODE)."""

    async def regime(self) -> tuple[_DemoRegimeReport, dict[str, Any]]:
        return _DemoRegimeReport(), {}


def make_advisor(redis: Redis, settings: Settings, *, wf: WealthfolioBridge | None = None) -> AdvisorEngine:
    """The ONE construction seam for `AdvisorEngine` (the `make_wealthfolio`
    pattern) — every call site MUST go through this. Under DEMO_MODE,
    every injected bridge is a synthetic, zero-I/O fixture — `wf`/`ledger`
    via `make_wealthfolio`/`make_ledger`'s own DEMO_MODE branches, `risk`/
    `regime` via `_DemoRisk`/`_DemoRegime` above (neither `RiskEngine` nor
    `MarketResearchEngine` has a demo seam of its own) — so `evaluate()` is
    fully reusable by the demo reports route with no live egress at all.

    `wf` is an optional override: the demo reports
    route's past-week reconciliation passes a week-sampled synthetic
    `Holdings` snapshot instead of the demo bridge's always-current fixture,
    so an archived week's recommendation rows carry THAT week's nav/cash,
    matching the report header, not always today's. `None` (every other
    call site) preserves the exact prior behaviour in both modes."""
    if settings.demo_mode:
        return AdvisorEngine(
            redis, settings, wf=wf if wf is not None else make_wealthfolio(redis, settings),
            ledger=make_ledger(redis, settings), risk=_DemoRisk(), regime=_DemoRegime(),
        )
    return AdvisorEngine(redis, settings, wf=wf)
