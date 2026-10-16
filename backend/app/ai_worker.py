"""Off-API AI intel worker — `python -m app.ai_worker`.

Runs in the `ai-runner` compose service (--profile ai), the container built from
Dockerfile.ai (backend code + claude CLI + mounted creds). NEVER serves HTTP: jobs
flow in via the Redis queue, enriched rows out via `news_intel` — so enrichment
never blocks the FastAPI event loop. Reliable queue (LMOVE claim + boot recover())
mirrors the backtester exactly.

Phase 19: a job's `kind` decides which service handles it — `"cockpit"`
jobs (analyst cockpit turns) route to `CockpitService.execute`, everything
else keeps going through `IntelService.execute` (the enrich pipeline)
unchanged. `IntelService.recover()` is kind-agnostic (it only re-queues by
`job_id`/status) so it covers both without a cockpit-specific counterpart.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import uuid

import redis.exceptions as redis_exc

from app.config import get_settings
from app.db import dispose_engine, init_engine
from app.redis import dispose_redis, get_redis, init_redis
from app.services.cockpit import CockpitService
from app.services.intel_service import _PROCESSING, _QUEUE, IntelService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("catallax.ai_worker")


async def run_worker() -> None:
    settings = get_settings()
    init_engine()
    init_redis()
    redis = get_redis()
    svc = IntelService(redis, settings)
    cockpit_svc = CockpitService(redis, settings)
    worker_id = f"{os.uname().nodename}:{os.getpid()}:{uuid.uuid4().hex[:6]}"

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover
            pass

    await svc.recover()
    sem = asyncio.Semaphore(settings.ai_max_concurrency)
    logger.info("ai worker %s up (concurrency=%d)", worker_id, settings.ai_max_concurrency)

    idle = min(max(0.5, settings.ai_poll_timeout_seconds / 3.0), 2.0)
    inflight: set[asyncio.Task] = set()
    try:
        while not stop.is_set():
            await sem.acquire()
            if stop.is_set():
                sem.release()
                break
            try:
                job_id = await redis.lmove(_QUEUE, _PROCESSING, "RIGHT", "LEFT")
            except (redis_exc.RedisError, OSError) as exc:
                logger.warning("queue poll error (retrying): %s", exc)
                sem.release()
                await asyncio.sleep(idle)
                continue
            if not job_id:
                sem.release()
                await asyncio.sleep(idle)
                continue

            async def _run(jid: str) -> None:
                try:
                    try:
                        job = await svc.get(jid)
                    except Exception:  # noqa: BLE001 — job vanished between claim and lookup
                        logger.exception("ai worker could not look up job %s — dropping", jid)
                        await redis.lrem(_PROCESSING, 1, jid)
                        return
                    if job.kind == "cockpit":
                        await cockpit_svc.execute(jid, worker_id)
                    else:
                        await svc.execute(jid, worker_id)
                except Exception:  # noqa: BLE001
                    logger.exception("ai worker run crashed for %s", jid)
                finally:
                    sem.release()

            task = asyncio.create_task(_run(job_id))
            inflight.add(task)
            task.add_done_callback(inflight.discard)
    finally:
        logger.info("ai worker %s draining %d job(s)…", worker_id, len(inflight))
        if inflight:
            await asyncio.gather(*inflight, return_exceptions=True)
        await dispose_redis()
        await dispose_engine()
        logger.info("ai worker %s stopped", worker_id)


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
