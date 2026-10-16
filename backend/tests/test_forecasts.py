"""Tests for the forecast ledger service.

Pure-helper and `build_shadow_forecasts` tests need no I/O and run
everywhere. The async DB methods (`issue`, `record_outcome`, …) are
exercised against a REAL Postgres, monkeypatching `session_scope` onto a
sessionmaker backed by that engine — mirrors `test_models_forecasts.py`'s
`asyncio.run(...)` convention and its "connect or skip" seam: `postgres` is
only DNS-resolvable inside the compose network, so these skip cleanly on
the host (outside docker) rather than hanging on a TCP retry.
"""
from __future__ import annotations

import asyncio
import contextlib
import math
import time
from datetime import date, datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import app.services.forecasts as forecasts_module
from app.config import Settings
from app.errors import InvalidRequest
from app.services.forecasts import (
    ForecastLedger,
    brier,
    build_shadow_forecasts,
    laplace_probability,
    log_score,
)


def _settings(**kw) -> Settings:
    kw.setdefault("demo_mode", False)
    return Settings(
        postgres_user="u", postgres_password="p", postgres_db="d", redis_password="r", **kw,
    )


# ── pure helpers ─────────────────────────────────────────────────────────
def test_laplace_probability_table():
    assert laplace_probability(0, 0) == 0.5
    # (3, 4) is 2/3, NOT 0.8 — a table entry reading (3,4)=0.8 is simply wrong (B1).
    assert laplace_probability(3, 4) == pytest.approx(2 / 3)
    assert laplace_probability(7, 8) == pytest.approx(0.8)


def test_brier_score():
    assert brier(0.8, True) == pytest.approx(0.04)
    assert brier(0.8, False) == pytest.approx(0.64)


def test_log_score_clamps_at_the_boundary():
    assert log_score(1.0, True) == pytest.approx(math.log(0.99))
    assert log_score(0.0, False) == pytest.approx(math.log(0.99))


# ── build_shadow_forecasts (pure, no I/O) ───────────────────────────────
_BAR_TS = datetime(2026, 8, 31, tzinfo=timezone.utc)
_FIRST_SEEN = datetime(2026, 8, 31, 12, 0, tzinfo=timezone.utc)
_REPORT_WEEK = "2026-W36"


def _verdict(tool, symbol, verdict, direction, reading=None):
    return {
        "report_week": _REPORT_WEEK, "symbol": symbol, "tool": tool, "verdict": verdict,
        "direction": direction, "reading": reading or {}, "rationale": "test fixture",
        "bar_ts": _BAR_TS,
    }


def _verdict_rows():
    return [
        _verdict("technicals", "AAA", "add_watch", 1),       # gradeable, seeded issuer
        _verdict("technicals", "BBB", "trim_watch", -1),     # gradeable, but already covered
        _verdict("patterns", "CCC", "hold", None),           # ungradeable (hold)
        _verdict("forensic", "DDD", "flag", None),           # ungradeable (forensic never issues)
        _verdict("factor", "EEE", "add_watch", 1, reading={"value": 1.0, "source": "yfinance"}),
        _verdict("signals", "FFF", "trim_watch", -1, reading={"value": -1.0}),
    ]


def _build(open_pending=None, scoreboard_stats=None):
    return build_shadow_forecasts(
        _REPORT_WEEK,
        _verdict_rows(),
        open_pending if open_pending is not None else set(),
        scoreboard_stats if scoreboard_stats is not None else {},
        first_seen_at=_FIRST_SEEN,
        settings=_settings(),
    )


def test_one_forecast_per_gradeable_verdict_not_covered():
    # BBB's verdict direction is -1 (trim_watch) — the SAME direction as the
    # open pending row, so it is correctly covered/skipped.
    rows, skipped = _build(open_pending={("shadow:technicals", "BBB", -1)})
    subjects = {(r["issued_by"], r["subject"]) for r in rows}
    # AAA/EEE/FFF issue; BBB skipped (open_pending), CCC/DDD never gradeable.
    assert subjects == {("shadow:technicals", "AAA"), ("shadow:factor", "EEE"), ("shadow:signals", "FFF")}
    assert skipped == {"covered": 1, "ungradeable": 2}


