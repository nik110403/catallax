"""Off-API backtest worker — `python -m app.worker`.

Runs in the `backtester` compose service (--profile backtest), a separate container
built from the SAME ./backend image (numpy/pandas already present), on the default
network so it reaches Postgres + Redis. It NEVER serves HTTP: jobs flow in via the
Redis queue, results out via the `backtests` table — so a backtest can never block
the FastAPI event loop (acceptance: "backtests never block the main API").

Reliable queue: BRPOPLPUSH moves a job id from the queue to a processing list
(atomic claim that survives a crash); a boot-time `recover()` re-queues orphaned /
stale jobs. In-flight jobs are bounded by a Semaphore(BACKTEST_MAX_CONCURRENCY).
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
from app.services.backtest import _PROCESSING, _QUEUE, BacktestService

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("catallax.worker")


async def run_worker() -> None:
    settings = get_settings()
    init_engine()
    init_redis()
    redis = get_redis()
    svc = BacktestService(redis, settings)
    worker_id = f"{os.uname().nodename}:{os.getpid()}:{uuid.uuid4().hex[:6]}"

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover
            pass

    await svc.recover()
    sem = asyncio.Semaphore(settings.backtest_max_concurrency)
    logger.info("backtest worker %s up (concurrency=%d)", worker_id, settings.backtest_max_concurrency)

    idle = min(max(0.5, settings.backtest_poll_timeout_seconds / 3.0), 2.0)
    inflight: set[asyncio.Task] = set()
    try:
        while not stop.is_set():
            await sem.acquire()
            if stop.is_set():
                sem.release()
                break
            # Atomic non-blocking claim (RIGHT-pop the queue → LEFT-push the processing
            # list = FIFO reliable queue). Non-blocking sidesteps redis-py's blocking-pop
            # socket-read-timeout; a short idle sleep keeps pickup snappy without busy-loop.
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
                    logger.exception("worker run crashed for %s", jid)
                finally:
                    sem.release()

            task = asyncio.create_task(_run(job_id))
            inflight.add(task)
            task.add_done_callback(inflight.discard)
    finally:
        logger.info("backtest worker %s draining %d job(s)…", worker_id, len(inflight))
        if inflight:
            await asyncio.gather(*inflight, return_exceptions=True)
        await dispose_redis()
        await dispose_engine()
        logger.info("backtest worker %s stopped", worker_id)


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
