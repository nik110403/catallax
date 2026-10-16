"""Quant/ML Lab service — the off-API job lifecycle + pre-flight.

`LabService` is a `BacktestService` sibling: redis + settings, no request-scoped DB
session (each op opens its own `session_scope()`). It owns pre-flight validation,
the Redis queue + queued-row INSERT (the API path), and the worker path: atomic
claim → READ-ONLY F0 gather → pure compute (factor study) or model train/eval in a
thread → persist the result + publish SSE progress. Two job kinds share the table:
`factor` (a cross-sectional IC study) and `model` (a return-prediction train/eval).

F0 SAFETY: the worker reads `daily_bars` (via HistoryStore, which only idempotently
upserts the shared backbone exactly like the signal grader / risk lens / backtester)
and writes ONLY the `lab_runs` table — no write path to F0.

The heavy ML imports (sklearn/torch via the engine factory; pandas feature builder)
happen in the worker thread inside the `qlib-lab` image; this module imports only
the import-light `adapters/lab/base` + `factory` so the MAIN backend can enqueue/
read jobs without the heavy stack present.
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

from app.adapters.envelope import Envelope
from app.adapters.factory import build_adapter
from app.adapters.lab.base import (
    FactorConfig,
    FactorReport,
    ModelConfig,
    ModelReport,
)
from app.adapters.lab.factory import build_lab_engine, known_engines
from app.config import Settings
from app.db import session_scope
from app.errors import DemoModeReadOnly, InvalidRequest, NotFound, RateLimitExceeded, UpstreamError
from app.models import LabRun
from app.services.history import HistoryStore

logger = logging.getLogger("catallax.lab")

_QUEUE = "catallax:lab:queue"
_PROCESSING = "catallax:lab:processing"


def _channel(job_id: str) -> str:
    return f"catallax:lab:progress:{job_id}"


# ── response models ─────────────────────────────────────────────
class LabJob(BaseModel):
    job_id: str
    kind: str
    name: str
    engine: str
    status: str
    progress: float
    phase: str | None
    created_at: str
    updated_at: str
    has_result: bool
    error: dict[str, Any] | None
    warnings: list[str]


class LabList(BaseModel):
    jobs: list[LabJob]


def _job(row: LabRun) -> LabJob:
    return LabJob(
        job_id=row.job_id, kind=row.kind, name=row.name, engine=row.engine,
        status=row.status, progress=float(row.progress), phase=row.phase,
        created_at=row.created_at.isoformat(), updated_at=row.updated_at.isoformat(),
        has_result=row.result is not None, error=row.error, warnings=list(row.warnings or []),
    )


class LabService:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings

    # ── universe resolution ──────────────────────────────────
    def _resolve_universe(self, explicit: list[str] | None) -> list[str]:
        if explicit:
            return explicit[: self._settings.lab_max_symbols]
        return [sym for sym, _ in self._settings.lab_universe_map][: self._settings.lab_max_symbols]

    # ── pre-flight (structured errors BEFORE enqueue) ─────────
    def _validate_universe(self, universe: list[str] | None) -> list[dict[str, str]]:
        errs: list[dict[str, str]] = []
        if universe and len(universe) > self._settings.lab_max_symbols:
            errs.append({"field": "universe", "code": "UNIVERSE_TOO_LARGE",
                         "message": f"universe exceeds {self._settings.lab_max_symbols} symbols"})
        return errs

    async def _flood_guard(self) -> None:
        async with session_scope() as db:
            inflight = (await db.execute(
                select(func.count()).where(LabRun.status.in_(("queued", "running")))
            )).scalar_one()
        if inflight >= self._settings.lab_max_queued:
            raise InvalidRequest(f"too many lab runs in flight ({inflight}); wait for some to finish")

    async def enqueue_factor(self, config: FactorConfig) -> LabJob:
        if self._settings.demo_mode:
            raise DemoModeReadOnly()
        errs = self._validate_universe(config.universe)
        if errs:
            raise InvalidRequest("; ".join(e["message"] for e in errs))
        await self._flood_guard()
        return await self._insert("factor", "factor-study", config)

    async def enqueue_model(self, config: ModelConfig) -> LabJob:
        if self._settings.demo_mode:
            raise DemoModeReadOnly()
        errs = self._validate_universe(config.universe)
        if config.engine not in known_engines():
            errs.append({"field": "engine", "code": "UNKNOWN_ENGINE",
                         "message": f"unknown engine '{config.engine}' (have: {', '.join(known_engines())})"})
        if errs:
            raise InvalidRequest("; ".join(e["message"] for e in errs))
        await self._flood_guard()
        return await self._insert("model", config.engine, config)

    async def _insert(self, kind: str, engine: str, config: BaseModel) -> LabJob:
        job_id = str(uuid.uuid4())
        async with session_scope() as db:
            row = LabRun(
                job_id=job_id, kind=kind, name=config.name, engine=engine,  # type: ignore[attr-defined]
                config=config.model_dump(mode="json"), status="queued", progress=0.0, warnings=[],
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            job = _job(row)
        await self._redis.lpush(_QUEUE, job_id)
        logger.info("lab %s enqueued %s (%s)", kind, job_id, config.name)  # type: ignore[attr-defined]
        return job

    async def get(self, job_id: str) -> LabJob:
        # DEMO_MODE: shared detail-read chokepoint — also covers the /stream route's
        # existence pre-check and in-generator state read (both call this method).
        if self._settings.demo_mode:
            raise NotFound("lab run not found")
        async with session_scope() as db:
            row = (await db.execute(select(LabRun).where(LabRun.job_id == job_id))).scalar_one_or_none()
        if row is None:
            raise NotFound("lab run not found")
        return _job(row)

    async def list(self, limit: int = 50) -> LabList:
        if self._settings.demo_mode:
            return LabList(jobs=[])
        limit = max(1, min(int(limit), 200))
        async with session_scope() as db:
            rows = (await db.execute(
                select(LabRun).order_by(LabRun.created_at.desc()).limit(limit)
            )).scalars().all()
        return LabList(jobs=[_job(r) for r in rows])

    async def report(self, job_id: str) -> Envelope[Any]:
        if self._settings.demo_mode:
            raise NotFound("lab run not found")
        async with session_scope() as db:
            row = (await db.execute(select(LabRun).where(LabRun.job_id == job_id))).scalar_one_or_none()
        if row is None:
            raise NotFound("lab run not found")
        if row.status != "done" or row.result is None:
            if row.status == "failed":
                msg = (row.error or {}).get("error", {}).get("message", "lab run failed")
                raise UpstreamError(f"lab run failed: {msg}")
            raise InvalidRequest(f"lab run not finished (status={row.status})")
        model = FactorReport if row.kind == "factor" else ModelReport
        report = model.model_validate(row.result)
        return Envelope.fresh(
            report, source=f"lab:{row.kind}:{row.engine}", cache_hit=False,
            stale=False, warnings=list(report.warnings),
        )

    # ── worker path ──────────────────────────────────────────
    async def recover(self) -> None:
        """Boot-time crash recovery (mirror of BacktestService.recover)."""
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self._settings.lab_stale_seconds)
        while True:
            jid = await self._redis.rpop(_PROCESSING)
            if not jid:
                break
            async with session_scope() as db:
                row = (await db.execute(select(LabRun).where(LabRun.job_id == jid))).scalar_one_or_none()
                if row is None or row.status in ("done", "failed"):
                    continue
                if row.status == "running" and row.heartbeat_at is not None and row.heartbeat_at >= cutoff:
                    continue
                await db.execute(update(LabRun).where(LabRun.job_id == jid)
                                 .values(status="queued", worker_id=None, phase="requeued (recovered)"))
                await db.commit()
                await self._redis.lpush(_QUEUE, jid)
                logger.warning("lab recovered orphaned job %s", jid)
        # also sweep rows stuck 'running' with a stale heartbeat (lost processing entry);
        # reuse the same cutoff computed above.
        async with session_scope() as db:
            stale = (await db.execute(select(LabRun).where(LabRun.status == "running"))).scalars().all()
            for row in stale:
                if row.heartbeat_at is None or row.heartbeat_at < cutoff:
                    await db.execute(update(LabRun).where(LabRun.job_id == row.job_id)
                                     .values(status="queued", worker_id=None, phase="requeued (stale)"))
                    await db.commit()
                    await self._redis.lpush(_QUEUE, row.job_id)
                    logger.warning("lab requeued stale running job %s", row.job_id)

    async def execute(self, job_id: str, worker_id: str) -> None:
        try:
            await self._execute(job_id, worker_id)
        finally:
            await self._redis.lrem(_PROCESSING, 1, job_id)

    async def _execute(self, job_id: str, worker_id: str) -> None:
        now = datetime.now(timezone.utc)
        async with session_scope() as db:
            res = await db.execute(
                update(LabRun)
                .where(LabRun.job_id == job_id, LabRun.status == "queued")
                .values(status="running", worker_id=worker_id, heartbeat_at=now,
                        progress=0.05, phase="claimed")
                .returning(LabRun.id)
            )
            claimed = res.scalar_one_or_none()
            await db.commit()
        if claimed is None:
            logger.info("lab %s already claimed/gone — skipping", job_id)
            return

        hb = asyncio.create_task(self._heartbeat(job_id))
        try:
            async with session_scope() as db:
                row = (await db.execute(select(LabRun).where(LabRun.job_id == job_id))).scalar_one()
                kind, cfg_raw = row.kind, row.config

            if kind == "factor":
                cfg = FactorConfig.model_validate(cfg_raw)
                report = await self._run_factor(job_id, cfg)
            else:
                cfg = ModelConfig.model_validate(cfg_raw)
                report = await self._run_model(job_id, cfg)

            async with session_scope() as db:
                await db.execute(update(LabRun).where(LabRun.job_id == job_id).values(
                    status="done", progress=1.0, phase="done",
                    result=report.model_dump(mode="json"), warnings=list(report.warnings), error=None,
                ))
                await db.commit()
            await self._publish(job_id, 1.0, "done", status="done")
            logger.info("lab done %s (%s)", job_id, kind)
        except Exception as exc:  # noqa: BLE001 — persist + report, never crash the worker
            envelope = exc.envelope() if hasattr(exc, "envelope") else {
                "error": {"code": "INTERNAL_ERROR", "message": "lab run error"}
            }
            if not hasattr(exc, "envelope"):
                logger.exception("lab %s failed", job_id)
            else:
                logger.warning("lab %s failed: %s", job_id, exc)
            async with session_scope() as db:
                await db.execute(update(LabRun).where(LabRun.job_id == job_id).values(
                    status="failed", phase="failed", error=envelope,
                ))
                await db.commit()
            await self._publish(job_id, 1.0, "failed", status="failed",
                                error=envelope.get("error", {}).get("message"))
        finally:
            hb.cancel()

    # ── job kinds ────────────────────────────────────────────
    async def _run_factor(self, job_id: str, cfg: FactorConfig) -> FactorReport:
        from app.adapters.lab.factors import run_factor_study  # heavy-ish (pandas) — worker only

        await self._publish(job_id, 0.15, "fetching price history", persist=True)
        panel, _feats, transient, stale_any = await self._build_panel(cfg.universe, cfg.horizon_days,
                                                                      cfg.start, cfg.end)
        if transient and panel.empty:
            raise UpstreamError("price history rate-limited; retry the lab run shortly")
        if panel.empty:
            raise InvalidRequest("no usable cross-sectional history for the universe/window")

        await self._publish(job_id, 0.5, "running factor study", persist=True)
        loop = asyncio.get_running_loop()

        def _prog(frac: float, phase: str) -> None:
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._publish(job_id, frac, phase)))

        report = await asyncio.to_thread(run_factor_study, cfg, panel, _prog)
        if stale_any:
            report.warnings.append("some F0 price series served stale (upstream degraded)")
        return report

    async def _run_model(self, job_id: str, cfg: ModelConfig) -> ModelReport:
        await self._publish(job_id, 0.15, "fetching price history", persist=True)
        panel, feats, transient, stale_any = await self._build_panel(cfg.universe, cfg.horizon_days,
                                                                     cfg.start, cfg.end)
        if transient and panel.empty:
            raise UpstreamError("price history rate-limited; retry the lab run shortly")
        if len(panel) < self._settings.lab_min_train_rows:
            raise InvalidRequest(
                f"only {len(panel)} training rows for the universe/window "
                f"(need ≥ {self._settings.lab_min_train_rows}); widen the universe or window"
            )

        await self._publish(job_id, 0.5, "training model", persist=True)
        engine = build_lab_engine(cfg.engine, self._settings)  # raises if unavailable
        loop = asyncio.get_running_loop()

        def _prog(frac: float, phase: str) -> None:
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._publish(job_id, frac, phase)))

        report = await asyncio.to_thread(engine.train_eval, cfg, panel, feats, _prog)
        if stale_any:
            report.warnings.append("some F0 price series served stale (upstream degraded)")
        return report

    # ── F0 panel build (READ-ONLY backbone) ──────────────────
    async def _build_panel(self, universe: list[str] | None, horizon_days: int,
                           start: date | None, end: date | None):
        """Gather F0 daily bars for the universe → tidy cross-sectional feature panel.

        Returns (panel, feature_cols, transient, stale_any)."""
        import pandas as pd  # worker only

        from app.adapters.lab.features import build_feature_panel

        symbols = self._resolve_universe(universe)
        source = self._settings.price_source_default
        hist_start = datetime.now(timezone.utc) - timedelta(days=self._settings.lab_history_lookback_days)
        hist_end = datetime.now(timezone.utc)

        series = await self._gather([(s, source) for s in symbols], hist_start, hist_end)
        transient = any(v == "TRANSIENT" for v in series.values())
        stale_any = False
        panels: dict[str, pd.DataFrame] = {}
        for (sym, _src), raw in series.items():
            if not isinstance(raw, (tuple, list)) or len(raw) < 3 or not raw[0]:
                continue
            dates, closes, volumes, stale = raw
            stale_any = stale_any or bool(stale)
            idx = pd.to_datetime(pd.Index(dates))
            df = pd.DataFrame({"close": closes, "volume": volumes}, index=idx).sort_index()
            df = df[~df.index.duplicated(keep="last")]
            panels[sym] = df

        panel, feature_cols = await asyncio.to_thread(build_feature_panel, panels, horizon_days)
        # optional date-window filter on decision dates
        if not panel.empty and (start or end):
            if start:
                panel = panel[panel["date"] >= pd.Timestamp(start)]
            if end:
                panel = panel[panel["date"] <= pd.Timestamp(end)]
            panel = panel.reset_index(drop=True)
        return panel, feature_cols, transient, stale_any

    async def _gather(self, pairs: list[tuple[str, str]], start: datetime, end: datetime):
        sem = asyncio.Semaphore(self._settings.lab_fetch_concurrency)

        async def one(symbol: str, source: str):
            async with sem:
                try:
                    return (symbol, source), await self._fetch_series(symbol, source, start, end)
                except (RateLimitExceeded, UpstreamError) as exc:
                    logger.warning("lab F0 TRANSIENT %s/%s: %s", source, symbol, exc)
                    return (symbol, source), "TRANSIENT"
                except Exception as exc:  # noqa: BLE001 — genuine no-data → uncovered
                    logger.info("lab F0 fetch failed %s/%s: %s", source, symbol, exc)
                    return (symbol, source), None

        return dict(await asyncio.gather(*(one(s, src) for s, src in pairs)))

    async def _fetch_series(self, symbol: str, source: str, start: datetime, end: datetime):
        async with session_scope() as session:
            adapter = build_adapter(source, self._redis, self._settings)
            env = await HistoryStore(session, self._redis, self._settings).get_daily(adapter, symbol, start, end)
        return (
            [c.ts.date().isoformat() for c in env.data],
            [float(c.close) for c in env.data],
            [float(c.volume) if c.volume is not None else float("nan") for c in env.data],
            bool(env.stale),
        )

    async def _heartbeat(self, job_id: str) -> None:
        try:
            while True:
                await asyncio.sleep(self._settings.lab_heartbeat_seconds)
                async with session_scope() as db:
                    await db.execute(update(LabRun).where(LabRun.job_id == job_id)
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
            logger.debug("lab progress publish failed: %s", exc)
        if persist:
            try:
                async with session_scope() as db:
                    await db.execute(update(LabRun).where(LabRun.job_id == job_id)
                                     .values(progress=round(float(frac), 4), phase=phase))
                    await db.commit()
            except Exception as exc:  # noqa: BLE001
                logger.debug("lab progress persist failed: %s", exc)
