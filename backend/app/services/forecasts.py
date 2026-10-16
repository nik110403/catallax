"""Forecast ledger — the ONLY writer of `forecasts` and the
ONLY writer of `outcomes`. The resolver calls into `record_outcome`; it
never writes `outcomes` itself. `forecasts` rows are append-only and
immutable once issued — the single exception is `record_outcome` flipping
`status` (`pending` -> `resolved` | `abstained`) in the same transaction as
the `outcomes` insert; no other method, and nothing else on the row, is
ever updated.

Pure helpers (`laplace_probability`, `brier`, `log_score`,
`build_shadow_forecasts`) do no I/O and are unit-tested directly; async
methods below them are the only DB access.

── Issued probability of a machine forecast (m11) ─────────────────────────
A `shadow:<tool>` forecast's `probability` is the Laplace rule-of-succession
estimate (`laplace_probability`) over that issuer's EFFECTIVE graded record
at issuance, frozen onto the row forever after. That record is, in
principle, this ledger's own accumulating history (`stats_by_issuer` —
`outcomes` joined to `forecasts`, same `issued_by`+`horizon_days`) PLUS a
one-time SEED read from the legacy shadow scoreboard's `_score_tool` output
for that tool at the CURRENT `report_week` (`shadow.ShadowEngine.
scoreboard()["tools"][tool]`) — since that scoreboard already recomputes
the full graded `shadow_verdicts` history on every call, the "seed" simply
gives every forecast issuance a running-start prior instead of beginning
blind. `build_shadow_forecasts` below is the pure, no-I/O helper and so
only has access to the SEED half of that record (passed in as
`scoreboard_stats`); it takes an optional `settings` (data, not I/O) for
the two config knobs it needs. The ledger's OWN half
(`stats_by_issuer`) grows only as forecasts resolve going forward — at
Phase-18 genesis there is no `outcomes` history yet, so the seed alone
drives every first-ever issuance; a future caller wiring both halves
together would add the ledger's own hits/n on top of the seed before
computing probability. Below `forecast_probability_min_n` **combined**
observations, `evidence_refs` also carries a `prior_note` flagging the
number as a Laplace prior, not a track record (the shadow `how_to_read` says the
same). Ledger observations from a CROSS-SECTIONAL issuer (`factor`,
`signals`) count ½ each toward `n` — `_score_tool` already halves its own
`effective_n` for the same reason (B4: a tercile week pairs one add_watch
against one trim_watch from the SAME ranking, so the raw count
double-counts one independent comparison).

Advisor-issued forecasts are OUT this phase — risk
actions are not directional views, and `dca_modulation` has no symbol; the
`advisor:<rule_id>` issuer is reserved for Phase 19 (projection-resolved
decision-outcome claims, `invalidation.still_valid`).
"""
from __future__ import annotations

import math
from datetime import date, datetime, timedelta, timezone
from typing import Any

from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert

from app.config import Settings
from app.db import session_scope
from app.errors import InvalidRequest, NotFound
from app.models import Forecast, Outcome

_SHADOW_HORIZON_DAYS = 28  # fixed for all shadow-issued forecasts
# on `shadow_verdicts.horizon_days` too — see the ORM docstring).
_SHADOW_ISSUER_VERSION = "shadow-verdict-engine-v1"
_CROSS_SECTIONAL_TOOLS = ("factor", "signals")  # mirrors shadow.py's own list
_LOG_SCORE_CLAMP = (0.01, 0.99)


# ── pure helpers (no I/O) ───────────────────────────────────────────────
def laplace_probability(hits: int, n: int) -> float:
    """Rule of succession: `(hits + 1) / (n + 2)`. `(0, 0) == 0.5`;
    `(3, 4) == 2/3` (NOT 0.8 — a table entry that reads `(3,4) = 0.8` is
    simply wrong, B1); `(7, 8) == 0.8`."""
    return (hits + 1) / (n + 2)


def brier(p: float, hit: bool) -> float:
    """`(p - hit)^2`, `hit` coerced to 0.0/1.0."""
    return (p - float(hit)) ** 2


