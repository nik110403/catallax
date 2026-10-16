"""Off-API Quant/ML Lab worker — `python -m app.lab_worker`.

Runs in the `qlib-lab` compose service (--profile lab), a separate HEAVY image
(Dockerfile.lab = backend code + torch/xgboost/qlib) on the default network so it
reaches Postgres + Redis. It NEVER serves HTTP: factor/model jobs flow in via the
Redis queue, results out via the `lab_runs` table — so a slow model train can never
block the FastAPI event loop (acceptance: "daily stack unaffected when the profile
is off"; the API just enqueues + reads).

Structurally identical to `app.worker` (the backtester): reliable claim via
LMOVE queue→processing (survives a crash), a boot-time `recover()` re-queues
orphaned/stale jobs, in-flight jobs bounded by Semaphore(LAB_MAX_CONCURRENCY=1 —
training is heavy). The only differences are the queue keys + service class.
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
from app.services.lab import _PROCESSING, _QUEUE, LabService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("catallax.lab_worker")


async def run_worker() -> None:
    settings = get_settings()
    init_engine()
    init_redis()
    redis = get_redis()
    svc = LabService(redis, settings)
    worker_id = f"{os.uname().nodename}:{os.getpid()}:{uuid.uuid4().hex[:6]}"

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover
            pass

    await svc.recover()
    sem = asyncio.Semaphore(settings.lab_max_concurrency)
    logger.info("lab worker %s up (concurrency=%d)", worker_id, settings.lab_max_concurrency)

    idle = min(max(0.5, settings.lab_poll_timeout_seconds / 3.0), 2.0)
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
                    await svc.execute(jid, worker_id)
                except Exception:  # noqa: BLE001 — execute persists failures; never die here
                    logger.exception("lab worker run crashed for %s", jid)
                finally:
                    sem.release()

            task = asyncio.create_task(_run(job_id))
            inflight.add(task)
            task.add_done_callback(inflight.discard)
    finally:
        logger.info("lab worker %s draining %d job(s)…", worker_id, len(inflight))
        if inflight:
            await asyncio.gather(*inflight, return_exceptions=True)
        await dispose_redis()
        await dispose_engine()
        logger.info("lab worker %s stopped", worker_id)


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
