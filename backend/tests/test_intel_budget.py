"""Intel enrichment wall-clock budget + cancellation hygiene.

Failure mode: the report-worker's in-process enrich job, at ~46s per
`claude -p` call, could eat the whole 1200s REPORT_PACK_JOB_TIMEOUT_SECONDS
cap before `_fetch_panels` even started, and the cancelled `ai_runs` row was left
`running` forever. These tests pin the budget seam + the cancel path.
"""
from __future__ import annotations

import asyncio
import contextlib
from typing import Any

import app.services.intel_service as intel_service_module
from app.config import Settings
from app.services.intel import Enrichment
from app.services.intel_service import IntelService
from app.services.news import NewsItem


def _settings(**kw: Any) -> Settings:
    kw.setdefault("demo_mode", False)
    return Settings(postgres_user="u", postgres_password="p", postgres_db="d", redis_password="r", **kw)


class _FakeRedis:
    async def publish(self, *a: Any, **k: Any) -> None:
        return None

    async def lrem(self, *a: Any, **k: Any) -> None:
        return None


class _Clock:
    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t


class _TickingEngine:
    """Each enrich call 'takes' 10s on the fake clock."""
    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.calls = 0

    async def enrich_item(self, adapter: Any, item: NewsItem) -> Enrichment:
        self.calls += 1
        self.clock.t += 10.0
        return Enrichment(summary="s", grounding="speculative")


def _items(n: int) -> list[NewsItem]:
    return [NewsItem(title=f"t{i}", link=f"l{i}", source="s", published=None, summary=None) for i in range(n)]


async def _noop_publish(*a: Any, **k: Any) -> None:
    return None


async def test_enrich_items_stops_at_deadline_and_keeps_the_real_partial_work():
    svc = IntelService(_FakeRedis(), _settings())
    svc._publish = _noop_publish  # type: ignore[method-assign]
    clock = _Clock()
    engine = _TickingEngine(clock)

    enriched, failed, truncated_at = await svc._enrich_items(
        "job", object(), engine, _items(10), deadline=25.0, clock=clock,
    )

    # t=0 -> item0 (t=10), item1 (t=20), item2 (t=30) — then 30 >= 25: stop.
    assert engine.calls == 3
    assert len(enriched) == 3
    assert truncated_at == 3
    assert failed == 0
    assert [r["link"] for r in enriched] == ["l0", "l1", "l2"]  # real items only, nothing padded


async def test_enrich_items_without_deadline_runs_every_item():
    svc = IntelService(_FakeRedis(), _settings())
    svc._publish = _noop_publish  # type: ignore[method-assign]
    clock = _Clock()
    engine = _TickingEngine(clock)

    enriched, failed, truncated_at = await svc._enrich_items(
        "job", object(), engine, _items(4), deadline=None, clock=clock,
    )

    assert engine.calls == 4 and len(enriched) == 4
    assert truncated_at is None


class _RecordingSession:
    def __init__(self, log: list[dict[str, Any]]) -> None:
        self.log = log

    async def execute(self, stmt: Any) -> Any:
        with contextlib.suppress(Exception):
            self.log.append(dict(stmt.compile().params))

        class _Res:
            def scalar_one_or_none(self_inner) -> int:
                return 1
        return _Res()

    async def commit(self) -> None:
        return None


async def test_cancelled_execute_marks_the_run_failed_instead_of_leaving_it_running(monkeypatch):
    log: list[dict[str, Any]] = []

    @contextlib.asynccontextmanager
    async def _scope():
        yield _RecordingSession(log)

    started = asyncio.Event()

    async def _hanging_news(redis: Any, settings: Any) -> list[NewsItem]:
        started.set()
        await asyncio.sleep(3600)
        return []

    monkeypatch.setattr(intel_service_module, "session_scope", _scope)
    monkeypatch.setattr(intel_service_module, "get_news", _hanging_news)

    svc = IntelService(_FakeRedis(), _settings())
    task = asyncio.create_task(svc.execute("job-x", "report-worker:1"))
    await asyncio.wait_for(started.wait(), 2)
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass

    statuses = [p.get("status") for p in log if "status" in p]
    assert statuses[-1] == "failed", log


# ── terminal-state guarantees on every exit path ──
# A tiny in-memory `ai_runs` table that interprets the service's real
# SQLAlchemy statements (eq / IN where-clauses), so conditional updates are
# checked by their actual semantics, not by string-matching SQL.
from sqlalchemy.sql import operators  # noqa: E402
from sqlalchemy.sql.dml import Update  # noqa: E402
from sqlalchemy.sql.selectable import Select  # noqa: E402