def log_score(p: float, hit: bool) -> float:
    """`ln(p)` if `hit` else `ln(1 - p)`; `p` clamped to `[0.01, 0.99]`
    first — an unclamped p at the boundary (0 or 1) would make a single
    miss/hit an unbounded (infinite) score."""
    clamped = min(max(p, _LOG_SCORE_CLAMP[0]), _LOG_SCORE_CLAMP[1])
    return math.log(clamped) if hit else math.log(1.0 - clamped)


def _seed_from_scoreboard(tool_stats: dict[str, Any] | None) -> tuple[int, int]:
    """(hits, n) seed from one tool's `_score_tool`-shaped stats
    (`shadow.ShadowEngine.scoreboard()["tools"][tool]`). `n` is
    `effective_n` when present (cross-sectional tools are already halved
    there, B4) else `n_graded - pushes`; `hits = round(hit_rate * n)`,
    consistent with `hit_rate == hits / n`. A never-graded/missing tool
    (no `hit_rate` yet — e.g. a brand-new issuer, or `forensic`, which
    never enters the graded denominator at all) seeds `(0, 0)`."""
    if not tool_stats:
        return 0, 0
    hit_rate = tool_stats.get("hit_rate")
    if hit_rate is None:
        return 0, 0
    n_graded = tool_stats.get("n_graded") or 0
    pushes = tool_stats.get("pushes") or 0
    effective_n = tool_stats.get("effective_n")
    n = effective_n if effective_n is not None else (n_graded - pushes)
    if not n or n <= 0:
        return 0, 0
    return round(hit_rate * n), n


def _week_start(report_week: str) -> date:
    """Monday of ISO week `"YYYY-Www"` (copied 3-line helper from
    `shadow._week_start` — deliberately duplicated, not imported, so this
    module has no import edge onto the shadow engine)."""
    year_s, week_s = report_week.split("-W")
    return date.fromisocalendar(int(year_s), int(week_s), 1)


def _statement(symbol: str, metric: str, comparator: str, horizon_days: int) -> str:
    """One sentence a stranger can judge true/false unaided. For the `>`
    comparator only, ends with the M10 suffix — the exactly-zero case is
    excluded from both hits and misses (a push), not silently a "yes"."""
    label = "excess return vs the book median" if metric == "excess_return_vs_book_median" else "forward return"
    word = "greater than" if comparator == ">" else "less than"
    text = f"{symbol} {label} over the next {horizon_days} days is {word} 0"
    if comparator == ">":
        text += " (exactly zero = no decision)"
    return text + "."


