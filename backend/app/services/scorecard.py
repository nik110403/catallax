"""Weekly Advisor scorecard — two arms computed from
`portfolio_snapshots` history: "actual" (the real, possibly-rebalanced book)
vs "do-nothing" (buy-and-hold: freeze the earliest available snapshot's
quantities and cash, then revalue only at market prices implied by later
snapshots). Pure functions, no I/O — the router fetches the snapshot rows and
this module turns them into the payload; unit-testable without a DB.

Single stated return convention (printed on every payload via `CONVENTION`):
time-weighted return (TWR), chain-linked across weekly sub-periods, each
sub-period approximated via modified Dietz. `portfolio_snapshots` carries no
per-period external-cash-flow figure today (no contribution/withdrawal ledger
join exists yet, and `ADVISOR_DCA_BASE=0` — dormant, confirmed 2026-08-15) so
every sub-period's cash flow is taken as 0, which collapses modified Dietz to
a simple period return — the CONSEQUENCE: any real
external contribution or withdrawal in a week is indistinguishable from
investment return in these figures. The formula itself is implemented in full
(a non-zero `cash_flow` argument works correctly) so the day `ADVISOR_DCA_BASE`
goes non-zero, wiring in real contribution amounts is a call-site change, not
a math change. Reimplemented from the public-domain modified-Dietz formula
(Dietz, 1966) — no AGPL code, no new dependency.

Controller review round (post-implementation):
  CRITICAL 1 — a sub-period where the two arms return the SAME figure (a tie)
    is not evidence of anything and must never feed the sign-test N — the
    original `n = depth_weeks - 1` did exactly that, so 30 dormant weeks
    (nothing ever diverges) printed `n=29` next to a wildly small
    `p_floor_two_sided` as if 29 independent comparisons had been made.
    `n_effective` now counts ONLY sub-periods where the arms' returns
    actually differ (`math.isclose` tolerance, not bit-exact); `p_floor` is
    `None` whenever `n_effective == 0` — the steady state while the
    advisor's firing rate is ~0.
  CRITICAL 2 — `arms_coincide` used to be derived from quantities alone, so a
    pure cash deposit/withdrawal week (quantities unchanged, but the actual
    book's nav jumps by the deposit while the frozen do-nothing basket
    doesn't) would print "mathematically identical" against two visibly
    different TWRs. `arms_coincide` is now `n_effective == 0` directly — the
    SAME differ-check drives both the N and the coincidence flag, so a
    deposit week (which makes `r_actual != r_donothing` for that
    sub-period) is correctly counted as a divergence rather than silently
    passed through the (cash-blind) quantity check.
"""
from __future__ import annotations

import math
from typing import Any

# ~2 years of weekly snapshots — bounds the read-time history query
# (`routers/reports.py` applies this as a `LIMIT` on distinct report_weeks);
# older weeks, if any, are excluded from both arms.
HISTORY_MAX_WEEKS = 104

CONVENTION = (
    "Time-weighted return (TWR), chain-linked over weekly sub-periods between "
    "portfolio_snapshots rows; each sub-period approximated via modified Dietz "
    "(external cash flow taken as 0 — no per-period contribution/withdrawal "
    "figure is tracked yet, and ADVISOR_DCA_BASE is dormant at 0; consequence: "
    "any external contribution or withdrawal in a week is indistinguishable "
    "from investment return in these figures). The 'do-nothing' arm freezes "
    "the earliest snapshot's quantities AND cash (no trades, no deposits), "
    "revaluing positions only at market prices implied by later snapshots; a "
    "symbol that fully exits the actual book has its price carried forward "
    "flat from its last-seen week (no independent price series is queried "
    "here). History is bounded to the most recent "
    f"{HISTORY_MAX_WEEKS} snapshot weeks (~2 years); older weeks are excluded "
    "from both arms. `n_effective` — not the raw sub-period count — is the "
    "number actually paired with the sign-test p-floor below: a sub-period "
    "where both arms return the same figure is a tie, not evidence, and is "
    "excluded from it."
)


# ── pure math ────────────────────────────────────────────────────────────
def modified_dietz_return(
    begin_value: float, end_value: float, cash_flow: float = 0.0, weight: float = 0.5,
) -> float | None:
    """Modified Dietz single-period return: R = (EV - BV - CF) / (BV + w*CF).
    `weight` is the fraction of the period remaining after the cash flow
    lands (0.5 = mid-period, the standard default absent finer dated flows).
    `None` when the denominator is non-positive (a fully-drained book) —
    the caller must not silently coerce that to 0.0."""
    denom = begin_value + weight * cash_flow
    if denom <= 0:
        return None
    return (end_value - begin_value - cash_flow) / denom


def chain_link(returns: list[float]) -> float | None:
    """Geometric TWR across sub-period returns; `None` for an empty list —
    there is nothing to chain-link, not a 0% return."""
    if not returns:
        return None
    total = 1.0
    for r in returns:
        total *= 1.0 + r
    return total - 1.0


def sign_test_two_sided_p_floor(n: int) -> float | None:
    """The smallest two-sided p-value a sign test over `n` paired
    observations could ever report (all `n` signs agreeing): 2 * (1/2)^n =
    2^(1-n), capped at 1.0. `None` below `n=1` — no comparison exists yet."""
    if n < 1:
        return None
    return min(1.0, 2.0 ** (1 - n))


