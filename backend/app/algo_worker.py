"""Continuous paper-trading algo engine loop — `python -m app.algo_worker`.

Runs in the `trading-engine` compose service (--profile algo), the ONLY writer
of order fills/positions/snapshots (see the single-worker assumption in the
`AlgoService` module docstring). NOT a queue consumer like `ai_worker`/`worker`
— a TICK LOOP: every `ALGO_TICK_SECONDS` (or woken early by a `_NUDGE` pub/sub
message published from `submit_order`, so a fresh order feels near-instant),
heartbeat, check the kill switch, resolve any 'new' orders, sweep resting
limits, snapshot the account, then run any due strategy rebalances. Every step
is wrapped in its own try/except — a single failing step logs and the loop
moves on; the worker itself must never crash.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal
import uuid

from app.config import get_settings
from app.db import dispose_engine, init_engine
from app.redis import dispose_redis, get_redis, init_redis
from app.services.algo import AlgoService, _NUDGE
from app.services.strategy_runner import StrategyRunner

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
logger = logging.getLogger("catallax.algo_worker")


async def run_worker() -> None:
    settings = get_settings()
    init_engine()
    init_redis()
    redis = get_redis()
    svc = AlgoService(redis, settings)
    runner = StrategyRunner(redis, settings)
    worker_id = f"{os.uname().nodename}:{os.getpid()}:{uuid.uuid4().hex[:6]}"

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # pragma: no cover
            pass

    pubsub = redis.pubsub()
    await pubsub.subscribe(_NUDGE)

    tick = settings.algo_tick_seconds
    logger.info("algo worker %s up (tick=%ds, broker=%s)", worker_id, tick, settings.algo_broker)

    try:
        while not stop.is_set():
            try:
                await svc.heartbeat(worker_id)
            except Exception:  # noqa: BLE001
                logger.exception("algo worker %s: heartbeat failed", worker_id)

            try:
                killed = await svc.kill_state()
            except Exception:  # noqa: BLE001
                # Fail CLOSED: a kill-switch check failure must not let trading proceed.
                logger.exception("algo worker %s: kill_state check failed", worker_id)
                killed = True

            if not killed:
                try:
                    for order_id in await svc.claim_new_orders(worker_id):
                        await svc.execute_order(order_id)  # never raises out
                except Exception:  # noqa: BLE001
                    logger.exception("algo worker %s: claim/execute step failed", worker_id)

                try:
                    await svc.check_resting_limits()
                except Exception:  # noqa: BLE001
                    logger.exception("algo worker %s: check_resting_limits failed", worker_id)

                try:
                    await svc.mark_and_snapshot()
                except Exception:  # noqa: BLE001
                    logger.exception("algo worker %s: mark_and_snapshot failed", worker_id)

                try:
                    await runner.run_due(svc)
                except Exception:  # noqa: BLE001
                    logger.exception("algo worker %s: run_due failed", worker_id)
            # kill_state on: still heartbeats every tick above, just skips order
            # resolution/rebalancing — "idle but alive".

            if stop.is_set():
                break
            try:
                # Sleeps for `tick` seconds UNLESS a `_NUDGE` arrives first (a
                # fresh order submitted via the API), in which case the loop
                # wakes immediately for the next tick.
                await pubsub.get_message(ignore_subscribe_messages=True, timeout=max(tick, 1))
            except Exception:  # noqa: BLE001 — a Redis hiccup on the wake channel must not crash the loop
                logger.exception("algo worker %s: nudge poll failed; falling back to plain sleep", worker_id)
                await asyncio.sleep(tick)
    finally:
        logger.info("algo worker %s shutting down…", worker_id)
        try:
            await pubsub.unsubscribe(_NUDGE)
            await pubsub.aclose()
        except Exception:  # noqa: BLE001
            logger.exception("algo worker %s: pubsub cleanup failed", worker_id)
        await dispose_redis()
        await dispose_engine()
        logger.info("algo worker %s stopped", worker_id)


def main() -> None:
    asyncio.run(run_worker())


if __name__ == "__main__":
    main()
