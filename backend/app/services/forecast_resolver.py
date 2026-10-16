"""Forecast resolver — the ONLY caller of
`ForecastLedger.record_outcome`. Never writes `forecasts`/`outcomes`
directly; every write goes through the ledger so it stays the ONE writer
(see the ledger docstring). Runs every report-worker tick via `report_worker.
_maintenance_tick`, not just on report-pack weeks.

`resolve_due` grades every `pending` forecast whose `due_date` has arrived,
priced off F0 daily bars through `shadow.fetch_forward_return` (module-
level, Phase 18 F6 — the SAME entry/exit bar logic the shadow scoreboard
grades with, parameterised on `settings.forecast_resolver_stale_days`
instead of the scoreboard's fixed `5`, and — unlike the scoreboard's
legacy `entry_stale_days=None` — passing that same value as
`entry_stale_days` too: the resolver's entry bound is real, a ledger row must not silently mature off a stale entry bar).
`manual` forecasts are never auto-resolved — they wait for a human/
Phase-19 write.

── Cross-sectional book median (M7) ────────────────────────────────────────
An `excess_return_vs_book_median` forecast's metric is `forward_return -
book_median`, where `book_median` is the MEDIAN forward return, at this
forecast's own `horizon_days`, over EVERY symbol that carried a
`shadow_verdicts` row for the same tool in the same `report_week` — the
tool's FULL cross-section that week (every matured name, `hold`/null-
direction included, mirroring `shadow._score_tool`'s `full_cross_section`
rule: grading a relative pick against a median built only from the graded
picks themselves is circular). Symbols that price `pending`/`uncovered`
are excluded from the median, same as the scoreboard. Memoized per `(tool,
report_week, horizon_days)` within one `resolve_due` call — every forecast
sharing that key reuses one fetch pass instead of re-pricing the whole
cross-section per forecast.

An exactly-zero metric (the book-median name itself, or a flat absolute
return) is a PUSH: `hit=None`, `brier`/`log_score` NULL (the ledger's own
`record_outcome` already computes that from `hit is None`), forecast
`status="resolved"` (matured, just undecided) — never `abstained` (that is
reserved for `uncovered`, the pricing-failure case). Per-forecast exceptions
are caught and counted in `errors`; one bad forecast never sinks the batch.
"""
from __future__ import annotations

import statistics
from datetime import date, datetime
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import select

from app.config import Settings
from app.db import session_scope
from app.models import ShadowVerdict
from app.services.forecasts import ForecastLedger
from app.services.shadow import fetch_forward_return

RESOLVER_VERSION = "forecast-resolver-v1"


def _hit(metric_value: float, comparator: str, threshold: float) -> bool | None:
    """`None` (push) on an exactly-threshold metric — excluded from both
    hits and misses, same convention `shadow._score_tool` uses for a
    zero-metric observation. Otherwise the comparator applied literally;
    mirrors `_score_tool`'s `(direction > 0) == (metric > 0)` hit rule for
    both absolute and cross-sectional tools (see module docstring)."""
    if metric_value == threshold:
        return None
    return metric_value > threshold if comparator == ">" else metric_value < threshold


async def _book_median(
    redis: Redis, settings: Settings, tool: str, report_week: str, horizon_days: int, decision: date,
    memo: dict[tuple[str, str, int], tuple[float | None, int]],
) -> tuple[float | None, int]:
    """Median forward return over the tool's FULL `report_week` cross-
    section (every `shadow_verdicts` row for that `(tool, report_week)`,
    any direction), pricing failures (`pending`/`uncovered`) excluded.
    Memoized so N forecasts sharing a `(tool, report_week, horizon_days)`
    key price the cross-section once, not N times."""
    key = (tool, report_week, horizon_days)
    if key in memo:
        return memo[key]
    async with session_scope() as session:
        rows = (await session.execute(
            select(ShadowVerdict.symbol, ShadowVerdict.reading).where(
                ShadowVerdict.report_week == report_week, ShadowVerdict.tool == tool,
            )
        )).all()
    values: list[float] = []
    for symbol, reading in rows:
        source = (reading or {}).get("source") or settings.price_source_default
        status, d = await fetch_forward_return(
            redis, settings, symbol, source, decision, horizon_days,
            stale_days=settings.forecast_resolver_stale_days,
            entry_stale_days=settings.forecast_resolver_stale_days,
        )
        if status == "matured" and d is not None:
            values.append(d["value"])
    result = (statistics.median(values) if values else None, len(values))
    memo[key] = result
    return result