# ── snapshot-history assembly ──────────────────────────────────────────
def _group_by_week(history: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """`history` is a flat list of snapshot-row dicts (one per symbol per
    week, as persisted). Groups into one entry per `report_week`: account
    figures (nav/cash, identical across every symbol row of that week since
    they're written from the same `Holdings` snapshot) plus a
    `{symbol: {qty, value_eur}}` map."""
    weeks: dict[str, dict[str, Any]] = {}
    for row in history:
        wk = row["report_week"]
        entry = weeks.setdefault(wk, {"nav": float(row["nav"]), "cash": float(row["cash"]), "positions": {}})
        entry["positions"][row["symbol"]] = {"qty": float(row["qty"]), "value_eur": float(row["value_eur"])}
    return weeks


def compute_scorecard(history: list[dict[str, Any]]) -> dict[str, Any]:
    """`history`: every `portfolio_snapshots` row for report weeks up to and
    including the report being assembled (already capped to
    `HISTORY_MAX_WEEKS` by the caller), ordered ascending by `report_week`.
    Depth = distinct weeks present; a return needs two anchor points, so
    depth < 2 is an explicit "insufficient" state (depth 0 an explicit empty
    state) rather than a zeroed one.

    `n_effective` is the count of sub-periods where the two arms' returns
    actually DIFFER (not merely the raw `depth_weeks - 1` sub-period count —
    see CRITICAL 1 in the module docstring); `p_floor_two_sided` pairs with
    `n_effective`, not with `depth_weeks`. `arms_coincide` is exactly
    `n_effective == 0` (see CRITICAL 2) — a cash deposit/withdrawal week
    diverges the arms even when quantities never changed.
    """
    weeks = _group_by_week(history)
    week_labels = sorted(weeks)
    depth_weeks = len(week_labels)

    if depth_weeks == 0:
        return {
            "convention": CONVENTION, "empty": True, "depth_weeks": 0,
            "n_effective": 0, "p_floor_two_sided": None,
            "actual_twr": None, "do_nothing_twr": None, "arms_coincide": None,
            "note": "no portfolio_snapshots history yet for this account",
        }

    if depth_weeks == 1:
        return {
            "convention": CONVENTION, "empty": False, "depth_weeks": 1,
            "n_effective": 0, "p_floor_two_sided": None,
            "actual_twr": None, "do_nothing_twr": None, "arms_coincide": None,
            "note": "insufficient history for a return calculation (need at least 2 snapshot weeks)",
        }

    week0 = weeks[week_labels[0]]
    qty0 = {sym: pos["qty"] for sym, pos in week0["positions"].items()}
    cash0 = week0["cash"]
    last_price = {sym: (pos["value_eur"] / pos["qty"]) for sym, pos in week0["positions"].items() if pos["qty"]}

    actual_returns: list[float] = []
    donothing_returns: list[float] = []
    n_differ = 0
    nav_actual_prev = week0["nav"]
    nav_donothing_prev = cash0 + sum(qty0[s] * last_price[s] for s in qty0 if s in last_price)

    for wk in week_labels[1:]:
        w = weeks[wk]
        for sym, pos in w["positions"].items():
            if pos["qty"]:
                last_price[sym] = pos["value_eur"] / pos["qty"]
        nav_actual = w["nav"]
        nav_donothing = cash0 + sum(qty0[s] * last_price[s] for s in qty0 if s in last_price)

        r_actual = modified_dietz_return(nav_actual_prev, nav_actual)
        r_donothing = modified_dietz_return(nav_donothing_prev, nav_donothing)
        if r_actual is not None:
            actual_returns.append(r_actual)
        if r_donothing is not None:
            donothing_returns.append(r_donothing)
        # A tie is not evidence — only a sub-period where BOTH arms produced
        # a comparable return AND that return actually differs counts.
        if r_actual is not None and r_donothing is not None and not math.isclose(
            r_actual, r_donothing, rel_tol=1e-9, abs_tol=1e-12,
        ):
            n_differ += 1

        nav_actual_prev = nav_actual
        nav_donothing_prev = nav_donothing

    arms_coincide = n_differ == 0
    note = None
    if arms_coincide:
        note = (
            "every comparable sub-period so far shows the same return in both arms "
            "(n_effective=0) — actual and do-nothing have not diverged yet; expected "
            "while the advisor's firing rate is near zero, not a statistical result"
        )

    return {
        "convention": CONVENTION, "empty": False, "depth_weeks": depth_weeks,
        "n_effective": n_differ,
        "p_floor_two_sided": sign_test_two_sided_p_floor(n_differ),
        "actual_twr": chain_link(actual_returns),
        "do_nothing_twr": chain_link(donothing_returns),
        "arms_coincide": arms_coincide,
        "note": note,
        "meta": {
            # Minor 8/9: the two counts below CAN differ
            # (a week with a fully-drained do-nothing basket, e.g., is a
            # denominator<=0 skip on that arm only, not the other) — printed
            # per-arm rather than folded into one misleading total.
            "n_actual": len(actual_returns), "n_donothing": len(donothing_returns),
            "base_composition_assumption": (
                "both arms assume the persisted `nav` already equals cash + held-"
                "position value for that week — `nav` is read as-is from "
                "portfolio_snapshots, never re-derived from cash+positions; a "
                "settlement-lag week where that equality doesn't hold would "
                "silently skew the comparison"
            ),
        },
    }