def test_flipped_verdict_issues_immediately_same_direction_still_skipped():
    """F3 (SEV2, fix round): a pending forecast for the SAME direction still
    covers/skips a re-issuance; a FLIPPED verdict (opposite direction on the
    book) issues immediately — the old pending row resolves on its own
    terms and never blocks the fresh, opposite call."""
    # AAA's verdict this week is direction +1 (add_watch, see `_verdict_rows`).
    # A pending SAME-direction (+1) row must still cover/skip it...
    rows, skipped = _build(open_pending={("shadow:technicals", "AAA", 1)})
    assert "AAA" not in {r["subject"] for r in rows if r["issued_by"] == "shadow:technicals"}
    assert skipped["covered"] == 1

    # ...but a pending OPPOSITE-direction (-1) row — a flip — must NOT.
    rows, skipped = _build(open_pending={("shadow:technicals", "AAA", -1)})
    assert "AAA" in {r["subject"] for r in rows if r["issued_by"] == "shadow:technicals"}
    assert skipped["covered"] == 0


def test_cross_sectional_tools_get_excess_return_target_and_book_median_rule():
    rows, _ = _build()
    eee = next(r for r in rows if r["subject"] == "EEE")
    assert eee["target"] == {"metric": "excess_return_vs_book_median", "comparator": ">", "threshold": 0.0}
    assert eee["resolution_rule"]["book_median"] == {"tool": "factor", "report_week": _REPORT_WEEK}
    aaa = next(r for r in rows if r["subject"] == "AAA")
    assert aaa["target"]["metric"] == "forward_return"
    assert aaa["resolution_rule"]["book_median"] is None


def test_seeded_issuer_uses_scoreboard_prior_no_prior_note():
    # _score_tool-shaped stats: hit_rate=0.75, effective_n=8 -> hits=6, n=8
    # -> laplace(6, 8) == 7/10, and 8 >= forecast_probability_min_n (8).
    rows, _ = _build(scoreboard_stats={
        "technicals": {"hit_rate": 0.75, "n_graded": 16, "pushes": 0, "effective_n": 8},
    })
    aaa = next(r for r in rows if r["subject"] == "AAA")
    assert aaa["probability"] == pytest.approx(0.7)
    kinds = [e["kind"] for e in aaa["evidence_refs"]]
    assert "prior_seed" in kinds
    assert "prior_note" not in kinds
    seed = next(e for e in aaa["evidence_refs"] if e["kind"] == "prior_seed")
    assert seed == {"kind": "prior_seed", "source": "shadow_scoreboard", "hits": 6, "n": 8, "report_week": _REPORT_WEEK}


def test_unseeded_issuer_gets_prior_probability_and_prior_note():
    rows, _ = _build()  # no scoreboard_stats at all
    eee = next(r for r in rows if r["subject"] == "EEE")
    assert eee["probability"] == pytest.approx(0.5)
    kinds = [e["kind"] for e in eee["evidence_refs"]]
    assert "prior_note" in kinds


def test_statement_suffix_only_on_greater_than_comparator():
    rows, _ = _build()
    aaa = next(r for r in rows if r["subject"] == "AAA")  # direction +1 -> ">"
    fff = next(r for r in rows if r["subject"] == "FFF")  # direction -1 -> "<"
    assert aaa["statement"].endswith("(exactly zero = no decision).")
    assert not fff["statement"].endswith("(exactly zero = no decision).")


# ── issue()/record_outcome() — real Postgres, skip cleanly if unreachable ──
def _pg_engine_or_skip():
    settings = _settings()
    engine = create_async_engine(settings.database_url, connect_args={"timeout": 3})

    async def _ping():
        async with engine.connect() as conn:
            await conn.execute(sa.text("SELECT 1"))

    try:
        asyncio.run(_ping())
    except Exception as exc:  # OperationalError, OSError/gaierror, TimeoutError, …
        asyncio.run(engine.dispose())
        pytest.skip(f"Postgres unreachable from host ({exc.__class__.__name__}): {exc}")
    return settings, engine


def _real_ledger(monkeypatch):
    settings, engine = _pg_engine_or_skip()
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        async with session_factory() as session:
            yield session

    monkeypatch.setattr(forecasts_module, "session_scope", _fake_session_scope)
    return ForecastLedger(redis=None, settings=settings), engine


_BASE_TARGET = {"metric": "forward_return", "comparator": ">", "threshold": 0.0}
_BASE_RULE = {
    "symbol": "TEST", "source": "yfinance",
    "entry": "first close on/after decision_date within stale_days",
    "exit": "first close on/after due_date within stale_days",
    "book_median": None,
}


