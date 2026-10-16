"""Tests for `routers/forecasts.py`.

Demo-mode reads/writes never touch the DB or `ForecastLedger`.
Non-demo round-trip exercises the router against a fake `ForecastLedger`
(issue_human/list/record_outcome) plus a fake `session_scope` backing the
one direct DB read `GET /{id}` does — both share one in-memory
`_FakeForecastStore` so the write path and the read path agree, mirroring
`test_reports_router.py`'s fake-session-scope convention (no live
Postgres/Redis needed).
"""
from __future__ import annotations

import contextlib
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

import app.routers.forecasts as forecasts_router
from app.config import Settings
from app.errors import DemoModeReadOnly, InvalidRequest, NotFound
from app.models import Forecast, Outcome
from app.services.forecasts import brier, log_score


def _settings(**kw: Any) -> Settings:
    kw.setdefault("demo_mode", False)
    return Settings(
        postgres_user="u", postgres_password="p", postgres_db="d", redis_password="r", **kw,
    )


def _boom_session_scope():
    raise AssertionError("demo mode must never open a DB session")


def _boom_ledger(*a, **kw):
    raise AssertionError("demo mode must never construct ForecastLedger")


# ── demo mode ────────────────────────────────────────────────────
async def test_list_forecasts_demo_mode_fixed_sample_never_touches_db(monkeypatch):
    monkeypatch.setattr(forecasts_router, "session_scope", _boom_session_scope)
    monkeypatch.setattr(forecasts_router, "ForecastLedger", _boom_ledger)
    settings = _settings(demo_mode=True)

    result = await forecasts_router.list_forecasts(settings=settings, redis=object())
    assert result["count"] == len(result["forecasts"])
    assert all(r["status"] == "pending" for r in result["forecasts"])  # default filter


async def test_list_forecasts_demo_mode_status_none_returns_all_six(monkeypatch):
    monkeypatch.setattr(forecasts_router, "session_scope", _boom_session_scope)
    monkeypatch.setattr(forecasts_router, "ForecastLedger", _boom_ledger)
    settings = _settings(demo_mode=True)

    result = await forecasts_router.list_forecasts(status=None, settings=settings, redis=object())
    assert result["count"] == 6


async def test_get_forecast_demo_mode_has_outcome_for_resolved_row(monkeypatch):
    monkeypatch.setattr(forecasts_router, "session_scope", _boom_session_scope)
    settings = _settings(demo_mode=True)

    row = await forecasts_router.get_forecast(forecast_id=4, settings=settings)
    assert row["status"] == "resolved"
    assert row["outcome"]["hit"] is True


async def test_get_forecast_demo_mode_unknown_id_404s(monkeypatch):
    monkeypatch.setattr(forecasts_router, "session_scope", _boom_session_scope)
    settings = _settings(demo_mode=True)
    with pytest.raises(NotFound):
        await forecasts_router.get_forecast(forecast_id=999, settings=settings)


async def test_issue_forecast_demo_mode_raises_before_any_db_touch(monkeypatch):
    monkeypatch.setattr(forecasts_router, "session_scope", _boom_session_scope)
    monkeypatch.setattr(forecasts_router, "ForecastLedger", _boom_ledger)
    settings = _settings(demo_mode=True)
    body = forecasts_router.ForecastIssueRequest(
        statement="EURUSD stays above 1.05.", probability=0.6, horizon_days=30,
        subject_kind="macro", subject="EURUSD",
    )
    with pytest.raises(DemoModeReadOnly):
        await forecasts_router.issue_forecast(body, settings=settings, redis=object())


async def test_resolve_forecast_demo_mode_raises_before_any_db_touch(monkeypatch):
    monkeypatch.setattr(forecasts_router, "session_scope", _boom_session_scope)
    monkeypatch.setattr(forecasts_router, "ForecastLedger", _boom_ledger)
    settings = _settings(demo_mode=True)
    with pytest.raises(DemoModeReadOnly):
        await forecasts_router.resolve_forecast(
            forecasts_router.ForecastResolveRequest(hit=True), forecast_id=1, settings=settings, redis=object(),
        )


# ── non-demo round trip (fake ledger + fake session_scope, shared store) ──
class _FakeForecastStore:
    def __init__(self) -> None:
        self.forecasts: dict[int, Forecast] = {}
        self.outcomes: dict[int, Outcome] = {}
        self._next_fid = 1
        self._next_oid = 1


