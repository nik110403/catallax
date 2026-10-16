"""Recommendation lifecycle transitions.

Generalises the append-only accept/override transition `routers/reports.py`
 already used into one service both the router and the report
worker's maintenance tick call. Same append-only contract throughout:
the service NEVER UPDATEs a `recommendations` row — every status change
INSERTs a new row whose `prior_id` points at the row it supersedes, and the
prior row (including its `note` — the rule's firing arithmetic) is carried
forward unchanged, with the human's reason (or the service's own note, for
`expired`/`superseded`) landing separately under
`input_snapshot["human_note"]`.

Status vocabulary: `evaluated -> proposed -> {accepted | modified |
overridden | expired | superseded} -> executed`. `overridden` is ALSO the
persisted form of a human reject: the frozen
`advisor_report._load_overrides` -> `advisor._cooldown_check` cooldown loop
keys off that exact status string, so `reject_via` (the router's `/reject`)
reuses it (`input_snapshot["human_action"] = "reject"`) instead of a
distinct `rejected` status, which would silently disable cooldown. A real
`rejected` status waits for the Phase-19 unfreeze of that loop. `expired`
and `superseded` are written only by `expire_and_supersede` below (the
report worker's periodic tick) — no human-facing endpoint reaches them
directly.
"""
from __future__ import annotations

from datetime import date as date_type
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.config import Settings
from app.db import session_scope
from app.errors import DemoModeReadOnly, InvalidRequest, NotFound
from app.models import Recommendation

# The only legal edges — everything else raises InvalidRequest. `proposed ->
# expired`/`proposed -> superseded` are reachable only via
# `expire_and_supersede` (no router endpoint requests them directly).
TRANSITIONS: dict[str, frozenset[str]] = {
    "proposed": frozenset({"accepted", "modified", "overridden", "expired", "superseded"}),
    "accepted": frozenset({"executed"}),
    "modified": frozenset({"executed"}),
}
_MODIFY_EXECUTE_FIELDS = ("executed_at", "fill_price", "quantity", "venue")


def _scope_key(rule_id: str, input_snapshot: dict[str, Any]) -> str:
    """Same scope shape `advisor_report._load_overrides` uses (read, not
    imported — that function is frozen): `"rule_id:SYMBOL"` for rule 1's
    per-position rows, bare `rule_id` for rule 2/3's single row."""
    symbol = input_snapshot.get("symbol") if rule_id == "weight_band_trim" else ""
    return f"{rule_id}:{symbol}" if symbol else rule_id


def _child_row(prior: Recommendation, *, status: str, input_snapshot: dict[str, Any], sizing: float | None) -> Recommendation:
    return Recommendation(
        rule_id=prior.rule_id, as_of=prior.as_of, report_week=prior.report_week,
        action=prior.action, sizing=sizing, input_snapshot=input_snapshot,
        regime_component_set_id=prior.regime_component_set_id, status=status,
        tax_cost_arithmetic=prior.tax_cost_arithmetic, note=prior.note, prior_id=prior.id,
    )


async def transition(
    recommendation_id: int, new_status: str, *, note: str,
    extra_snapshot: dict[str, Any] | None, settings: Settings,
) -> dict[str, Recommendation]:
    """The generalised `reports._transition`: FOR UPDATE on the prior row,
    an append-only insert, `prior.note` carried forward, the human's reason
    under `input_snapshot["human_note"]`. `modified` additionally requires
    `extra_snapshot["sizing_override"]` (the new row's `sizing`; the
    original is preserved under `input_snapshot["sizing_proposed"]`).
    `executed` requires the four execution fields in `extra_snapshot`,
    stored verbatim under `input_snapshot["execution"]`. Returns the ORM
    rows (not dicts) — callers apply their own serialisation (`_rec_dict`)."""
    if settings.demo_mode:
        raise DemoModeReadOnly()
    async with session_scope() as db:
        # FOR UPDATE: without it two concurrent transitions on the same row
        # could both read the pre-transition status and each insert a row
        # pointing at the same prior_id, forking the chain.
        prior = (await db.execute(
            select(Recommendation).where(Recommendation.id == recommendation_id).with_for_update()
        )).scalar_one_or_none()
        if prior is None:
            raise NotFound(f"recommendation {recommendation_id} not found")
        # Append-only means `prior.status` is frozen at insert time and never
        # reflects a later transition — the only way to tell prior is still
        # the current leaf of its chain is to check whether a child already
        # points `prior_id` at it. Still inside the FOR UPDATE transaction,
        # so this closes the same fork this row's own lock is meant to close.
        existing_child_id = (await db.execute(
            select(Recommendation.id).where(Recommendation.prior_id == prior.id).limit(1)
        )).scalar_one_or_none()
        if existing_child_id is not None:
            raise InvalidRequest(
                f"recommendation {prior.id} is not a leaf — already superseded by "
                f"{existing_child_id}; act on the current leaf"
            )
        legal = TRANSITIONS.get(prior.status, frozenset())
        if new_status not in legal:
            raise InvalidRequest(
                f"recommendation {recommendation_id} is '{prior.status}' — cannot transition to "
                f"'{new_status}' (legal targets: {sorted(legal) or 'none'})"
            )
        new_input_snapshot: dict[str, Any] = {**prior.input_snapshot, "human_note": note}
        sizing = prior.sizing
        if extra_snapshot and "human_action" in extra_snapshot:
            new_input_snapshot["human_action"] = extra_snapshot["human_action"]
        if new_status == "modified":
            if not extra_snapshot or "sizing_override" not in extra_snapshot:
                raise InvalidRequest("'modified' transition requires 'sizing_override'")
            new_input_snapshot["sizing_proposed"] = prior.sizing
            sizing = float(extra_snapshot["sizing_override"])
        elif new_status == "executed":
            if not extra_snapshot or not all(k in extra_snapshot for k in _MODIFY_EXECUTE_FIELDS):
                raise InvalidRequest(f"'executed' transition requires {list(_MODIFY_EXECUTE_FIELDS)}")
            new_input_snapshot["execution"] = {k: extra_snapshot[k] for k in _MODIFY_EXECUTE_FIELDS}
        new_row = _child_row(prior, status=new_status, input_snapshot=new_input_snapshot, sizing=sizing)
        db.add(new_row)
        await db.commit()
        await db.refresh(new_row)
        await db.refresh(prior)  # proves append-only: the prior row's own status is untouched
    return {"new": new_row, "prior": prior}