def _forecast_row(**overrides):
    now = datetime(2026, 9, 2, tzinfo=timezone.utc)
    values = dict(
        issued_by="human", issuer_version="human", subject_kind="symbol", subject="TEST",
        statement="TEST closes higher than today in 28 days.", kind="binary", probability=0.6,
        target=dict(_BASE_TARGET), horizon_days=28, decision_date=date(2026, 9, 2),
        due_date=date(2026, 9, 30), published_at=now, first_seen_at=now, available_at=now,
        resolution_source="manual", resolution_rule=dict(_BASE_RULE),
        evidence_refs=[{"kind": "issuance_reason", "value": "test fixture"}],
    )
    values.update(overrides)
    return values


def test_second_issue_of_the_same_rows_is_all_duplicates(monkeypatch):
    ledger, engine = _real_ledger(monkeypatch)
    subject = f"S2_DUP_{int(time.time() * 1000)}"
    try:
        rows = [_forecast_row(subject=subject)]

        async def _run():
            return await ledger.issue(rows), await ledger.issue(rows)

        first, second = asyncio.run(_run())
        assert first == {"issued": 1, "duplicates": 0, "errors": []}
        assert second["issued"] == 0
        assert second["duplicates"] == len(rows)
    finally:
        asyncio.run(engine.dispose())


def test_pit_violating_row_lands_in_errors_not_in_the_db(monkeypatch):
    # No live Postgres needed for this one: `issue()` only ever enters
    # `session_scope` when there is at least one VALID row — replacing it
    # with a call-and-fail seam proves an all-invalid batch never touches
    # the DB at all.
    class _BoomSessionScope:
        async def __aenter__(self):
            raise AssertionError("session_scope must not be entered for an all-invalid batch")

        async def __aexit__(self, *exc_info):
            return False

    monkeypatch.setattr(forecasts_module, "session_scope", lambda: _BoomSessionScope())
    ledger = ForecastLedger(redis=None, settings=_settings())

    now = datetime(2026, 9, 2, 12, 0, tzinfo=timezone.utc)
    earlier = datetime(2026, 9, 2, 11, 0, tzinfo=timezone.utc)
    bad_row = _forecast_row(subject="PIT_BAD", first_seen_at=now, available_at=earlier)

    result = asyncio.run(ledger.issue([bad_row]))
    assert result["issued"] == 0
    assert len(result["errors"]) == 1
    assert "PIT" in result["errors"][0]["reason"]


def test_retro_dated_row_before_ledger_floor_lands_in_errors_not_in_the_db(monkeypatch):
    """the spec 'no retro-dated forecasts': FORECAST_LEDGER_START_DATE is a
    hard floor on `decision_date` — a row dated before it is rejected into
    `errors`, same non-raising path as a PIT violation, never inserted even
    when a human forces an old week's pack."""
    class _BoomSessionScope:
        async def __aenter__(self):
            raise AssertionError("session_scope must not be entered for an all-invalid batch")

        async def __aexit__(self, *exc_info):
            return False

    monkeypatch.setattr(forecasts_module, "session_scope", lambda: _BoomSessionScope())
    settings = _settings(forecast_ledger_start_date=date(2026, 8, 31))
    ledger = ForecastLedger(redis=None, settings=settings)

    stale_row = _forecast_row(subject="RETRO_DATED", decision_date=date(2026, 8, 1))

    result = asyncio.run(ledger.issue([stale_row]))
    assert result["issued"] == 0
    assert len(result["errors"]) == 1
    assert "ledger floor" in result["errors"][0]["reason"]


def test_row_on_the_ledger_floor_is_accepted_by_the_floor_check(monkeypatch):
    """The floor is inclusive-below (`<`, not `<=`) — a row dated exactly on
    `FORECAST_LEDGER_START_DATE` must clear the floor check and reach
    insertion (real Postgres — skips cleanly on the host, mirrors
    `test_second_issue_of_the_same_rows_is_all_duplicates`)."""
    ledger, engine = _real_ledger(monkeypatch)
    assert ledger._settings.forecast_ledger_start_date == date(2026, 8, 31)
    subject = f"S2_ON_FLOOR_{int(time.time() * 1000)}"
    try:
        row = _forecast_row(subject=subject, decision_date=date(2026, 8, 31))
        result = asyncio.run(ledger.issue([row]))
        assert result == {"issued": 1, "duplicates": 0, "errors": []}
    finally:
        asyncio.run(engine.dispose())