def _make_fake_ledger(store: _FakeForecastStore):
    class _FakeLedger:
        def __init__(self, redis, settings) -> None:
            pass

        async def list(self, status=None, week=None, issued_by=None, limit=100):
            rows = list(store.forecasts.values())
            if status is not None:
                rows = [r for r in rows if r.status == status]
            if week is not None:
                rows = [r for r in rows if r.report_week == week]
            if issued_by is not None:
                rows = [r for r in rows if r.issued_by == issued_by]
            rows = sorted(rows, key=lambda r: -r.id)[:limit]
            return [forecasts_router._forecast_dict(r) for r in rows]

        async def issue_human(self, payload: dict[str, Any]) -> dict[str, Any]:
            if payload.get("probability") is None:
                raise InvalidRequest("issue_human requires an explicit probability")
            # mirrors the real ledger's ON CONFLICT DO NOTHING on
            # (issued_by, subject, decision_date, horizon_days)
            for existing in store.forecasts.values():
                if (
                    existing.issued_by == "human" and existing.subject == payload["subject"]
                    and existing.decision_date == payload["decision_date"]
                    and existing.horizon_days == payload["horizon_days"]
                ):
                    return {"issued": 0, "duplicates": 1, "errors": []}
            fid = store._next_fid
            store._next_fid += 1
            now = datetime.now(timezone.utc)
            subject_kind, source = payload["subject_kind"], payload.get("source")
            resolution_source = f"daily_bars:{source}" if subject_kind == "symbol" and source else "manual"
            row = Forecast(
                id=fid, issued_by="human", issuer_version="human", subject_kind=subject_kind,
                subject=payload["subject"], statement=payload["statement"], kind=payload.get("kind", "binary"),
                probability=payload["probability"], direction=payload.get("direction"), target=payload["target"],
                horizon_days=payload["horizon_days"], decision_date=payload["decision_date"],
                due_date=payload["decision_date"] + timedelta(days=payload["horizon_days"]),
                report_week=payload.get("report_week"), published_at=now, first_seen_at=now, available_at=now,
                resolution_source=resolution_source, resolution_rule=payload["resolution_rule"],
                evidence_refs=payload.get("evidence_refs") or [], status="pending", created_at=now,
            )
            store.forecasts[fid] = row
            return {"issued": 1, "duplicates": 0, "errors": []}

        async def get_by_natural_key(self, *, issued_by, subject, decision_date, horizon_days):
            for row in store.forecasts.values():
                if (
                    row.issued_by == issued_by and row.subject == subject
                    and row.decision_date == decision_date and row.horizon_days == horizon_days
                ):
                    return forecasts_router._forecast_dict(row)
            return None

        async def record_outcome(
            self, forecast_id: int, *, resolution_status, realized, hit, resolver_version, manual_note=None,
        ) -> dict[str, Any]:
            forecast = store.forecasts.get(forecast_id)
            if forecast is None:
                raise NotFound(f"forecast {forecast_id} not found")
            if forecast.status != "pending":
                raise InvalidRequest(f"forecast {forecast_id} is already '{forecast.status}'")
            if manual_note is not None:  # mirrors the real ledger's note merge
                realized = {**realized, "note": manual_note}
            p = forecast.probability
            b = brier(p, hit) if hit is not None else None
            ls = log_score(p, hit) if hit is not None else None
            oid = store._next_oid
            store._next_oid += 1
            outcome = Outcome(
                id=oid, forecast_id=forecast_id, resolved_at=datetime.now(timezone.utc),
                resolution_status=resolution_status, realized=realized, hit=hit, brier=b, log_score=ls,
                attribution={"thesis": "n/a", "direction": "hit" if hit else "miss", "sizing": "n/a"},
                resolver_version=resolver_version, created_at=datetime.now(timezone.utc),
            )
            store.outcomes[forecast_id] = outcome
            forecast.status = "resolved"
            return {
                "outcome": {
                    "id": oid, "forecast_id": forecast_id, "resolution_status": resolution_status,
                    "hit": hit, "brier": b, "log_score": ls,
                },
                "forecast_status": "resolved",
            }

    return _FakeLedger


def _wire_read_db(monkeypatch, store: _FakeForecastStore) -> None:
    class _Result:
        def __init__(self, value: object) -> None:
            self._value = value

        def scalar_one_or_none(self) -> object:
            return self._value

    class _FakeDB:
        async def execute(self, stmt: object) -> _Result:
            entity = stmt.column_descriptions[0]["entity"]
            key = stmt.whereclause.right.value
            if entity is Forecast:
                return _Result(store.forecasts.get(key))
            if entity is Outcome:
                return _Result(store.outcomes.get(key))
            raise AssertionError(f"unexpected entity in test double: {entity}")  # pragma: no cover

    db = _FakeDB()

    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        yield db

    monkeypatch.setattr(forecasts_router, "session_scope", _fake_session_scope)