def _bind_value(expr: Any) -> Any:
    return expr.effective_value if hasattr(expr, "effective_value") else expr.value


def _matches(row: dict[str, Any], crit: Any) -> bool:
    col, val = crit.left.key, _bind_value(crit.right)
    if crit.operator is operators.eq:
        return row.get(col) == val
    if crit.operator is operators.in_op:
        return row.get(col) in val
    raise NotImplementedError(crit.operator)


class _AiRunsDB:
    """`hold` names a checkpoint (`commit`, or `update:<status|phase>`) at
    which the session parks forever — the test cancels the task there."""

    def __init__(self, hold: str | None = None) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.hold = hold
        self.reached = asyncio.Event()

    async def _checkpoint(self, label: str) -> None:
        if label == self.hold:
            self.hold = None  # park once; the cleanup write must go through
            self.reached.set()
            await asyncio.sleep(3600)

    def scope(self):
        db = self

        class _Result:
            def __init__(self, scalar: Any) -> None:
                self._scalar = scalar

            def scalar_one_or_none(self) -> Any:
                return self._scalar

            def scalar_one(self) -> Any:
                return self._scalar

        class _Session:
            def __init__(self) -> None:
                self.pending: list[Any] = []

            def add(self, row: Any) -> None:
                self.pending.append(row)

            async def commit(self) -> None:
                for r in self.pending:  # commit lands, THEN the caller may be cancelled
                    db.rows[r.job_id] = {"job_id": r.job_id, "status": r.status, "phase": None}
                self.pending.clear()
                await db._checkpoint("commit")

            async def execute(self, stmt: Any) -> _Result:
                if isinstance(stmt, Select):
                    return _Result(sum(r["status"] in ("queued", "running") for r in db.rows.values()))
                assert isinstance(stmt, Update), stmt
                values = {getattr(k, "key", k): _bind_value(v) for k, v in stmt._values.items()}
                await db._checkpoint(f"update:{values.get('status') or values.get('phase')}")
                hit = [r for r in db.rows.values() if all(_matches(r, c) for c in stmt._where_criteria)]
                for r in hit:
                    r.update(values)
                return _Result(1 if hit else None)

        @contextlib.asynccontextmanager
        async def _scope():
            yield _Session()

        return _scope


def _wire(monkeypatch, db: _AiRunsDB, *, news: Any = None, upsert: Any = None) -> IntelService:
    async def _news(redis: Any, settings: Any) -> list[NewsItem]:
        return _items(2)

    class _Engine:
        async def enrich_item(self, adapter: Any, item: NewsItem) -> Enrichment:
            return Enrichment(summary="s", grounding="speculative")

    monkeypatch.setattr(intel_service_module, "session_scope", db.scope())
    monkeypatch.setattr(intel_service_module, "get_news", news or _news)
    monkeypatch.setattr(intel_service_module, "build_llm_adapter", lambda *a, **k: object())
    monkeypatch.setattr(intel_service_module, "IntelEngine", _Engine)
    svc = IntelService(_FakeRedis(), _settings())

    async def _noop_upsert(rows: Any) -> None:
        return None
    svc._upsert = upsert or _noop_upsert  # type: ignore[method-assign]
    return svc


async def _cancel_when(task: asyncio.Task, event: asyncio.Event) -> None:
    await asyncio.wait_for(event.wait(), 2)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def test_run_inline_happy_path_ends_done(monkeypatch):
    db = _AiRunsDB()
    svc = _wire(monkeypatch, db)
    await svc.run_inline("j1", "report-worker:1")
    assert db.rows["j1"]["status"] == "done"


async def test_cancel_between_insert_and_claim_leaves_no_queued_row(monkeypatch):
    """Finding 2: the row is committed `queued`, the caller is cancelled
    before `_execute` claims it — nothing in the worker deployment would ever consume it."""
    db = _AiRunsDB(hold="commit")
    svc = _wire(monkeypatch, db)
    task = asyncio.create_task(svc.run_inline("j2", "report-worker:1"))
    await _cancel_when(task, db.reached)
    assert db.rows["j2"]["status"] == "failed", db.rows
    assert db.rows["j2"]["phase"] == "cancelled"


async def test_cancel_during_the_claim_update_leaves_no_queued_row(monkeypatch):
    db = _AiRunsDB(hold="update:running")
    svc = _wire(monkeypatch, db)
    task = asyncio.create_task(svc.run_inline("j3", "report-worker:1"))
    await _cancel_when(task, db.reached)
    assert db.rows["j3"]["status"] == "failed", db.rows