def build_shadow_forecasts(
    report_week: str,
    verdict_rows: list[dict[str, Any]],
    open_pending: set[tuple[str, str, int]],
    scoreboard_stats: dict[str, dict[str, Any]],
    *,
    first_seen_at: datetime,
    settings: Settings,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """One binary forecast per GRADEABLE verdict (`direction` not NULL —
    `forensic` and `hold` never issue, mirroring the scoreboard's own
    exclusions) that is not already covered: skipped
    ONLY when `(f"shadow:{tool}", symbol, direction)` — SAME direction — is
    in `open_pending`; a pending forecast at the same horizon already
    stands for that exact call, giving rolling, non-overlapping issuance
    windows. A FLIPPED verdict (a pending forecast on the book for the
    opposite direction) issues immediately — the old pending row still
    resolves on its own terms, it just no longer blocks a fresh, opposite
    call. `open_pending` therefore carries direction (was `(issued_by,
    symbol)`; `ForecastLedger.open_pending` was extended to match — see its
    docstring). NOTE: a flip lands on the SAME `decision_date`/`horizon_days`
    as the still-open opposite-direction row only if two flips happen
    within one calendar week, which cannot happen (one verdict per
    (report_week, symbol, tool) — `uq_forecasts_issuer_subject_date_horizon`
    can only collide across DIFFERENT weeks' decision_dates, which never
    match); documented rather than guarded, since it is unreachable via this
    caller. `verdict_rows` are the rows
    `ShadowEngine.compute_verdicts()` produced this week, each carrying an
    extra transient key `bar_ts` (the caller threads it in; never persisted to
    `shadow_verdicts.reading`) used as `published_at` (the timestamp of the
    bar the reading was read from, NOT the fetch time — M6).

    `settings` supplies `forecast_probability_min_n` and
    `price_source_default`; passing it in keeps this function pure (a
    `Settings` object is data, not I/O).

    Returns `(rows, skipped)` — `skipped == {"covered": k, "ungradeable":
    j}` — rather than folding the summary into the row list itself, so a
    caller never has to pick it back apart.

    NOTE for the caller (open question, documented rather than silently guessed):
    `evidence_refs`' `issuance_reason` is always `"new_call"` here — telling
    `"rolled"` (a continuation of an already-RESOLVED same-direction call)
    from a genuinely new one needs the ledger's resolved-forecast history,
    which this pure function does not have access to (only `open_pending`,
    i.e. still-open forecasts). The calling step, which does have DB access, is better
    placed to upgrade this tag before calling `issue()` if it wants the
    distinction to be load-bearing.
    """
    decision_date = _week_start(report_week)
    due_date = decision_date + timedelta(days=_SHADOW_HORIZON_DAYS)
    rows: list[dict[str, Any]] = []
    covered = ungradeable = 0
    for v in verdict_rows:
        direction = v.get("direction")
        if direction is None:
            ungradeable += 1
            continue
        tool, symbol = v["tool"], v["symbol"]
        issued_by = f"shadow:{tool}"
        if (issued_by, symbol, direction) in open_pending:
            covered += 1
            continue
        cross_sectional = tool in _CROSS_SECTIONAL_TOOLS
        metric = "excess_return_vs_book_median" if cross_sectional else "forward_return"
        comparator = ">" if direction > 0 else "<"
        reading = v.get("reading") or {}
        source = reading.get("source") or settings.price_source_default
        hits, n = _seed_from_scoreboard(scoreboard_stats.get(tool))
        probability = laplace_probability(hits, n)
        evidence_refs: list[dict[str, Any]] = [
            {"kind": "shadow_verdict", "id": f"{report_week}:{tool}:{symbol}"},
            {"kind": "report_pack", "week": report_week},
            {
                "kind": "prior_seed", "source": "shadow_scoreboard",
                "hits": hits, "n": n, "report_week": report_week,
            },
        ]
        if n < settings.forecast_probability_min_n:
            evidence_refs.append({
                "kind": "prior_note",
                "text": (
                    f"effective n={n} < {settings.forecast_probability_min_n}: "
                    "probability is a Laplace prior, not a track record"
                ),
            })
        evidence_refs.append({"kind": "issuance_reason", "value": "new_call"})
        rows.append({
            "issued_by": issued_by,
            "issuer_version": _SHADOW_ISSUER_VERSION,
            "subject_kind": "symbol",
            "subject": symbol,
            "statement": _statement(symbol, metric, comparator, _SHADOW_HORIZON_DAYS),
            "kind": "binary",
            "probability": probability,
            "direction": direction,
            "target": {"metric": metric, "comparator": comparator, "threshold": 0.0},
            "horizon_days": _SHADOW_HORIZON_DAYS,
            "decision_date": decision_date,
            "due_date": due_date,
            "report_week": report_week,
            "published_at": v["bar_ts"],
            "first_seen_at": first_seen_at,
            "resolution_source": f"daily_bars:{source}",
            "resolution_rule": {
                "symbol": symbol, "source": source,
                "entry": "first close on/after decision_date within stale_days",
                "exit": "first close on/after due_date within stale_days",
                "book_median": {"tool": tool, "report_week": report_week} if cross_sectional else None,
            },
            "evidence_refs": evidence_refs,
        })
    return rows, {"covered": covered, "ungradeable": ungradeable}


def _forecast_dict(row: Forecast) -> dict[str, Any]:
    return {
        "id": row.id, "issued_by": row.issued_by, "issuer_version": row.issuer_version,
        "subject_kind": row.subject_kind, "subject": row.subject, "statement": row.statement,
        "kind": row.kind, "probability": row.probability, "direction": row.direction,
        "target": row.target, "horizon_days": row.horizon_days,
        "decision_date": row.decision_date.isoformat(), "due_date": row.due_date.isoformat(),
        "report_week": row.report_week, "published_at": row.published_at.isoformat(),
        "first_seen_at": row.first_seen_at.isoformat(), "available_at": row.available_at.isoformat(),
        "resolution_source": row.resolution_source, "resolution_rule": row.resolution_rule,
        "evidence_refs": row.evidence_refs, "status": row.status,
    }


class ForecastLedger:
    """The only writer of `forecasts` and `outcomes`."""

    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings

    # ── reads used to build this week's issuance ────────────────────────
    async def open_pending(self, issued_by_prefix: str, horizon_days: int) -> set[tuple[str, str, int]]:
        """`(issued_by, subject, direction)` of every `pending` forecast at
        `horizon_days` whose `issued_by` starts with `issued_by_prefix`
        (e.g. `"shadow:"` covers every tool in one query). Direction is part
        of the key: a flipped verdict must issue a fresh
        forecast even while the opposite-direction one is still open — only
        a SAME-direction pending row means "already covered"."""
        async with session_scope() as session:
            rows = (await session.execute(
                select(Forecast.issued_by, Forecast.subject, Forecast.direction).where(
                    Forecast.status == "pending",
                    Forecast.horizon_days == horizon_days,
                    Forecast.issued_by.like(f"{issued_by_prefix}%"),
                )
            )).all()
        return {(r.issued_by, r.subject, r.direction) for r in rows}

    # ── writes ───────────────────────────────────────────────────────────
    async def issue(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        """`INSERT … ON CONFLICT DO NOTHING` on the unique issuance key;
        never UPDATEs, never raises out of the batch — a PIT-invariant or
        probability-range violation is validated BEFORE insert and reported
        in `errors` instead (M6: a silent CHECK failure inside the worker's
        try/except would otherwise vanish the whole tick's issuance)."""
        now = datetime.now(timezone.utc)
        valid_rows: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        for row in rows:
            row = dict(row)
            row.setdefault("available_at", now)
            row.setdefault("status", "pending")
            p = row.get("probability")
            reason = None
            floor = self._settings.forecast_ledger_start_date
            if p is not None and not (0.0 <= p <= 1.0):
                reason = f"probability {p!r} out of [0, 1]"
            elif row.get("decision_date") is not None and row["decision_date"] < floor:
                reason = f"decision_date {row['decision_date']!r} is before the ledger floor {floor!r}"
            elif not (row["published_at"] <= row["first_seen_at"] <= row["available_at"]):
                reason = (
                    f"PIT order violated: published_at={row['published_at']!r} "
                    f"first_seen_at={row['first_seen_at']!r} available_at={row['available_at']!r}"
                )
            if reason:
                errors.append({
                    "issued_by": row.get("issued_by"), "subject": row.get("subject"), "reason": reason,
                })
                continue
            valid_rows.append(row)

        issued = 0
        if valid_rows:
            async with session_scope() as session:
                stmt = (
                    insert(Forecast)
                    .values(valid_rows)
                    .on_conflict_do_nothing(constraint="uq_forecasts_issuer_subject_date_horizon")
                    .returning(Forecast.id)
                )
                result = await session.execute(stmt)
                issued = len(result.scalars().all())
                await session.commit()
        return {"issued": issued, "duplicates": len(valid_rows) - issued, "errors": errors}

    async def issue_human(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Minimal by design (F11 x m15): backend only, no UI form this
        phase. Same `issue()` path with `issued_by="human"`; the PIT triple
        collapses to a single `now()` (a human statement is published the
        moment it is typed) and `probability` is required — no seed, no
        `prior_note` (there is no track record to season it with)."""
        if payload.get("probability") is None:
            raise InvalidRequest("issue_human requires an explicit probability")
        now = datetime.now(timezone.utc)
        subject_kind = payload["subject_kind"]
        source = payload.get("source")
        resolution_source = f"daily_bars:{source}" if subject_kind == "symbol" and source else "manual"
        decision_date = payload["decision_date"]
        horizon_days = payload["horizon_days"]
        evidence_refs = list(payload.get("evidence_refs") or [])
        evidence_refs.append({"kind": "issuance_reason", "value": "human"})
        row = {
            "issued_by": "human", "issuer_version": "human",
            "subject_kind": subject_kind, "subject": payload["subject"],
            "statement": payload["statement"], "kind": payload.get("kind", "binary"),
            "probability": payload["probability"], "direction": payload.get("direction"),
            "target": payload["target"], "horizon_days": horizon_days,
            "decision_date": decision_date, "due_date": decision_date + timedelta(days=horizon_days),
            "report_week": payload.get("report_week"),
            "published_at": now, "first_seen_at": now, "available_at": now,
            "resolution_source": resolution_source, "resolution_rule": payload["resolution_rule"],
            "evidence_refs": evidence_refs,
        }
        result = await self.issue([row])
        if result["errors"]:
            raise InvalidRequest(f"human forecast rejected: {result['errors'][0]['reason']}")
        return result

    async def record_outcome(
        self,
        forecast_id: int,
        *,
        resolution_status: str,
        realized: dict[str, Any],
        hit: bool | None,
        resolver_version: str,
        manual_note: str | None = None,
    ) -> dict[str, Any]:
        """Inserts the ONE `outcomes` row for `forecast_id` and flips
        `forecasts.status` in the same transaction — the single column this
        ledger ever updates on an already-issued row; the row's content
        (statement, probability, target, …) is never touched. `SELECT …
        FOR UPDATE` on the forecast (mirrors `reports._transition`) so two
        concurrent resolutions of the same forecast can't both succeed;
        the second raises `InvalidRequest`."""
        if manual_note is not None:
            realized = {**realized, "note": manual_note}
        async with session_scope() as session:
            forecast = (await session.execute(
                select(Forecast).where(Forecast.id == forecast_id).with_for_update()
            )).scalar_one_or_none()
            if forecast is None:
                raise NotFound(f"forecast {forecast_id} not found")
            if forecast.status != "pending":
                raise InvalidRequest(
                    f"forecast {forecast_id} is already '{forecast.status}' — cannot resolve twice"
                )
            p = forecast.probability
            b = brier(p, hit) if (hit is not None and p is not None) else None
            ls = log_score(p, hit) if (hit is not None and p is not None) else None
            if resolution_status == "uncovered":
                direction_label = "n/a"
            elif hit is None:
                direction_label = "push"
            else:
                direction_label = "hit" if hit else "miss"
            outcome = Outcome(
                forecast_id=forecast_id, resolved_at=datetime.now(timezone.utc),
                resolution_status=resolution_status, realized=realized, hit=hit,
                brier=b, log_score=ls,
                attribution={"thesis": "n/a", "direction": direction_label, "sizing": "n/a"},
                resolver_version=resolver_version,
            )
            session.add(outcome)
            forecast.status = "abstained" if resolution_status == "uncovered" else "resolved"
            await session.commit()
            await session.refresh(outcome)
            await session.refresh(forecast)
        return {
            "outcome": {
                "id": outcome.id, "forecast_id": outcome.forecast_id,
                "resolution_status": outcome.resolution_status, "hit": outcome.hit,
                "brier": outcome.brier, "log_score": outcome.log_score,
            },
            "forecast_status": forecast.status,
        }

    async def get_by_natural_key(
        self, *, issued_by: str, subject: str, decision_date: date, horizon_days: int,
    ) -> dict[str, Any] | None:
        """Read-after-write for `POST /forecasts`: fetches the row just
        written by `issue_human`, keyed on the exact columns
        `uq_forecasts_issuer_subject_date_horizon` enforces uniqueness on —
        precise, and race-free against any other concurrent issuance (unlike
        `list(...).[0]`, which would only be right by `ORDER BY id DESC`
        coincidence)."""
        async with session_scope() as session:
            row = (await session.execute(
                select(Forecast).where(
                    Forecast.issued_by == issued_by, Forecast.subject == subject,
                    Forecast.decision_date == decision_date, Forecast.horizon_days == horizon_days,
                )
            )).scalar_one_or_none()
        return _forecast_dict(row) if row is not None else None

    # ── reads ────────────────────────────────────────────────────────────
    async def pending(self, due_before: date, limit: int) -> list[Forecast]:
        async with session_scope() as session:
            rows = (await session.execute(
                select(Forecast)
                .where(Forecast.status == "pending", Forecast.due_date <= due_before)
                .order_by(Forecast.due_date)
                .limit(limit)
            )).scalars().all()
        return list(rows)

    async def count_pending(self, due_before: date) -> int:
        """`SELECT count(*)` companion to `pending()`: a
        caller that only needs the total (e.g. the issuance chapter's
        `pending_total`) must never pull `limit=100_000` rows off the wire
        just to call `len()` on them."""
        async with session_scope() as session:
            result = await session.execute(
                select(func.count()).select_from(Forecast).where(
                    Forecast.status == "pending", Forecast.due_date <= due_before,
                )
            )
        return result.scalar_one()

    async def list(
        self,
        status: str | None = None,
        week: str | None = None,
        issued_by: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        async with session_scope() as session:
            stmt = select(Forecast).order_by(Forecast.id.desc()).limit(limit)
            if status is not None:
                stmt = stmt.where(Forecast.status == status)
            if week is not None:
                stmt = stmt.where(Forecast.report_week == week)
            if issued_by is not None:
                stmt = stmt.where(Forecast.issued_by == issued_by)
            rows = (await session.execute(stmt)).scalars().all()
        return [_forecast_dict(r) for r in rows]

    async def resolved_keys(self, issued_by: str, *, batch_size: int = 1000) -> set[tuple[str, Any]]:
        """Every `(subject, direction)` pair with a RESOLVED forecast for
        `issued_by` — loops an id-cursor over `batch_size`-row pages to
        exhaustion instead of taking a single capped `list(...)` call
       . `report_worker._mark_rolled_forecasts` used to call
        `list(status="resolved", issued_by=..., limit=1000)`: fine under
        1000 resolved rows per issuer, but past that the tail silently
        never rolls — a genuine continuation of a standing call reads as
        `"new_call"` forever because its resolved history fell outside the
        capped page. Each page is bounded by `ix_forecasts_issued_by_
        subject_status` (`issued_by`+`status`) plus the id cursor, so a
        large ledger costs O(pages) queries, never one unbounded scan."""
        keys: set[tuple[str, Any]] = set()
        async with session_scope() as session:
            last_id = 0
            while True:
                stmt = (
                    select(Forecast.id, Forecast.subject, Forecast.direction)
                    .where(
                        Forecast.status == "resolved", Forecast.issued_by == issued_by,
                        Forecast.id > last_id,
                    )
                    .order_by(Forecast.id)
                    .limit(batch_size)
                )
                page = (await session.execute(stmt)).all()
                if not page:
                    break
                for row_id, subject, direction in page:
                    keys.add((subject, direction))
                    last_id = row_id
                if len(page) < batch_size:
                    break
        return keys

    async def stats_by_issuer(self, horizon_days: int) -> dict[str, dict[str, Any]]:
        """`outcomes ⋈ forecasts` at `horizon_days`, grouped by `issued_by`
        — feeds `laplace_probability` for this ledger's OWN half of the
        record (see module docstring). `effective_n` applies the ½ weight
        for cross-sectional issuers (`shadow:factor`, `shadow:signals`)."""
        async with session_scope() as session:
            rows = (await session.execute(
                select(Forecast.issued_by, Outcome.hit, Outcome.resolution_status)
                .join(Outcome, Outcome.forecast_id == Forecast.id)
                .where(Forecast.horizon_days == horizon_days)
            )).all()
        out: dict[str, dict[str, Any]] = {}
        for issued_by, hit, resolution_status in rows:
            s = out.setdefault(
                issued_by, {"hits": 0, "n": 0, "effective_n": 0.0, "pushes": 0, "abstained": 0}
            )
            tool = issued_by.split(":", 1)[1] if issued_by.startswith("shadow:") else None
            weight = 0.5 if tool in _CROSS_SECTIONAL_TOOLS else 1.0
            if resolution_status == "uncovered":
                s["abstained"] += 1
                continue
            if hit is None:
                s["pushes"] += 1
                continue
            s["n"] += 1
            s["effective_n"] += weight
            if hit:
                s["hits"] += 1
        return out