def _wire(monkeypatch) -> _FakeForecastStore:
    store = _FakeForecastStore()
    monkeypatch.setattr(forecasts_router, "ForecastLedger", _make_fake_ledger(store))
    _wire_read_db(monkeypatch, store)
    return store


async def test_round_trip_issue_list_resolve_get_shows_outcome_brier(monkeypatch):
    _wire(monkeypatch)
    settings = _settings(demo_mode=False)
    body = forecasts_router.ForecastIssueRequest(
        statement="EURUSD stays above 1.05 over the next 30 days.", probability=0.7,
        horizon_days=30, subject_kind="macro", subject="EURUSD",
    )
    issued = await forecasts_router.issue_forecast(body, settings=settings, redis=object())
    assert issued["issued"] == 1 and issued["duplicates"] == 0 and issued["errors"] == []
    assert issued["forecast"]["subject"] == "EURUSD"
    assert issued["forecast"]["status"] == "pending"

    listing = await forecasts_router.list_forecasts(
        status="pending", week=None, issued_by="human", limit=200, settings=settings, redis=object(),
    )
    assert listing["count"] == 1
    forecast_id = listing["forecasts"][0]["id"]
    assert listing["forecasts"][0]["resolution_source"] == "manual"

    resolved = await forecasts_router.resolve_forecast(
        forecasts_router.ForecastResolveRequest(hit=True, note="matured true"),
        forecast_id=forecast_id, settings=settings, redis=object(),
    )
    assert resolved["forecast_status"] == "resolved"

    detail = await forecasts_router.get_forecast(forecast_id=forecast_id, settings=settings)
    assert detail["outcome"]["hit"] is True
    assert detail["outcome"]["brier"] == pytest.approx(brier(0.7, True))
    assert detail["outcome"]["log_score"] == pytest.approx(log_score(0.7, True))


async def test_issue_forecast_response_shape_matches_get_minus_outcome(monkeypatch):
    """fix: POST /forecasts additively returns the created row as
    `"forecast"` (same shape as `GET /forecasts/{id}` minus `outcome`)."""
    store = _wire(monkeypatch)
    settings = _settings(demo_mode=False)
    body = forecasts_router.ForecastIssueRequest(
        statement="EURUSD stays above 1.05.", probability=0.6, horizon_days=30,
        subject_kind="macro", subject="EURUSD",
    )
    issued = await forecasts_router.issue_forecast(body, settings=settings, redis=object())
    forecast_id = issued["forecast"]["id"]

    detail = await forecasts_router.get_forecast(forecast_id=forecast_id, settings=settings)
    detail_minus_outcome = {k: v for k, v in detail.items() if k != "outcome"}
    assert issued["forecast"] == detail_minus_outcome


async def test_issue_forecast_returns_null_forecast_on_duplicate(monkeypatch):
    store = _wire(monkeypatch)
    settings = _settings(demo_mode=False)
    body = forecasts_router.ForecastIssueRequest(
        statement="EURUSD stays above 1.05.", probability=0.6, horizon_days=30,
        subject_kind="macro", subject="EURUSD",
    )
    first = await forecasts_router.issue_forecast(body, settings=settings, redis=object())
    assert first["forecast"] is not None
    second = await forecasts_router.issue_forecast(body, settings=settings, redis=object())
    assert second["issued"] == 0 and second["duplicates"] == 1
    assert second["forecast"] is None


async def test_resolve_daily_bars_forecast_manually_returns_400(monkeypatch):
    _wire(monkeypatch)
    settings = _settings(demo_mode=False)
    body = forecasts_router.ForecastIssueRequest(
        statement="AAA forward return over the next 28 days is greater than 0.", probability=0.6,
        horizon_days=28, subject_kind="symbol", subject="AAA", source="yfinance", direction=1,
    )
    await forecasts_router.issue_forecast(body, settings=settings, redis=object())
    listing = await forecasts_router.list_forecasts(
        status="pending", week=None, issued_by="human", limit=200, settings=settings, redis=object(),
    )
    forecast_id = listing["forecasts"][0]["id"]
    assert listing["forecasts"][0]["resolution_source"] == "daily_bars:yfinance"

    with pytest.raises(InvalidRequest):
        await forecasts_router.resolve_forecast(
            forecasts_router.ForecastResolveRequest(hit=True), forecast_id=forecast_id,
            settings=settings, redis=object(),
        )