async def test_cancel_after_done_commit_does_not_rewrite_the_success(monkeypatch):
    """Finding 3: `done` is committed, then the cap cancels us while the
    completion is being published — the persisted success must stand."""
    db = _AiRunsDB()
    svc = _wire(monkeypatch, db)
    published_done = asyncio.Event()
    real_publish = svc._publish

    async def _publish(job_id: str, frac: float, phase: str, status: str = "running", **kw: Any) -> None:
        if status == "done":
            published_done.set()
            await asyncio.sleep(3600)
        await real_publish(job_id, frac, phase, status=status, **kw)
    svc._publish = _publish  # type: ignore[method-assign]

    db.rows["j4"] = {"job_id": "j4", "status": "queued", "phase": None}
    task = asyncio.create_task(svc.execute("j4", "report-worker:1"))
    await _cancel_when(task, published_done)
    assert db.rows["j4"]["status"] == "done", db.rows
    assert db.rows["j4"]["phase"] == "done"


async def test_error_after_done_commit_does_not_rewrite_the_success(monkeypatch):
    """Same race on the `except Exception` path: a failure after the `done`
    commit (here: the completion publish) must not flip it to failed."""
    db = _AiRunsDB()
    svc = _wire(monkeypatch, db)
    real_publish = svc._publish

    async def _publish(job_id: str, frac: float, phase: str, status: str = "running", **kw: Any) -> None:
        if status == "done":
            raise RuntimeError("redis gone")
        await real_publish(job_id, frac, phase, status=status, **kw)
    svc._publish = _publish  # type: ignore[method-assign]

    db.rows["j5"] = {"job_id": "j5", "status": "queued", "phase": None}
    await svc.execute("j5", "report-worker:1")
    assert db.rows["j5"]["status"] == "done", db.rows


async def test_hard_timeout_during_news_fetch_marks_the_row_failed(monkeypatch):
    """Finding 1+2 together: an outer `asyncio.timeout` firing inside the
    news fetch (before any per-article deadline check) still ends terminal."""
    db = _AiRunsDB()

    async def _hanging_news(redis: Any, settings: Any) -> list[NewsItem]:
        await asyncio.sleep(3600)
        return []

    svc = _wire(monkeypatch, db, news=_hanging_news)
    try:
        async with asyncio.timeout(0.2):
            await svc.run_inline("j6", "report-worker:1")
    except TimeoutError:
        pass
    assert db.rows["j6"]["status"] == "failed", db.rows


async def test_run_inline_refuses_when_too_many_jobs_in_flight(monkeypatch):
    from app.errors import InvalidRequest

    db = _AiRunsDB()
    db.rows["other"] = {"job_id": "other", "status": "running", "phase": None}
    svc = _wire(monkeypatch, db)
    svc._settings = _settings(ai_max_queued=1)
    try:
        await svc.run_inline("j7", "report-worker:1")
    except InvalidRequest:
        pass
    else:
        raise AssertionError("expected InvalidRequest")
    assert "j7" not in db.rows


async def test_outer_job_cap_through_taskgroup_leaves_the_real_row_terminal(monkeypatch):
    """Finding 5, end to end: the build's outer `wait_for` fires while the
    real `IntelService.run_inline` is stuck in the news fetch and the panel
    fetch is stuck too — the cancel crosses the TaskGroup and the
    enrichment's own `asyncio.timeout`, and the ai_runs row still ends
    `failed`, never `running`."""
    import app.report_worker as report_worker_module

    db = _AiRunsDB()
    fetching = asyncio.Event()

    async def _hanging_news(redis: Any, settings: Any) -> list[NewsItem]:
        fetching.set()
        await asyncio.sleep(3600)
        return []

    async def _hanging_panels(redis: Any, settings: Any, resolved: Any) -> Any:
        await asyncio.sleep(3600)

    _wire(monkeypatch, db, news=_hanging_news)
    monkeypatch.setattr(report_worker_module, "_fetch_panels", _hanging_panels)
    settings = _settings(report_pack_intel_budget_seconds=600)  # hard bound far beyond the outer cap

    try:
        await asyncio.wait_for(
            report_worker_module._quant_work(_FakeRedis(), settings, {}, {}, None), timeout=0.3,
        )
    except TimeoutError:
        pass
    else:
        raise AssertionError("expected the outer cap to fire")
    assert fetching.is_set()
    assert len(db.rows) == 1
    (row,) = db.rows.values()
    assert row["status"] == "failed" and row["phase"] == "cancelled", row
