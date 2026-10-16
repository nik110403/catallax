import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.auth import DemoReadOnlyMiddleware, ShareGateMiddleware
from app.auth import router as auth_router
from app.config import get_settings
from app.db import dispose_engine, init_engine
from app.errors import register_exception_handlers
from app.redis import dispose_redis, get_redis, init_redis
from app.routers import (
    algo,
    backtests,
    cockpit,
    event_sources,
    events,
    forecasts,
    health,
    intel,
    journal,
    lab,
    layouts,
    ledger,
    macro,
    market_research,
    markets,
    networth,
    news,
    notes,
    portfolio,
    prices,
    reports,
    research,
    risk,
    session,
    signals,
    watchlist,
    wealthfolio,
    ws,
)


logger = logging.getLogger("catallax.main")


async def _breadth_autopersist_loop() -> None:
    """Persist the WSJ breadth diary on a schedule so the accumulated series
    (A/D line, McClellan) doesn't depend on someone opening the tab that day."""
    from app.services.market_research import MarketResearchEngine

    settings = get_settings()
    # Floor guards a .env typo from tight-looping against WSJ (diary cache is 4h).
    interval = max(settings.breadth_autofetch_interval_seconds, 600)
    while True:
        try:
            await MarketResearchEngine(get_redis(), settings).persist_breadth_snapshot()
        except Exception as exc:
            logger.warning("breadth autopersist failed: %s", exc)
        await asyncio.sleep(interval)


async def _advisor_weekly_tick_loop() -> None:
    """Weekly Portfolio Advisor report generation, gated by
    ADVISOR_ENABLED. Runs immediately on boot (before the first sleep) so a
    container down over an ISO-week boundary produces its catch-up report as
    soon as it comes back up, not after a full poll interval — the actual
    once-per-week cadence is enforced by `advisor_report.report_due`
    (mirrors `strategy_runner.rebalance_due`), not by this loop's interval."""
    from app.services.advisor_report import generate_weekly_report

    settings = get_settings()
    interval = max(settings.advisor_tick_interval_seconds, 300)
    while True:
        try:
            await generate_weekly_report(get_redis(), settings)
        except Exception as exc:  # noqa: BLE001 — one bad tick must not kill the loop
            logger.warning("advisor weekly tick failed: %s", exc)
        await asyncio.sleep(interval)


async def _ai_autoenrich_loop() -> None:
    """Enqueue an enrich run every ai_enrich_interval_seconds (0 disables). Fire-and-forget:
    the ai-runner worker does the work; this only drops a job so the feed self-refreshes."""
    settings = get_settings()
    interval = max(settings.ai_enrich_interval_seconds, 600)
    from app.services.intel_service import IntelService

    while True:
        await asyncio.sleep(interval)
        try:
            await IntelService(get_redis(), settings).enqueue("enrich")
        except Exception as exc:  # noqa: BLE001
            logger.warning("ai autoenrich enqueue failed: %s", exc)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    init_engine()
    init_redis()
    if get_settings().demo_sample:
        from app.demo_seed import verify_at_startup  # demo-only; fail closed to empty reads

        await verify_at_startup(get_settings())
    breadth_task: asyncio.Task[None] | None = None
    if get_settings().breadth_autofetch_interval_seconds > 0:
        breadth_task = asyncio.create_task(_breadth_autopersist_loop())
    ai_task: asyncio.Task[None] | None = None
    if get_settings().ai_enrich_interval_seconds > 0:
        ai_task = asyncio.create_task(_ai_autoenrich_loop())
    advisor_task: asyncio.Task[None] | None = None
    if get_settings().advisor_enabled:
        advisor_task = asyncio.create_task(_advisor_weekly_tick_loop())
    yield
    if breadth_task is not None:
        # Await the cancellation so the loop isn't mid-flight on redis/db
        # when the pools below are disposed.
        breadth_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await breadth_task
    if ai_task is not None:
        ai_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await ai_task
    if advisor_task is not None:
        advisor_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await advisor_task
    await dispose_redis()
    await dispose_engine()


def create_app() -> FastAPI:
    # Security review (2026-08-15): ShareGateMiddleware's scope is /api/v1
    # only, so FastAPI's schema endpoints (/docs, /redoc, /openapi.json) sit
    # outside it and are reachable without the passphrase even with the gate
    # on (verified 200 from a sibling container on the compose network).
    # Disable them outright once a passphrase is configured; keep today's
    # behaviour (enabled) when the gate is off.
    gated = bool(get_settings().share_passphrase.get_secret_value())
    app = FastAPI(
        title="Catallax API",
        lifespan=lifespan,
        docs_url=None if gated else "/docs",
        redoc_url=None if gated else "/redoc",
        openapi_url=None if gated else "/openapi.json",
    )
    register_exception_handlers(app)
    # Pure-ASGI (see app/auth.py docstring) — must wrap the whole app so the
    # WebSocket ticker scope reaches it too (and passes straight through).
    app.add_middleware(ShareGateMiddleware)
    # Added last = outermost: refuses writes before the share gate or routing.
    app.add_middleware(DemoReadOnlyMiddleware)
    for module in (
        health,
        prices,
        ledger,
        macro,
        market_research,
        markets,
        networth,
        news,
        events,
        event_sources,
        portfolio,
        research,
        risk,
        signals,
        backtests,
        lab,
        intel,
        wealthfolio,
        layouts,
        watchlist,
        ws,
        algo,
        session,
        reports,
        forecasts,
        journal,
        notes,
        cockpit,
    ):
        app.include_router(module.router, prefix="/api/v1")
    app.include_router(auth_router, prefix="/api/v1")
    return app


app = create_app()