async def test_resolve_forecast_twice_raises(monkeypatch):
    _wire(monkeypatch)
    settings = _settings(demo_mode=False)
    body = forecasts_router.ForecastIssueRequest(
        statement="EURUSD stays above 1.05.", probability=0.5, horizon_days=30,
        subject_kind="macro", subject="EURUSD",
    )
    await forecasts_router.issue_forecast(body, settings=settings, redis=object())
    listing = await forecasts_router.list_forecasts(
        status="pending", week=None, issued_by="human", limit=200, settings=settings, redis=object(),
    )
    forecast_id = listing["forecasts"][0]["id"]
    await forecasts_router.resolve_forecast(
        forecasts_router.ForecastResolveRequest(hit=True), forecast_id=forecast_id, settings=settings, redis=object(),
    )
    with pytest.raises(InvalidRequest):
        await forecasts_router.resolve_forecast(
            forecasts_router.ForecastResolveRequest(hit=False), forecast_id=forecast_id,
            settings=settings, redis=object(),
        )


# ── F2: sourced human forecast target/realized shape ─────
async def test_sourced_forecast_builds_full_forward_return_target(monkeypatch):
    """F2a: `subject_kind=symbol` + `source` used to persist a manual-shaped
    `target` with no `metric` — `forecast_resolver.resolve_due` then raised
    `KeyError('metric')` on it forever. It must now carry a full resolvable target
    the resolver can actually grade."""
    _wire(monkeypatch)
    settings = _settings(demo_mode=False)
    body = forecasts_router.ForecastIssueRequest(
        statement="AAA forward return over the next 28 days is greater than 0.", probability=0.6,
        horizon_days=28, subject_kind="symbol", subject="AAA", source="yfinance", direction=1,
    )
    issued = await forecasts_router.issue_forecast(body, settings=settings, redis=object())
    assert issued["forecast"]["target"] == {"metric": "forward_return", "comparator": ">", "threshold": 0.0}
    assert issued["forecast"]["direction"] == 1
    assert issued["forecast"]["resolution_rule"]["entry"] == "first close on/after decision_date within stale_days"

    body_down = forecasts_router.ForecastIssueRequest(
        statement="BBB forward return over the next 28 days is less than 0.", probability=0.6,
        horizon_days=28, subject_kind="symbol", subject="BBB", source="yfinance", direction=-1,
    )
    issued_down = await forecasts_router.issue_forecast(body_down, settings=settings, redis=object())
    assert issued_down["forecast"]["target"] == {"metric": "forward_return", "comparator": "<", "threshold": 0.0}


async def test_sourced_forecast_without_direction_raises_invalid_request(monkeypatch):
    """F2a: a sourced POST with no direction cannot build a resolvable
    target at all — 422 (`InvalidRequest`), never a silently-broken row."""
    _wire(monkeypatch)
    settings = _settings(demo_mode=False)
    body = forecasts_router.ForecastIssueRequest(
        statement="AAA forward return over the next 28 days is greater than 0.", probability=0.6,
        horizon_days=28, subject_kind="symbol", subject="AAA", source="yfinance",
    )
    with pytest.raises(InvalidRequest):
        await forecasts_router.issue_forecast(body, settings=settings, redis=object())


async def test_manual_resolve_realized_carries_only_note_not_hit(monkeypatch):
    """F2b: the spec says manual `realized` is `{"note": …}` — `hit` already lives
    on the `outcomes.hit` column and must not be duplicated inside
    `realized`."""
    store = _wire(monkeypatch)
    settings = _settings(demo_mode=False)
    body = forecasts_router.ForecastIssueRequest(
        statement="EURUSD stays above 1.05.", probability=0.5, horizon_days=30,
        subject_kind="macro", subject="EURUSD",
    )
    await forecasts_router.issue_forecast(body, settings=settings, redis=object())
    listing = await forecasts_router.list_forecasts(
        status="pending", week=None, issued_by="human", limit=200, settings=settings, redis=object(),
    )
    forecast_id = listing["forecasts"][0]["id"]
    resolved = await forecasts_router.resolve_forecast(
        forecasts_router.ForecastResolveRequest(hit=True, note="matured true"),
        forecast_id=forecast_id, settings=settings, redis=object(),
    )
    assert resolved["outcome"]["hit"] is True
    stored_realized = store.outcomes[forecast_id].realized
    assert stored_realized == {"note": "matured true"}
