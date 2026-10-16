"""Backtester service (Phase 9, engine) — the off-API job lifecycle + pre-flight.

`BacktestService` is a SignalEngine sibling: it holds redis + settings, no
request-scoped DB session (each op opens its own `session_scope()`). It owns
pre-flight validation, the Redis queue + queued-row INSERT (the API path), and the
worker path: atomic claim → READ-ONLY F0 gather → pure-engine run in a thread →
persist the result + publish SSE progress.

F0 SAFETY: the worker reads `signals` (SELECT) + `daily_bars` (via HistoryStore,
which only ever idempotently upserts the shared backbone, exactly like the signal
grader / risk lens) and writes ONLY the `backtests` table — it has no write path
to `signals` and adds no new write path to `daily_bars`.
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import func, select, update

from app.adapters.backtest.base import BacktestConfig, BacktestReport
from app.adapters.backtest.factory import build_backtest_engine, enabled_engines, known_engines
from app.adapters.backtest.vectorized import _downsample
from app.adapters.envelope import Envelope
from app.adapters.factory import build_adapter
from app.config import Settings
from app.db import session_scope
from app.errors import DemoModeReadOnly, InvalidRequest, NotFound, RateLimitExceeded, UpstreamError
from app.models import Backtest, SignalRow
from app.services.history import HistoryStore
from app.services.signals import _bars, _rank_ic

logger = logging.getLogger("catallax.backtest")

_QUEUE = "catallax:bt:queue"
_PROCESSING = "catallax:bt:processing"


def _channel(job_id: str) -> str:
    return f"catallax:bt:progress:{job_id}"


# ── response models ─────────────────────────────────────────────
class BacktestJob(BaseModel):
    job_id: str
    name: str
    signal_id: str
    engine: str
    status: str
    progress: float
    phase: str | None
    created_at: str
    updated_at: str
    has_result: bool
    error: dict[str, Any] | None
    warnings: list[str]


class BacktestList(BaseModel):
    jobs: list[BacktestJob]


class PreflightFailed(InvalidRequest):
    """Pre-flight validation rejected the config (structured field errors)."""

    code = "PREFLIGHT_FAILED"

    def __init__(self, errors: list[dict[str, str]]) -> None:
        self.errors = errors
        super().__init__(
            "pre-flight validation failed: " + "; ".join(e.get("message", "") for e in errors)
        )

    def envelope(self) -> dict[str, Any]:  # type: ignore[override]
        env = super().envelope()
        env["error"]["details"] = self.errors
        return env


def _job(row: Backtest) -> BacktestJob:
    return BacktestJob(
        job_id=row.job_id, name=row.name, signal_id=row.signal_id, engine=row.engine,
        status=row.status, progress=float(row.progress), phase=row.phase,
        created_at=row.created_at.isoformat(), updated_at=row.updated_at.isoformat(),
        has_result=row.result is not None, error=row.error, warnings=list(row.warnings or []),
    )


class BacktestService:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings

    # ── pre-flight validation (structured errors BEFORE enqueue) ──
    async def validate(self, config: BacktestConfig) -> list[dict[str, str]]:
        s = self._settings
        errs: list[dict[str, str]] = []
        if config.engine not in known_engines():
            errs.append({"field": "engine", "code": "UNKNOWN_ENGINE",
                         "message": f"unknown engine '{config.engine}'"})
        elif config.engine not in enabled_engines():
            errs.append({"field": "engine", "code": "ENGINE_NOT_ENABLED",
                         "message": f"engine '{config.engine}' is a documented drop-in but not enabled in this build "
                                    f"(use: {', '.join(enabled_engines())})"})
        if (config.end - config.start).days > s.backtest_max_window_days:
            errs.append({"field": "window", "code": "WINDOW_TOO_LONG",
                         "message": f"window exceeds {s.backtest_max_window_days} days"})

        async with session_scope() as db:
            n_rows = (await db.execute(
                select(func.count()).where(SignalRow.signal_id == config.signal_id)
            )).scalar_one()
            if not n_rows:
                errs.append({"field": "signal_id", "code": "SIGNAL_NOT_FOUND",
                             "message": f"no signals logged for '{config.signal_id}'"})
                return errs  # nothing else is checkable without the signal
            dates_in = [r[0] for r in (await db.execute(
                select(SignalRow.as_of_date.distinct())
                .where(SignalRow.signal_id == config.signal_id,
                       SignalRow.as_of_date >= config.start,
                       SignalRow.as_of_date <= config.end)
            )).all()]
            symbols = {r[0] for r in (await db.execute(
                select(SignalRow.symbol.distinct()).where(SignalRow.signal_id == config.signal_id)
            )).all()}

        rebals = _downsample(sorted(dates_in), config.rebalance)
        if len(rebals) < s.backtest_min_rebalances:
            errs.append({"field": "window", "code": "TOO_FEW_REBALANCES",
                         "message": f"only {len(rebals)} rebalance date(s) for '{config.signal_id}' in the window "
                                    f"(need ≥ {s.backtest_min_rebalances}); widen the window or pick a finer rebalance"})
        if config.universe:
            missing = [u for u in config.universe if u not in symbols]
            if missing:
                errs.append({"field": "universe", "code": "SYMBOLS_NOT_IN_SIGNAL",
                             "message": f"not scored by '{config.signal_id}': {', '.join(missing[:8])}"})
        if config.universe and len(config.universe) > s.backtest_max_symbols:
            errs.append({"field": "universe", "code": "UNIVERSE_TOO_LARGE",
                         "message": f"universe exceeds {s.backtest_max_symbols} symbols"})
        return errs

    # ── enqueue (API path: validate → INSERT queued → LPUSH) ──
    async def enqueue(self, config: BacktestConfig) -> BacktestJob:
        if self._settings.demo_mode:
            raise DemoModeReadOnly()
        errs = await self.validate(config)
        if errs:
            raise PreflightFailed(errs)
        async with session_scope() as db:
            # Flood guard: bound the backlog so a POST loop can't grow the queue +
            # table unbounded (worker concurrency is capped, but the backlog isn't).
            inflight = (await db.execute(
                select(func.count()).where(Backtest.status.in_(("queued", "running")))
            )).scalar_one()
        if inflight >= self._settings.backtest_max_queued:
            raise InvalidRequest(
                f"too many backtests in flight ({inflight}); wait for some to finish"
            )
        job_id = str(uuid.uuid4())
        async with session_scope() as db:
            row = Backtest(
                job_id=job_id, name=config.name, engine=config.engine,
                signal_id=config.signal_id, config=config.model_dump(mode="json"),
                status="queued", progress=0.0, warnings=[],
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            job = _job(row)
        await self._redis.lpush(_QUEUE, job_id)
        logger.info("backtest enqueued %s (%s on %s)", job_id, config.name, config.signal_id)
        return job

    async def get(self, job_id: str) -> BacktestJob:
        # DEMO_MODE: shared detail-read chokepoint — also covers the /stream route's
        # existence pre-check and in-generator state read (both call this method).
        if self._settings.demo_empty_reads:
            raise NotFound("backtest not found")
        async with session_scope() as db:
            row = (await db.execute(select(Backtest).where(Backtest.job_id == job_id))).scalar_one_or_none()
        if row is None:
            raise NotFound("backtest not found")
        return _job(row)

    async def list(self, limit: int = 50) -> BacktestList:
        if self._settings.demo_empty_reads:
            return BacktestList(jobs=[])
        limit = max(1, min(int(limit), 200))
        async with session_scope() as db:
            rows = (await db.execute(
                select(Backtest).order_by(Backtest.created_at.desc()).limit(limit)
            )).scalars().all()
        return BacktestList(jobs=[_job(r) for r in rows])

    async def report(self, job_id: str) -> Envelope[BacktestReport]:
        if self._settings.demo_empty_reads:
            raise NotFound("backtest not found")
        async with session_scope() as db:
            row = (await db.execute(select(Backtest).where(Backtest.job_id == job_id))).scalar_one_or_none()
        if row is None:
            raise NotFound("backtest not found")
        if row.status != "done" or row.result is None:
            if row.status == "failed":
                msg = (row.error or {}).get("error", {}).get("message", "backtest failed")
                raise UpstreamError(f"backtest failed: {msg}")
            raise InvalidRequest(f"backtest not finished (status={row.status})")
        report = BacktestReport.model_validate(row.result)
        return Envelope.fresh(
            report, source=f"backtest:{row.engine}", cache_hit=False,
            stale=False, warnings=report.warnings,
        )

    # ── worker path ──────────────────────────────────────────
    async def recover(self) -> None:
        """Boot-time crash recovery: move any orphaned processing-list ids + any
        'running' row whose heartbeat is stale back to 'queued' so a dead worker
        never strands a job. Safe to run on every worker start."""
        # drain the processing list (a crashed worker left its in-flight ids there)
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self._settings.backtest_stale_seconds)
        while True:
            jid = await self._redis.rpop(_PROCESSING)
            if not jid:
                break
            async with session_scope() as db:
                row = (await db.execute(select(Backtest).where(Backtest.job_id == jid))).scalar_one_or_none()
                if row is None or row.status in ("done", "failed"):
                    continue
                # Don't requeue a job a LIVE worker is still running (fresh heartbeat) —
                # that would let a second worker double-run it under >1 replica. The
                # heartbeat-staleness DB sweep below is the backstop if it later dies.
                if row.status == "running" and row.heartbeat_at is not None and row.heartbeat_at >= cutoff:
                    continue
                await db.execute(update(Backtest).where(Backtest.job_id == jid)
                                 .values(status="queued", worker_id=None, phase="requeued (recovered)"))
                await db.commit()
                await self._redis.lpush(_QUEUE, jid)
                logger.warning("backtest recovered orphaned job %s", jid)
        # also sweep rows stuck 'running' with a stale heartbeat (lost processing entry)
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self._settings.backtest_stale_seconds)
        async with session_scope() as db:
            stale = (await db.execute(
                select(Backtest).where(Backtest.status == "running")
            )).scalars().all()
            for row in stale:
                if row.heartbeat_at is None or row.heartbeat_at < cutoff:
                    await db.execute(update(Backtest).where(Backtest.job_id == row.job_id)
                                     .values(status="queued", worker_id=None, phase="requeued (stale)"))
                    await db.commit()
                    await self._redis.lpush(_QUEUE, row.job_id)
                    logger.warning("backtest requeued stale running job %s", row.job_id)

    async def execute(self, job_id: str, worker_id: str) -> None:
        """Claim + run one job (worker). Always LREMs the processing entry at the end."""
        try:
            await self._execute(job_id, worker_id)
        finally:
            await self._redis.lrem(_PROCESSING, 1, job_id)

    async def _execute(self, job_id: str, worker_id: str) -> None:
        now = datetime.now(timezone.utc)
        # atomic claim: only the worker that flips queued→running proceeds
        async with session_scope() as db:
            res = await db.execute(
                update(Backtest)
                .where(Backtest.job_id == job_id, Backtest.status == "queued")
                .values(status="running", worker_id=worker_id, heartbeat_at=now,
                        progress=0.05, phase="claimed")
                .returning(Backtest.id)
            )
            claimed = res.scalar_one_or_none()
            await db.commit()
        if claimed is None:
            logger.info("backtest %s already claimed/gone — skipping", job_id)
            return

        hb = asyncio.create_task(self._heartbeat(job_id))
        try:
            async with session_scope() as db:
                row = (await db.execute(select(Backtest).where(Backtest.job_id == job_id))).scalar_one()
                cfg = BacktestConfig.model_validate(row.config)
            await self._publish(job_id, 0.1, "loading signals", persist=True)
            signal_rows, pairs = await self._load_signals(cfg)
            if not signal_rows:
                raise InvalidRequest("no signal rows in the window to backtest")

            await self._publish(job_id, 0.2, "fetching price history", persist=True)
            panels, bench_panel, stale_any, transient = await self._gather_panels(cfg, pairs)
            if transient:
                raise UpstreamError("price history rate-limited; retry the backtest shortly")
            if not panels:
                raise UpstreamError("no price history available for the universe")

            await self._publish(job_id, 0.5, "running engine", persist=True)
            engine = build_backtest_engine(cfg.engine, self._settings)
            loop = asyncio.get_running_loop()

            def _prog(frac: float, phase: str) -> None:
                loop.call_soon_threadsafe(
                    lambda: asyncio.ensure_future(self._publish(job_id, frac, phase))
                )

            try:
                report = await asyncio.to_thread(
                    engine.run, cfg, panels, signal_rows, bench_panel,
                    self._settings.signal_settle_days, _rank_ic, _prog,
                )
            except ValueError as exc:
                # Engine guard conditions (e.g. too few price-covered rebalances) are a
                # bad-spec problem, not an internal error — surface a clean 4xx envelope.
                raise InvalidRequest(f"backtest could not run: {exc}")
            if stale_any:
                report.warnings.append("some F0 price series served stale (upstream degraded; last-good bars used)")

            async with session_scope() as db:
                await db.execute(update(Backtest).where(Backtest.job_id == job_id).values(
                    status="done", progress=1.0, phase="done",
                    result=report.model_dump(mode="json"), warnings=report.warnings, error=None,
                ))
                await db.commit()
            await self._publish(job_id, 1.0, "done", status="done")
            logger.info("backtest done %s (cum %.2f%%, sharpe %s)", job_id,
                        report.metrics.cum_return_pct or 0, report.metrics.sharpe)
        except Exception as exc:  # noqa: BLE001 — persist + report, never crash the worker
            envelope = exc.envelope() if hasattr(exc, "envelope") else {
                "error": {"code": "INTERNAL_ERROR", "message": "backtest engine error"}
            }
            if not hasattr(exc, "envelope"):
                logger.exception("backtest %s failed", job_id)
            else:
                logger.warning("backtest %s failed: %s", job_id, exc)
            async with session_scope() as db:
                await db.execute(update(Backtest).where(Backtest.job_id == job_id).values(
                    status="failed", phase="failed", error=envelope,
                ))
                await db.commit()
            await self._publish(job_id, 1.0, "failed", status="failed",
                                error=envelope.get("error", {}).get("message"))
        finally:
            hb.cancel()

    async def _heartbeat(self, job_id: str) -> None:
        try:
            while True:
                await asyncio.sleep(self._settings.backtest_heartbeat_seconds)
                async with session_scope() as db:
                    await db.execute(update(Backtest).where(Backtest.job_id == job_id)
                                     .values(heartbeat_at=datetime.now(timezone.utc)))
                    await db.commit()
        except asyncio.CancelledError:
            pass

    async def _publish(self, job_id: str, frac: float, phase: str,
                       status: str = "running", error: str | None = None,
                       persist: bool = False) -> None:
        msg = {"status": status, "progress": round(float(frac), 4), "phase": phase}
        if error:
            msg["error"] = error
        try:
            await self._redis.publish(_channel(job_id), json.dumps(msg))
        except Exception as exc:  # noqa: BLE001
            logger.debug("backtest progress publish failed: %s", exc)
        if persist:
            try:
                async with session_scope() as db:
                    await db.execute(update(Backtest).where(Backtest.job_id == job_id)
                                     .values(progress=round(float(frac), 4), phase=phase))
                    await db.commit()
            except Exception as exc:  # noqa: BLE001
                logger.debug("backtest progress persist failed: %s", exc)

    async def _load_signals(self, cfg: BacktestConfig):
        uni = set(cfg.universe) if cfg.universe else None
        async with session_scope() as db:
            res = (await db.execute(
                select(SignalRow.as_of_date, SignalRow.symbol, SignalRow.source, SignalRow.value)
                .where(SignalRow.signal_id == cfg.signal_id,
                       SignalRow.as_of_date >= cfg.start, SignalRow.as_of_date <= cfg.end)
                .order_by(SignalRow.as_of_date, SignalRow.symbol)
            )).all()
        signal_rows: list[tuple[date, str, float]] = []
        pairs: set[tuple[str, str]] = set()
        for r in res:
            if uni and r.symbol not in uni:
                continue
            signal_rows.append((r.as_of_date, r.symbol, float(r.value)))
            pairs.add((r.symbol, r.source))
        return signal_rows, sorted(pairs)

    async def _gather_panels(self, cfg: BacktestConfig, pairs: list[tuple[str, str]]):
        if not pairs:
            return {}, None, False, False
        min_date = min(d for d, _, _ in await self._dates(cfg))
        start = datetime.combine(min_date - timedelta(days=45), datetime.min.time(), tzinfo=timezone.utc)
        end = datetime.now(timezone.utc)
        fetch = list(pairs)
        if cfg.benchmark_symbol:
            fetch = fetch + [(cfg.benchmark_symbol, self._settings.price_source_default)]
        series = await self._gather(sorted(set(fetch)), start, end)

        transient = any(v == "TRANSIENT" for v in series.values())
        stale_any = False
        panels: dict[str, tuple[list[date], Any]] = {}
        for (sym, _src), raw in series.items():
            if cfg.benchmark_symbol and sym == cfg.benchmark_symbol and sym not in {s for s, _ in pairs}:
                continue  # benchmark handled below
            parsed = _bars(raw)
            if parsed is None:
                continue
            dates, closes, stale = parsed
            stale_any = stale_any or stale
            panels[sym] = (dates, closes)

        bench_panel = None
        if cfg.benchmark_symbol:
            parsed = _bars(series.get((cfg.benchmark_symbol, self._settings.price_source_default)))
            if parsed is not None:
                bench_panel = (parsed[0], parsed[1])
        return panels, bench_panel, stale_any, transient

    async def _dates(self, cfg: BacktestConfig):
        async with session_scope() as db:
            res = (await db.execute(
                select(SignalRow.as_of_date, SignalRow.symbol, SignalRow.value)
                .where(SignalRow.signal_id == cfg.signal_id,
                       SignalRow.as_of_date >= cfg.start, SignalRow.as_of_date <= cfg.end)
            )).all()
        return [(r.as_of_date, r.symbol, r.value) for r in res]

    # ── F0 fan-out (own session per series; risk/signal precedent) ──
    async def _fetch_series(self, symbol: str, source: str, start: datetime, end: datetime):
        async with session_scope() as session:
            adapter = build_adapter(source, self._redis, self._settings)
            env = await HistoryStore(session, self._redis, self._settings).get_daily(adapter, symbol, start, end)
        return ([c.ts.date().isoformat() for c in env.data],
                [float(c.close) for c in env.data], bool(env.stale))

    async def _gather(self, pairs: list[tuple[str, str]], start: datetime, end: datetime):
        sem = asyncio.Semaphore(self._settings.backtest_fetch_concurrency)

        async def one(symbol: str, source: str):
            async with sem:
                try:
                    return (symbol, source), await self._fetch_series(symbol, source, start, end)
                except (RateLimitExceeded, UpstreamError) as exc:
                    logger.warning("backtest F0 TRANSIENT %s/%s: %s", source, symbol, exc)
                    return (symbol, source), "TRANSIENT"
                except Exception as exc:  # noqa: BLE001 — genuine no-data → uncovered
                    logger.info("backtest F0 fetch failed %s/%s: %s", source, symbol, exc)
                    return (symbol, source), None

        return dict(await asyncio.gather(*(one(s, src) for s, src in pairs)))
