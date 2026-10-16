"""Intel job lifecycle — the off-API enrich pipeline + persistence.

Mirrors BacktestService: redis + settings, own session per op, Redis reliable
queue + SSE progress. The worker path pulls RSS, enriches each article through
the F3 seam (in the ai-runner container where the claude provider lives), clusters,
and upserts `news_intel`. API path only enqueues + reads.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
import uuid
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from typing import Any

from pydantic import BaseModel
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.adapters.llm.factory import build_llm_adapter
from app.config import Settings
from app.db import session_scope
from app.errors import InvalidRequest, NotFound
from app.models import AiRun, NewsIntelRow
from app.services.intel import IntelEngine, cluster
from app.services.news import get_news

logger = logging.getLogger("catallax.intel_service")

_QUEUE = "catallax:ai:queue"
_PROCESSING = "catallax:ai:processing"
# Non-terminal ai_runs states — a row must never be left in one of these.
_UNFINISHED = ("queued", "running")


def _channel(job_id: str) -> str:
    return f"catallax:ai:progress:{job_id}"


class AiJob(BaseModel):
    job_id: str
    kind: str
    status: str
    progress: float
    phase: str | None
    created_at: str
    updated_at: str
    error: dict[str, Any] | None
    warnings: list[str]


class AiList(BaseModel):
    jobs: list[AiJob]


def _job(row: AiRun) -> AiJob:
    return AiJob(
        job_id=row.job_id, kind=row.kind, status=row.status, progress=float(row.progress),
        phase=row.phase, created_at=row.created_at.isoformat(), updated_at=row.updated_at.isoformat(),
        error=row.error, warnings=list(row.warnings or []),
    )


class IntelService:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings

    async def enqueue(self, kind: str = "enrich") -> AiJob:
        async with session_scope() as db:
            inflight = (await db.execute(
                select(func.count()).where(AiRun.status.in_(("queued", "running")))
            )).scalar_one()
        if inflight >= self._settings.ai_max_queued:
            raise InvalidRequest(f"too many AI jobs in flight ({inflight}); wait for some to finish")
        job_id = str(uuid.uuid4())
        async with session_scope() as db:
            row = AiRun(job_id=job_id, kind=kind, config={}, status="queued", progress=0.0, warnings=[])
            db.add(row)
            await db.commit()
            await db.refresh(row)
            job = _job(row)
        await self._redis.lpush(_QUEUE, job_id)
        logger.info("ai job enqueued %s (%s)", job_id, kind)
        return job

    async def get(self, job_id: str) -> AiJob:
        async with session_scope() as db:
            row = (await db.execute(select(AiRun).where(AiRun.job_id == job_id))).scalar_one_or_none()
        if row is None:
            raise NotFound("ai job not found")
        return _job(row)

    async def list(self, limit: int = 50) -> AiList:
        limit = max(1, min(int(limit), 200))
        async with session_scope() as db:
            rows = (await db.execute(
                select(AiRun).order_by(AiRun.created_at.desc()).limit(limit)
            )).scalars().all()
        return AiList(jobs=[_job(r) for r in rows])

    async def news(self, limit: int = 60) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 200))
        async with session_scope() as db:
            rows = (await db.execute(
                select(NewsIntelRow).order_by(NewsIntelRow.published.desc().nullslast()).limit(limit)
            )).scalars().all()
        return [_row_dict(r) for r in rows]

    async def events(self, limit: int = 200) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        async with session_scope() as db:
            rows = (await db.execute(
                select(NewsIntelRow).where(NewsIntelRow.lat.isnot(None), NewsIntelRow.lon.isnot(None))
                .order_by(NewsIntelRow.published.desc().nullslast()).limit(limit)
            )).scalars().all()
        return [_row_dict(r) for r in rows]

    # ── worker path ──
    async def recover(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=self._settings.ai_stale_seconds)
        while True:
            jid = await self._redis.rpop(_PROCESSING)
            if not jid:
                break
            async with session_scope() as db:
                row = (await db.execute(select(AiRun).where(AiRun.job_id == jid))).scalar_one_or_none()
                if row is None or row.status in ("done", "failed"):
                    continue
                if row.status == "running" and row.heartbeat_at is not None and row.heartbeat_at >= cutoff:
                    continue
                await db.execute(update(AiRun).where(AiRun.job_id == jid)
                                 .values(status="queued", worker_id=None, phase="requeued (recovered)"))
                await db.commit()
            await self._redis.lpush(_QUEUE, jid)
            logger.warning("ai recovered orphaned job %s", jid)
        async with session_scope() as db:
            stale = (await db.execute(select(AiRun).where(AiRun.status == "running"))).scalars().all()
            for row in stale:
                if row.heartbeat_at is None or row.heartbeat_at < cutoff:
                    await db.execute(update(AiRun).where(AiRun.job_id == row.job_id)
                                     .values(status="queued", worker_id=None, phase="requeued (stale)"))
                    await db.commit()
                    await self._redis.lpush(_QUEUE, row.job_id)

    async def run_inline(self, job_id: str, worker_id: str, *, deadline: float | None = None) -> None:
        """Create + run one enrich job in THIS process, bypassing the Redis
        queue — for callers with no queue consumer (the report-worker). The caller picks `job_id` so it can reference the row even
        when it cancels us. Guarantee: on ANY exit (return, error, cancel —
        including between the insert and `_execute`'s claim) the row ends
        `done` or `failed`, never `queued`/`running`; the cleanup write is a
        conditional update, so it never rewrites a committed `done`.
        Raises `InvalidRequest` (and creates no row) when too many AI jobs
        are already in flight."""
        async with session_scope() as db:
            inflight = (await db.execute(
                select(func.count()).where(AiRun.status.in_(_UNFINISHED))
            )).scalar_one()
        if inflight >= self._settings.ai_max_queued:
            raise InvalidRequest(f"too many AI jobs in flight ({inflight}); wait for some to finish")
        phase = "interrupted"
        try:
            async with session_scope() as db:
                db.add(AiRun(job_id=job_id, kind="enrich", config={}, status="queued",
                             progress=0.0, warnings=[]))
                await db.commit()
            await self._execute(job_id, worker_id, deadline)
            phase = ""
        except asyncio.CancelledError:
            phase = "cancelled"
            raise
        finally:
            if phase:
                with contextlib.suppress(Exception):
                    await asyncio.shield(self._fail_if_unfinished(job_id, phase))

    async def execute(self, job_id: str, worker_id: str, *, deadline: float | None = None) -> None:
        """Run one enrich job. `deadline` (a `time.monotonic()` instant) is an
        optional wall-clock budget: once passed, no further article is sent
        to the LLM and the real partial work is persisted with
        `result.truncated_at` set — the in-flight call still finishes, so the
        overshoot is bounded by one `claude_code_timeout_seconds`."""
        try:
            await self._execute(job_id, worker_id, deadline)
        finally:
            await self._redis.lrem(_PROCESSING, 1, job_id)

    async def _enrich_items(
        self, job_id: str, adapter: Any, engine: Any, items: list[Any],
        *, deadline: float | None, clock: Callable[[], float] = time.monotonic,
    ) -> tuple[list[dict[str, Any]], int, int | None]:
        """Enrich `items` in order until done or `deadline` passes. Returns
        (enriched rows, failed count, truncated_at) — `truncated_at` is the
        number of items enriched when the budget cut the run short, else None."""
        enriched: list[dict[str, Any]] = []
        failed = 0
        total = len(items)
        for i, item in enumerate(items):
            if deadline is not None and clock() >= deadline:
                logger.warning(
                    "ai job %s: enrich budget exhausted after %d/%d articles — persisting the partial run",
                    job_id, i, total,
                )
                return enriched, failed, i
            enr = await engine.enrich_item(adapter, item)
            if enr.grounding == "unverifiable" and enr.summary is None:
                failed += 1
            enriched.append({
                "link": item.link, "title": item.title, "source": item.source,
                "published": item.published, "topics": enr.topics,
                "sentiment": enr.sentiment, "sentiment_score": enr.sentiment_score,
                "country": enr.country, "lat": enr.lat, "lon": enr.lon,
                "summary": enr.summary, "grounding": enr.grounding,
            })
            if i % 5 == 0:
                await self._publish(job_id, 0.1 + 0.7 * (i / total), f"enriching {i+1}/{total}", persist=True)
        return enriched, failed, None

    async def _execute(self, job_id: str, worker_id: str, deadline: float | None = None) -> None:
        now = datetime.now(timezone.utc)
        async with session_scope() as db:
            res = await db.execute(
                update(AiRun).where(AiRun.job_id == job_id, AiRun.status == "queued")
                .values(status="running", worker_id=worker_id, heartbeat_at=now, progress=0.05, phase="claimed")
                .returning(AiRun.id)
            )
            claimed = res.scalar_one_or_none()
            await db.commit()
        if claimed is None:
            logger.info("ai job %s already claimed/gone — skipping", job_id)
            return

        hb = asyncio.create_task(self._heartbeat(job_id))
        try:
            await self._publish(job_id, 0.1, "fetching news", persist=True)
            items = await get_news(self._redis, self._settings)
            items = items[: self._settings.ai_enrich_max_articles]
            if not items:
                raise InvalidRequest("no news items to enrich")

            adapter = build_llm_adapter(self._settings.llm_provider, self._settings)
            engine = IntelEngine()
            enriched, failed, truncated_at = await self._enrich_items(
                job_id, adapter, engine, items, deadline=deadline,
            )

            clustered = cluster(enriched)
            await self._publish(job_id, 0.9, "persisting", persist=True)
            await self._upsert(clustered)

            result: dict[str, Any] = {"enriched": len(clustered), "failed": failed,
                                      "clusters": len({r["cluster_id"] for r in clustered})}
            if truncated_at is not None:
                result["truncated_at"] = truncated_at
                result["available"] = len(items)
            async with session_scope() as db:
                await db.execute(update(AiRun).where(AiRun.job_id == job_id).values(
                    status="done", progress=1.0, phase="done", result=result, error=None))
                await db.commit()
            await self._publish(job_id, 1.0, "done", status="done")
            logger.info("ai enrich done %s (%s)", job_id, result)
        except Exception as exc:  # noqa: BLE001 — persist + report, never crash the worker
            envelope = exc.envelope() if hasattr(exc, "envelope") else {
                "error": {"code": "INTERNAL_ERROR", "message": "ai enrich error"}}
            if not hasattr(exc, "envelope"):
                logger.exception("ai job %s failed", job_id)
            # Conditional: an error raised AFTER the `done` commit (e.g. while
            # publishing completion) must not rewrite a persisted success.
            if await self._fail_if_unfinished(job_id, "failed", envelope):
                await self._publish(job_id, 1.0, "failed", status="failed",
                                    error=envelope.get("error", {}).get("message"))
        except asyncio.CancelledError:
            # A caller's wall-clock cap (e.g. the report-worker's
            # asyncio.wait_for) cancelled us mid-run. Never leave the row
            # `running` forever: it counts against the in-flight cap and, on a
            # host with no ai-runner, nothing would ever recover it (seen in
            # practice). Shielded so the write survives the cancel;
            # conditional so a cancel landing after the `done` commit (during
            # the completion publish) leaves the success standing.
            with contextlib.suppress(Exception):
                await asyncio.shield(self._fail_if_unfinished(job_id, "cancelled"))
            raise
        finally:
            hb.cancel()

    async def _fail_if_unfinished(
        self, job_id: str, phase: str, envelope: dict[str, Any] | None = None,
    ) -> bool:
        """Mark the row `failed` ONLY if it is still queued/running — the one
        terminal write for every non-success exit. Returns whether a row was
        changed (False: already `done`/`failed`, or never inserted)."""
        if envelope is None:
            envelope = {"error": {"code": phase.upper(),
                                  "message": f"ai enrich {phase} before completing (caller's time budget)"}}
        async with session_scope() as db:
            res = await db.execute(
                update(AiRun).where(AiRun.job_id == job_id, AiRun.status.in_(_UNFINISHED))
                .values(status="failed", phase=phase, error=envelope)
                .returning(AiRun.id)
            )
            changed = res.scalar_one_or_none() is not None
            await db.commit()
        return changed

    async def _upsert(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        async with session_scope() as db:
            for r in rows:
                stmt = pg_insert(NewsIntelRow).values(
                    link=r["link"][:1024], title=r["title"][:512],
                    source=(r["source"][:128] if r["source"] else None),
                    published=r["published"], sentiment=r["sentiment"], sentiment_score=r["sentiment_score"],
                    topics=r["topics"], country=r["country"], lat=r["lat"], lon=r["lon"],
                    summary=r["summary"], cluster_id=r["cluster_id"], grounding=r["grounding"],
                    enriched_at=datetime.now(timezone.utc),
                ).on_conflict_do_update(
                    index_elements=[NewsIntelRow.link],
                    set_={
                        "sentiment": r["sentiment"], "sentiment_score": r["sentiment_score"],
                        "topics": r["topics"], "country": r["country"], "lat": r["lat"], "lon": r["lon"],
                        "summary": r["summary"], "cluster_id": r["cluster_id"], "grounding": r["grounding"],
                        "enriched_at": datetime.now(timezone.utc),
                    },
                )
                await db.execute(stmt)
            await db.commit()

    async def _heartbeat(self, job_id: str) -> None:
        try:
            while True:
                await asyncio.sleep(self._settings.ai_heartbeat_seconds)
                async with session_scope() as db:
                    await db.execute(update(AiRun).where(AiRun.job_id == job_id)
                                     .values(heartbeat_at=datetime.now(timezone.utc)))
                    await db.commit()
        except asyncio.CancelledError:
            pass

    async def _publish(self, job_id: str, frac: float, phase: str,
                       status: str = "running", error: str | None = None, persist: bool = False) -> None:
        msg = {"status": status, "progress": round(float(frac), 4), "phase": phase}
        if error:
            msg["error"] = error
        try:
            await self._redis.publish(_channel(job_id), json.dumps(msg))
        except Exception as exc:  # noqa: BLE001
            logger.debug("ai progress publish failed: %s", exc)
        if persist:
            try:
                async with session_scope() as db:
                    await db.execute(update(AiRun).where(AiRun.job_id == job_id)
                                     .values(progress=round(float(frac), 4), phase=phase))
                    await db.commit()
            except Exception as exc:  # noqa: BLE001
                logger.debug("ai progress persist failed: %s", exc)


def _row_dict(r: NewsIntelRow) -> dict[str, Any]:
    return {
        "link": r.link, "title": r.title, "source": r.source,
        "published": r.published.isoformat() if r.published else None,
        "sentiment": r.sentiment, "sentiment_score": r.sentiment_score,
        "topics": list(r.topics or []), "country": r.country, "lat": r.lat, "lon": r.lon,
        "summary": r.summary, "cluster_id": r.cluster_id, "grounding": r.grounding,
    }