async def _lock_leaf(db: Any, leaf_id: int) -> Recommendation | None:
    """Re-select+lock a leaf right before writing its child. The initial
    leaf scan (below) reads without a lock, so between that scan and this
    insert a concurrent `transition()` call (which does take its own row
    lock) may have already moved the row off `proposed` — re-checking here
    under `FOR UPDATE` closes that race instead of forking the chain.
    Returns `None` (skip this leaf) if it's gone or no longer `proposed`.
    Also skips it if a child already points `prior_id` at it: append-only
    rows never flip their own `status` column, so a concurrent `transition()`
    insert leaves this row's `status` reading `proposed` forever — the
    status re-check above can't see that fork, only the child-existence
    probe below can (same fix as `transition()`'s own leaf guard)."""
    row = (await db.execute(
        select(Recommendation).where(Recommendation.id == leaf_id).with_for_update()
    )).scalar_one_or_none()
    if row is None or row.status != "proposed":
        return None
    existing_child_id = (await db.execute(
        select(Recommendation.id).where(Recommendation.prior_id == row.id).limit(1)
    )).scalar_one_or_none()
    return None if existing_child_id is not None else row


async def expire_and_supersede(settings: Settings, *, now: datetime) -> dict[str, int]:
    """Called every report-worker tick, after the demo/enabled guard.
    For every `proposed` **leaf** row (not itself some other row's
    `prior_id` — i.e. still the current state of its chain) older than
    `RECOMMENDATION_EXPIRY_WEEKS` (0 = fall back to
    `advisor_review_horizon_weeks`), inserts an `expired` row. Then, among
    the leaves that survive that pass, for every `(rule_id, symbol-or-"")`
    scope with >= 2 proposed leaves, all but the newest (by `created_at`)
    get a `superseded` row. Idempotent: once written, an expired/superseded
    row's own `prior_id` makes the original leaf a non-leaf, so a rerun
    finds nothing left to act on. Each leaf is re-selected with
    `FOR UPDATE` (`_lock_leaf`) immediately before its child is inserted,
    and skipped if a concurrent transition already moved it off `proposed`
    — see `_lock_leaf`."""
    if settings.demo_mode:
        raise DemoModeReadOnly()
    expiry_weeks = settings.recommendation_expiry_weeks or settings.advisor_review_horizon_weeks
    expired = 0
    superseded = 0
    async with session_scope() as db:
        # fix 3 (was: full-table scan). Bounded to two index-backed reads
        # instead: `status == "proposed"` (`ix_recommendations_status`) for
        # the leaf candidates — append-only means a leaf's own status column
        # is frozen at `proposed` forever, so this is exactly the same
        # candidate set the old full scan filtered down to — then
        # `prior_id IN (candidate ids)` (`ix_recommendations_prior_id`) to
        # resolve which candidates are still leaves (no other row, of any
        # status, points prior_id at them), never reading the whole table.
        candidates = (await db.execute(
            select(Recommendation).where(Recommendation.status == "proposed")
        )).scalars().all()
        candidate_ids = [r.id for r in candidates]
        prior_ids: set[int] = set()
        if candidate_ids:
            prior_ids = set((await db.execute(
                select(Recommendation.prior_id).where(Recommendation.prior_id.in_(candidate_ids))
            )).scalars().all())
        leaves = [r for r in candidates if r.id not in prior_ids]

        still_active: list[Recommendation] = []
        for r in leaves:
            cutoff: date_type = r.as_of + timedelta(weeks=expiry_weeks)
            if cutoff < now.date():
                locked = await _lock_leaf(db, r.id)
                if locked is None:
                    continue  # already transitioned concurrently — nothing to expire
                note = f"expired by lifecycle service after {expiry_weeks} weeks"
                db.add(_child_row(
                    locked, status="expired", sizing=locked.sizing,
                    input_snapshot={**locked.input_snapshot, "human_note": note},
                ))
                expired += 1
            else:
                still_active.append(r)

        by_scope: dict[str, list[Recommendation]] = {}
        for r in still_active:
            by_scope.setdefault(_scope_key(r.rule_id, r.input_snapshot), []).append(r)
        for group in by_scope.values():
            if len(group) < 2:
                continue
            group.sort(key=lambda r: r.created_at)
            newest = group[-1]
            for r in group[:-1]:
                locked = await _lock_leaf(db, r.id)
                if locked is None:
                    continue  # already transitioned concurrently — nothing to supersede
                note = f"superseded by a newer {r.rule_id} recommendation (row {newest.id})"
                db.add(_child_row(
                    locked, status="superseded", sizing=locked.sizing,
                    input_snapshot={**locked.input_snapshot, "human_note": note},
                ))
                superseded += 1

        await db.commit()
    return {"expired": expired, "superseded": superseded}