def test_record_outcome_twice_raises(monkeypatch):
    ledger, engine = _real_ledger(monkeypatch)
    subject = f"S2_TWICE_{int(time.time() * 1000)}"
    try:
        async def _run():
            await ledger.issue([_forecast_row(subject=subject)])
            rows = await ledger.list(issued_by="human", limit=1000)
            forecast_id = next(r["id"] for r in rows if r["subject"] == subject)
            await ledger.record_outcome(
                forecast_id, resolution_status="matured", realized={"entry": 1, "exit": 1.1, "value": 0.1},
                hit=True, resolver_version="test",
            )
            with pytest.raises(InvalidRequest):
                await ledger.record_outcome(
                    forecast_id, resolution_status="matured", realized={"entry": 1, "exit": 1.1, "value": 0.1},
                    hit=True, resolver_version="test",
                )

        asyncio.run(_run())
    finally:
        asyncio.run(engine.dispose())


def test_record_outcome_hit_none_stores_null_brier_and_log_score(monkeypatch):
    ledger, engine = _real_ledger(monkeypatch)
    subject = f"S2_PUSH_{int(time.time() * 1000)}"
    try:
        async def _run():
            await ledger.issue([_forecast_row(subject=subject)])
            rows = await ledger.list(issued_by="human", limit=1000)
            forecast_id = next(r["id"] for r in rows if r["subject"] == subject)
            return await ledger.record_outcome(
                forecast_id, resolution_status="uncovered", realized={"note": "gap"},
                hit=None, resolver_version="test",
            )

        result = asyncio.run(_run())
        assert result["outcome"]["hit"] is None
        assert result["outcome"]["brier"] is None
        assert result["outcome"]["log_score"] is None
        assert result["forecast_status"] == "abstained"
    finally:
        asyncio.run(engine.dispose())


# ── resolved_keys() — batches to exhaustion past the old list(limit=1000)
#    cap. Fake session/id-cursor, no live Postgres needed — same
#    convention as `test_recommendation_lifecycle.py` / `test_decisions.py`. ──
def _id_cursor_from(stmt: object) -> int:
    for clause in stmt.whereclause.clauses:
        if clause.left.key == "id":
            return clause.right.value
    raise AssertionError("resolved_keys' statement must filter on Forecast.id")


class _FakeCursorSession:
    """Serves `resolved_keys()`'s `id > last_id` pagination straight off a
    pre-sorted synthetic row list, extracting the cursor and page size the
    real statement carries — proves the loop-to-exhaustion behaviour
    without a live Postgres."""

    def __init__(self, sorted_rows: list[tuple[int, str, int]]) -> None:
        self._rows = sorted_rows
        self.call_count = 0

    async def execute(self, stmt: object) -> _SeqResult:
        self.call_count += 1
        last_id = _id_cursor_from(stmt)
        batch_size = stmt._limit_clause.value
        page = [r for r in self._rows if r[0] > last_id][:batch_size]
        return _SeqResult(page)


class _SeqResult:
    def __init__(self, rows: list[tuple[int, str, int]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[int, str, int]]:
        return self._rows


def test_resolved_keys_loops_past_the_old_1000_row_cap(monkeypatch):
    """1200 synthetic resolved rows, all distinct `(subject, direction)`
    pairs, for one issuer — a single capped `list(..., limit=1000)` call
    would have silently dropped the last 200; `resolved_keys()` must return
    all 1200 across two pages."""
    rows = [(i, f"SYN_{i}", 1) for i in range(1, 1201)]
    session = _FakeCursorSession(rows)

    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        yield session

    monkeypatch.setattr(forecasts_module, "session_scope", _fake_session_scope)
    ledger = ForecastLedger(redis=None, settings=_settings())

    keys = asyncio.run(ledger.resolved_keys("human"))

    assert len(keys) == 1200
    assert keys == {(f"SYN_{i}", 1) for i in range(1, 1201)}
    assert session.call_count == 2  # 1000-row page, then the trailing 200 that ends the loop


def test_resolved_keys_single_page_under_batch_size_stops_after_one_call(monkeypatch):
    rows = [(i, f"SYN_{i}", 1) for i in range(1, 51)]
    session = _FakeCursorSession(rows)

    @contextlib.asynccontextmanager
    async def _fake_session_scope():
        yield session

    monkeypatch.setattr(forecasts_module, "session_scope", _fake_session_scope)
    ledger = ForecastLedger(redis=None, settings=_settings())

    keys = asyncio.run(ledger.resolved_keys("human"))

    assert len(keys) == 50
    assert session.call_count == 1
