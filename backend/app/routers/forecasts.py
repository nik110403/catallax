"""Forecast ledger read/write routes. All under
/api/v1/forecasts. `services/forecasts.ForecastLedger` is the only
writer of `forecasts`/`outcomes`; this router shapes the HTTP surface over
it — body validation, the `{"forecasts": [...], "count": n}` envelope,
and the one direct DB read (`GET /{id}`, joined with its `outcome`) the
ledger itself doesn't expose a method for (it has no by-id getter — `list`/
`pending` are its only bulk reads).

Demo mode (binding for every new GET here): every GET returns a
fixed, deterministic in-memory sample — never a DB session, never a
`RiskEngine`/`ForecastLedger` construction; every write (`POST`) raises
`DemoModeReadOnly` before touching anything.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, Field
from redis.asyncio import Redis
from sqlalchemy import select

from app.config import Settings, get_settings
from app.db import session_scope
from app.errors import DemoModeReadOnly, InvalidRequest, NotFound
from app.models import Forecast, Outcome
from app.redis import get_redis
from app.services.forecasts import ForecastLedger

router = APIRouter(prefix="/forecasts", tags=["forecasts"])

_DEFAULT_LIST_LIMIT = 200
_MAX_LIST_LIMIT = 1000


# ── demo fixtures — fixed, deterministic, never touches the DB ────
def _demo_forecast(i: int, *, status: str = "pending", hit: bool | None = None) -> dict[str, Any]:
    published = datetime(2026, 9, 1, 12, 0, tzinfo=timezone.utc)
    row = {
        "id": i, "issued_by": "shadow:technicals" if i % 2 else "human",
        "issuer_version": "demo-fixture-v1", "subject_kind": "symbol", "subject": f"DEMO{i}",
        "statement": f"DEMO{i} forward return over the next 28 days is greater than 0 (exactly zero = no decision).",
        "kind": "binary", "probability": 0.6, "direction": 1,
        "target": {"metric": "forward_return", "comparator": ">", "threshold": 0.0},
        "horizon_days": 28, "decision_date": "2026-09-01", "due_date": "2026-09-29",
        "report_week": "2026-W36", "published_at": published.isoformat(),
        "first_seen_at": published.isoformat(), "available_at": published.isoformat(),
        "resolution_source": "daily_bars:yfinance",
        "resolution_rule": {"symbol": f"DEMO{i}", "source": "yfinance"},
        "evidence_refs": [{"kind": "issuance_reason", "value": "new_call"}],
        "status": status,
    }
    if status in ("resolved", "abstained"):
        row["outcome"] = {
            "id": i, "forecast_id": i, "resolution_status": "matured" if hit is not None else "uncovered",
            "hit": hit, "brier": round((0.6 - float(hit)) ** 2, 4) if hit is not None else None,
            "log_score": None,
        }
    return row


def _demo_forecasts_sample() -> list[dict[str, Any]]:
    return [
        _demo_forecast(1), _demo_forecast(2), _demo_forecast(3),
        _demo_forecast(4, status="resolved", hit=True),
        _demo_forecast(5, status="resolved", hit=False),
        _demo_forecast(6, status="abstained", hit=None),
    ]


class ForecastIssueRequest(BaseModel):
    """Human issuance (F11 x m15) — minimal by design, backend only, no UI
    form this phase. `subject` doubles as the resolvable symbol when
    `subject_kind == "symbol"`; `source` opts that symbol into `daily_bars:`
    resolution (the resolver picks it up like any other pending forecast)
    — omit it for a `manual` (macro/portfolio) claim, which is the ONLY kind
    `/resolve` below can close out by hand."""

    statement: str = Field(min_length=1, max_length=500)
    probability: float = Field(ge=0.0, le=1.0)
    horizon_days: int = Field(ge=1, le=730)
    subject_kind: str = Field(min_length=1, max_length=16)
    subject: str = Field(min_length=1, max_length=64)
    source: str | None = None
    decision_date: date | None = None
    report_week: str | None = None
    direction: int | None = Field(
        default=None,
        description=(
            "Required (+1 or -1) when subject_kind=symbol and source are both set — the "
            "resolver needs a direction to build a resolvable forward_return target."
        ),
    )


class ForecastResolveRequest(BaseModel):
    hit: bool
    note: str | None = Field(default=None, max_length=2000)


def _outcome_dict(row: Outcome | None) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        "id": row.id, "forecast_id": row.forecast_id, "resolution_status": row.resolution_status,
        "hit": row.hit, "brier": row.brier, "log_score": row.log_score,
    }


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


@router.get("")
async def list_forecasts(
    status: str | None = "pending", week: str | None = Query(None, alias="week"),
    issued_by: str | None = None,
    limit: int = Query(_DEFAULT_LIST_LIMIT, ge=1, le=_MAX_LIST_LIMIT),
    settings: Settings = Depends(get_settings), redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    if settings.demo_mode:
        rows = _demo_forecasts_sample()
        if status is not None:
            rows = [r for r in rows if r["status"] == status]
        return {"forecasts": rows, "count": len(rows)}
    rows = await ForecastLedger(redis, settings).list(status=status, week=week, issued_by=issued_by, limit=limit)
    return {"forecasts": rows, "count": len(rows)}


@router.get("/{forecast_id}")
async def get_forecast(
    forecast_id: int = Path(..., ge=1), settings: Settings = Depends(get_settings),
) -> dict[str, Any]:
    if settings.demo_mode:
        for row in _demo_forecasts_sample():
            if row["id"] == forecast_id:
                row = dict(row)
                row.setdefault("outcome", None)
                return row
        raise NotFound(f"demo forecast {forecast_id} not found")
    async with session_scope() as db:
        forecast = (await db.execute(select(Forecast).where(Forecast.id == forecast_id))).scalar_one_or_none()
        if forecast is None:
            raise NotFound(f"forecast {forecast_id} not found")
        outcome = (await db.execute(select(Outcome).where(Outcome.forecast_id == forecast_id))).scalar_one_or_none()
    body = _forecast_dict(forecast)
    body["outcome"] = _outcome_dict(outcome)
    return body


@router.post("")
async def issue_forecast(
    body: ForecastIssueRequest, settings: Settings = Depends(get_settings), redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    if settings.demo_mode:
        raise DemoModeReadOnly()
    decision_date = body.decision_date or datetime.now(timezone.utc).date()
    sourced = body.subject_kind == "symbol" and body.source
    if sourced:
        # F2: a sourced (subject_kind=symbol + source) forecast is picked up
        # by the daily_bars resolver like any shadow-issued one — it MUST
        # carry a full resolvable target (metric/comparator/threshold), never the
        # manual-shaped placeholder (that left `forecast_resolver.resolve_due`
        # raising KeyError('metric') forever). Direction comes straight from
        # the request; without one there's no resolvable target to build.
        if body.direction not in (1, -1):
            raise InvalidRequest(
                "subject_kind=symbol with source requires direction=1 or direction=-1 "
                "to build a resolvable forward_return target"
            )
        target: dict[str, Any] = {
            "metric": "forward_return", "comparator": ">" if body.direction > 0 else "<", "threshold": 0.0,
        }
        resolution_rule: dict[str, Any] = {
            "symbol": body.subject, "source": body.source,
            "entry": "first close on/after decision_date within stale_days",
            "exit": "first close on/after due_date within stale_days", "book_median": None,
        }
    else:
        target = {"kind": "manual", "statement": body.statement}
        resolution_rule = {"manual": True}
    payload = {
        "subject_kind": body.subject_kind, "subject": body.subject, "statement": body.statement,
        "probability": body.probability, "direction": body.direction if sourced else None,
        "target": target,
        "horizon_days": body.horizon_days, "decision_date": decision_date,
        "report_week": body.report_week, "source": body.source, "resolution_rule": resolution_rule,
    }
    ledger = ForecastLedger(redis, settings)
    result = await ledger.issue_human(payload)
    forecast = None
    if result["issued"] == 1:
        forecast = await ledger.get_by_natural_key(
            issued_by="human", subject=body.subject, decision_date=decision_date, horizon_days=body.horizon_days,
        )
    return {**result, "forecast": forecast}


@router.post("/{forecast_id}/resolve")
async def resolve_forecast(
    body: ForecastResolveRequest, forecast_id: int = Path(..., ge=1),
    settings: Settings = Depends(get_settings), redis: Redis = Depends(get_redis),
) -> dict[str, Any]:
    if settings.demo_mode:
        raise DemoModeReadOnly()
    async with session_scope() as db:
        forecast = (await db.execute(select(Forecast).where(Forecast.id == forecast_id))).scalar_one_or_none()
    if forecast is None:
        raise NotFound(f"forecast {forecast_id} not found")
    if forecast.resolution_source != "manual":
        raise InvalidRequest(
            f"forecast {forecast_id} has resolution_source={forecast.resolution_source!r} — "
            "daily_bars forecasts are resolver-only, not manually resolvable"
        )
    # manual `realized` is `{"note": …}` (or `{}` when no note) — `hit` is
    # already the column, not duplicated inside `realized` (F2b).
    return await ForecastLedger(redis, settings).record_outcome(
        forecast_id, resolution_status="manual", realized={},
        hit=body.hit, resolver_version="manual-resolve-v1", manual_note=body.note,
    )