async def resolve_due(
    redis: Redis, settings: Settings, ledger: ForecastLedger, *, now: datetime,
) -> dict[str, Any]:
    """Grades up to `settings.forecast_resolver_batch_size` `pending`
    forecasts due on/before `now.date()`. `manual` forecasts (never
    auto-resolved) and a `resolution_source` that isn't `daily_bars:...`
    count as `skipped`. Returns `{"matured", "uncovered", "pushes",
    "skipped", "errors"}` — `matured` counts decided (non-push) matured
    outcomes; `pushes` counts exactly-zero-metric matured outcomes
    separately (both flip `forecasts.status` to `resolved`)."""
    counts: dict[str, Any] = {"matured": 0, "uncovered": 0, "pushes": 0, "skipped": 0, "errors": []}
    forecasts = await ledger.pending(due_before=now.date(), limit=settings.forecast_resolver_batch_size)
    book_median_memo: dict[tuple[str, str, int], tuple[float | None, int]] = {}

    for f in forecasts:
        try:
            if not (f.resolution_source or "").startswith("daily_bars:"):
                counts["skipped"] += 1  # manual forecasts are never auto-resolved
                continue
            source = f.resolution_source.split(":", 1)[1]
            symbol = (f.resolution_rule or {}).get("symbol") or f.subject
            status, d = await fetch_forward_return(
                redis, settings, symbol, source, f.decision_date, f.horizon_days,
                stale_days=settings.forecast_resolver_stale_days,
                entry_stale_days=settings.forecast_resolver_stale_days,
            )
            if status == "pending":
                counts["skipped"] += 1  # due_date reached but not actually priceable yet
                continue
            if status == "uncovered":
                await ledger.record_outcome(
                    f.id, resolution_status="uncovered",
                    realized={"reason": "no daily bar within stale_days of decision_date/due_date"},
                    hit=None, resolver_version=RESOLVER_VERSION,
                )
                counts["uncovered"] += 1
                continue

            realized: dict[str, Any] = {**d, "book_median": None, "cross_section_n": None}
            metric_value = d["value"]
            if f.target["metric"] == "excess_return_vs_book_median":
                tool = f.issued_by.split(":", 1)[1] if f.issued_by.startswith("shadow:") else f.issued_by
                book_median, cross_section_n = await _book_median(
                    redis, settings, tool, f.report_week, f.horizon_days, f.decision_date, book_median_memo,
                )
                if book_median is None:
                    # No priced cross-section this week to grade a relative
                    # call against — a pricing-coverage failure, not a
                    # directional one.
                    await ledger.record_outcome(
                        f.id, resolution_status="uncovered",
                        realized={"reason": "no priced cross-section to compute a book median"},
                        hit=None, resolver_version=RESOLVER_VERSION,
                    )
                    counts["uncovered"] += 1
                    continue
                metric_value = d["value"] - book_median
                realized["book_median"] = book_median
                realized["cross_section_n"] = cross_section_n

            hit = _hit(metric_value, f.target["comparator"], f.target.get("threshold", 0.0))
            await ledger.record_outcome(
                f.id, resolution_status="matured", realized=realized, hit=hit,
                resolver_version=RESOLVER_VERSION,
            )
            counts["pushes" if hit is None else "matured"] += 1
        except Exception as exc:  # noqa: BLE001 — one bad forecast never sinks the batch
            counts["errors"].append({"forecast_id": f.id, "reason": str(exc)})

    return counts
