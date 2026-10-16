"""SQLAlchemy ORM models.

Mirror the schema produced by the Alembic migrations in
`backend/alembic/versions/`. The migrations remain the source of truth for
DDL; these models are the typed query surface used by services/routers.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from datetime import date as date_type

from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Double,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Holding(Base):
    __tablename__ = "holdings"
    __table_args__ = (
        UniqueConstraint("symbol", "account", name="uq_holdings_symbol_account"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    account: Mapped[str] = mapped_column(String(128), nullable=False)
    cost_basis: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), onupdate=func.now()
    )


class WatchlistItem(Base):
    __tablename__ = "watchlist"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    label: Mapped[str | None] = mapped_column(String(128))
    # Which price adapter feeds this symbol (e.g. "yfinance", "coingecko").
    source: Mapped[str | None] = mapped_column(String(32))
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PriceCacheRow(Base):
    __tablename__ = "price_cache"
    __table_args__ = (
        UniqueConstraint(
            "symbol", "source", "as_of", name="uq_price_cache_symbol_source_as_of"
        ),
        # Mirror migration 0002's composite index so Base.metadata fully
        # describes the schema (if autogenerate is ever enabled).
        Index("ix_price_cache_symbol_source", "symbol", "source"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    asset_type: Mapped[str | None] = mapped_column(String(16))
    price: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)
    as_of: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class DailyBarRow(Base):
    """One daily OHLC bar per (symbol, source, bar_date).

    The history backbone (Foundations F0). Unlike `price_cache` (a quote log),
    this holds clean daily bars that risk/backtest/ML/signal code reads back
    from Postgres via `services.history.HistoryStore`, backfilled idempotently
    from a `PriceSourceAdapter.get_history` call.
    """

    __tablename__ = "daily_bars"
    __table_args__ = (
        UniqueConstraint(
            "symbol", "source", "bar_date", name="uq_daily_bars_symbol_source_date"
        ),
        Index("ix_daily_bars_symbol_source_date", "symbol", "source", "bar_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    asset_type: Mapped[str | None] = mapped_column(String(16))
    bar_date: Mapped[date_type] = mapped_column(Date, nullable=False)
    open: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    volume: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    currency: Mapped[str | None] = mapped_column(String(8))
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SignalRow(Base):
    """One emitted cross-sectional score (Phase 9 — the signal self-scoring core).

    A row is "at decision date `as_of_date` (T), `symbol` scored `value`" within the
    `signal_id` family (higher value = higher expected forward return, sign-normalised
    at emission). Grades are never persisted — `services.signals.SignalEngine` recomputes
    them lazily from the F0 `daily_bars` backbone (forward return of a matured, settled
    window), so a derived number can't go stale against a restated bar. The unique
    (signal_id, as_of_date, symbol) key makes re-emitting a cross-section idempotent.
    """

    __tablename__ = "signals"
    __table_args__ = (
        UniqueConstraint(
            "signal_id", "as_of_date", "symbol", name="uq_signals_id_date_symbol"
        ),
        Index("ix_signals_id_date", "signal_id", "as_of_date"),
        Index("ix_signals_symbol_source", "symbol", "source"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    signal_id: Mapped[str] = mapped_column(String(64), nullable=False)
    as_of_date: Mapped[date_type] = mapped_column(Date, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    value: Mapped[float] = mapped_column(Double, nullable=False)
    meta: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Backtest(Base):
    """A backtester job AND its result (1:1) — Phase 9 (engine).

    The FastAPI process only INSERTs a row (status='queued') + SELECTs; the off-API
    `python -m app.worker` claims a queued row (atomic UPDATE … WHERE status='queued'),
    runs the engine, and writes `result`/`status` back. The worker is structurally
    READ-ONLY on the F0 `daily_bars`/`signals` backbone and writes ONLY this table.
    `job_id` is a server-minted UUID — the public route handle + Redis progress channel.
    """

    __tablename__ = "backtests"
    __table_args__ = (
        UniqueConstraint("job_id", name="uq_backtests_job_id"),
        Index("ix_backtests_status", "status"),
        Index("ix_backtests_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(String(36), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    engine: Mapped[str] = mapped_column(String(32), nullable=False)
    signal_id: Mapped[str] = mapped_column(String(64), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="queued"
    )
    progress: Mapped[float] = mapped_column(Double, nullable=False, server_default="0")
    phase: Mapped[str | None] = mapped_column(String(48))
    worker_id: Mapped[str | None] = mapped_column(String(64))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    warnings: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class LayoutRow(Base):
    """A key→JSON state blob backing the dynamic-UI platform (Foundations F4).

    The frontend persists each UI state bucket here — `workspaces` and
    `presets:<namespace>` — so a user's tabs + layout presets follow them across
    devices instead of living only in one browser's localStorage. Single-user
    app; keys are allowlisted and payloads size-capped in `routers/layouts.py`.
    """

    __tablename__ = "layouts"
    __table_args__ = (UniqueConstraint("key", name="uq_layouts_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    key: Mapped[str] = mapped_column(String(128), nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class MarketBreadthRow(Base):
    """Daily market-breadth snapshot per exchange (Phase 10a v1.2).

    Mirrors migration 0007. One row per (breadth_date, market) from the WSJ
    markets-diary feed, upserted on read. Breadth oscillators (A/D line,
    McClellan) are built forward from this accumulated series.
    """

    __tablename__ = "market_breadth"
    __table_args__ = (
        UniqueConstraint(
            "breadth_date", "market", name="uq_market_breadth_date_market"
        ),
        Index("ix_market_breadth_market_date", "market", "breadth_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    breadth_date: Mapped[date_type] = mapped_column(Date, nullable=False)
    market: Mapped[str] = mapped_column(String(24), nullable=False)
    issues: Mapped[int | None] = mapped_column(Integer)
    advances: Mapped[int | None] = mapped_column(Integer)
    declines: Mapped[int | None] = mapped_column(Integer)
    unchanged: Mapped[int | None] = mapped_column(Integer)
    new_highs: Mapped[int | None] = mapped_column(Integer)
    new_lows: Mapped[int | None] = mapped_column(Integer)
    adv_volume: Mapped[int | None] = mapped_column(BigInteger)
    decl_volume: Mapped[int | None] = mapped_column(BigInteger)
    total_volume: Mapped[int | None] = mapped_column(BigInteger)
    trin: Mapped[float | None] = mapped_column(Double)
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class LabRun(Base):
    """A Quant/ML Lab run AND its result (1:1) — Phase 11.

    Mirrors `Backtest`. The FastAPI process only INSERTs a row
    (status='queued') + SELECTs; the off-API `python -m app.lab_worker` (the heavy
    `qlib-lab` container, --profile lab) claims a queued row (atomic UPDATE …
    WHERE status='queued'), runs the factor study / model train-eval, and writes
    `result`/`status` back. The worker is structurally READ-ONLY on the F0
    `daily_bars`/`signals` backbone and writes ONLY this table. `job_id` is a
    server-minted UUID — the public route handle + Redis progress channel.
    `kind` ∈ {factor, model}; `engine` is the factory key.
    """

    __tablename__ = "lab_runs"
    __table_args__ = (
        UniqueConstraint("job_id", name="uq_lab_runs_job_id"),
        Index("ix_lab_runs_status", "status"),
        Index("ix_lab_runs_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(String(36), nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    engine: Mapped[str] = mapped_column(String(32), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="queued"
    )
    progress: Mapped[float] = mapped_column(Double, nullable=False, server_default="0")
    phase: Mapped[str | None] = mapped_column(String(48))
    worker_id: Mapped[str | None] = mapped_column(String(64))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    warnings: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class AiRun(Base):
    """A Phase-12 AI job AND its result (1:1) — mirrors LabRun/Backtest. The API
    INSERTs status='queued' + SELECTs; the off-API `python -m app.ai_worker`
    (ai-runner, --profile ai) claims a queued row (atomic UPDATE … WHERE
    status='queued'), runs enrichment, and writes result/status back. Writes ONLY
    this table + `news_intel`. `job_id` is a server-minted UUID (route handle +
    Redis progress channel). `kind` ∈ {enrich, narrate} — `narrate` rows (Phase
    16) are written directly by `services/advisor_report.py`, not via the
    API/queue/worker path above; `config` carries their `report_week`."""

    __tablename__ = "ai_runs"
    __table_args__ = (
        UniqueConstraint("job_id", name="uq_ai_runs_job_id"),
        Index("ix_ai_runs_status", "status"),
        Index("ix_ai_runs_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    job_id: Mapped[str] = mapped_column(String(36), nullable=False)
    kind: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="enrich"
    )
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="queued"
    )
    progress: Mapped[float] = mapped_column(Double, nullable=False, server_default="0")
    phase: Mapped[str | None] = mapped_column(String(48))
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    warnings: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    worker_id: Mapped[str | None] = mapped_column(String(96))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class NewsIntelRow(Base):
    """One enriched article — upserted idempotently on `link`. Sentiment/summary
    are model inference (grounding=speculative); title/source/date are grounded."""

    __tablename__ = "news_intel"
    __table_args__ = (
        UniqueConstraint("link", name="uq_news_intel_link"),
        Index("ix_news_intel_published", "published"),
        Index("ix_news_intel_cluster", "cluster_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    link: Mapped[str] = mapped_column(String(1024), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    source: Mapped[str | None] = mapped_column(String(128))
    published: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sentiment: Mapped[str | None] = mapped_column(String(16))
    sentiment_score: Mapped[float | None] = mapped_column(Double)
    topics: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    country: Mapped[str | None] = mapped_column(String(64))
    lat: Mapped[float | None] = mapped_column(Double)
    lon: Mapped[float | None] = mapped_column(Double)
    summary: Mapped[str | None] = mapped_column(Text)
    cluster_id: Mapped[str | None] = mapped_column(String(36))
    grounding: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="unverifiable"
    )
    enriched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AlgoOrder(Base):
    """One paper order. The API INSERTs status='new' (manual ticket or
    strategy runner); the off-API `python -m app.algo_worker` (trading-engine,
    --profile algo) claims it atomically and resolves it to filled/cancelled/
    rejected. `reason` is set on every rejection. strategy_id NULL = manual."""

    __tablename__ = "algo_orders"
    __table_args__ = (
        Index("ix_algo_orders_status", "status"),
        Index("ix_algo_orders_created_at", "created_at"),
        Index("ix_algo_orders_strategy", "strategy_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    strategy_id: Mapped[int | None] = mapped_column(Integer)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)  # buy|sell
    type: Mapped[str] = mapped_column(
        String(8), nullable=False, server_default="market"
    )  # market|limit
    qty: Mapped[float] = mapped_column(Double, nullable=False)
    limit_price: Mapped[float | None] = mapped_column(Double)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="new"
    )
    # new -> accepted (resting limit) -> filled | cancelled | rejected
    reason: Mapped[str | None] = mapped_column(String(256))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class AlgoFill(Base):
    __tablename__ = "algo_fills"
    __table_args__ = (
        Index("ix_algo_fills_order", "order_id"),
        Index("ix_algo_fills_filled_at", "filled_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    order_id: Mapped[int] = mapped_column(Integer, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    side: Mapped[str] = mapped_column(String(4), nullable=False)
    price: Mapped[float] = mapped_column(Double, nullable=False)
    qty: Mapped[float] = mapped_column(Double, nullable=False)
    commission: Mapped[float] = mapped_column(
        Double, nullable=False, server_default="0"
    )
    slippage_bps: Mapped[float] = mapped_column(
        Double, nullable=False, server_default="0"
    )
    filled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AlgoPosition(Base):
    __tablename__ = "algo_positions"
    __table_args__ = (UniqueConstraint("symbol", name="uq_algo_positions_symbol"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    qty: Mapped[float] = mapped_column(Double, nullable=False, server_default="0")
    avg_cost: Mapped[float] = mapped_column(Double, nullable=False, server_default="0")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class AlgoStrategy(Base):
    """Declarative paper strategy (NO code execution — Phase-9 decision inherited).
    config JSONB validated pre-flight at POST (see services/strategy_runner.py)."""

    __tablename__ = "algo_strategies"
    __table_args__ = (UniqueConstraint("name", name="uq_algo_strategies_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    config: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="paused"
    )  # active|paused
    last_rebalance_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AlgoAccountSnapshot(Base):
    __tablename__ = "algo_account_snapshots"
    __table_args__ = (UniqueConstraint("snapshot_date", name="uq_algo_snapshots_date"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    snapshot_date: Mapped[date_type] = mapped_column(Date, nullable=False)
    cash: Mapped[float] = mapped_column(Double, nullable=False)
    positions_value: Mapped[float] = mapped_column(Double, nullable=False)
    equity: Mapped[float] = mapped_column(Double, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AlgoState(Base):
    """Tiny key/value state (DB mirror for the kill switch — survives Redis flush)."""

    __tablename__ = "algo_state"

    key: Mapped[str] = mapped_column(String(32), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class PortfolioSnapshot(Base):
    """One held symbol's weekly-advisor snapshot. One row per symbol per
    `report_week` (the idempotent weekly key), denormalized with the account-level
    cash/nav/regime figures for that snapshot so the row is self-contained.
    `cost_basis` is nullable until the exporter patch lands cost basis on
    `/holdings`. Regime is computed live and persisted here only for audit —
    `regime_component_set_id` records which component set produced it."""

    __tablename__ = "portfolio_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "report_week", "symbol", name="uq_portfolio_snapshots_week_symbol"
        ),
        Index("ix_portfolio_snapshots_report_week", "report_week"),
        Index("ix_portfolio_snapshots_date", "date"),
        Index("ix_portfolio_snapshots_symbol", "symbol"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_week: Mapped[str] = mapped_column(String(16), nullable=False)
    date: Mapped[date_type] = mapped_column(Date, nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    qty: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    value_eur: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    cost_basis: Mapped[Decimal | None] = mapped_column(Numeric(24, 8))
    cash: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    nav: Mapped[Decimal] = mapped_column(Numeric(24, 8), nullable=False)
    regime_composite: Mapped[float | None] = mapped_column(Double)
    regime_component_set_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Recommendation(Base):
    """One rule evaluation/decision event (Phase 16 weekly advisor). Append-only by
    policy — the service layer never UPDATEs a row; a status transition (evaluated ->
    proposed -> {accepted | modified | overridden | expired | superseded} -> executed)
    INSERTs a new row whose `prior_id` points at the row it supersedes (NULL on the
    first `evaluated` row of a rule's lifecycle). `input_snapshot` freezes the data
    the rule saw; `tax_cost_arithmetic` is the indicative tax-gate arithmetic behind
    a sell-side recommendation. `overridden` is ALSO the persisted form of a human
    reject: the frozen `advisor_report._load_overrides` ->
    `advisor._cooldown_check` cooldown loop keys off this exact status string, so the
    `/reject` endpoint reuses it (`input_snapshot["human_action"] = "reject"`, vs.
    `"override"` for the pre-existing `/override` path) rather than a distinct
    `rejected` status, which would silently disable cooldown and let the rule re-fire
    every week. A real `rejected` status is deferred to the Phase-19 unfreeze of that
    loop.

    `executed` is a lifecycle stage this model reserved the column value for; the
    writer now exists (Phase 18,
    `services/recommendation_lifecycle.transition`): an `accepted`/`modified` row can
    transition to `executed`, recording `input_snapshot["execution"] = {executed_at,
    fill_price, quantity, venue}` — the row that means an actual trade went out.
    `expired` (an untouched `proposed` row past `RECOMMENDATION_EXPIRY_WEEKS`) and
    `superseded` (an older `proposed` row in the same rule/symbol scope once a newer
    one exists) are written only by the lifecycle service's periodic
    `expire_and_supersede`, never by a human-facing endpoint."""

    __tablename__ = "recommendations"
    __table_args__ = (
        Index("ix_recommendations_rule_id", "rule_id"),
        Index("ix_recommendations_report_week", "report_week"),
        Index("ix_recommendations_status", "status"),
        Index("ix_recommendations_prior_id", "prior_id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    rule_id: Mapped[str] = mapped_column(String(64), nullable=False)
    as_of: Mapped[date_type] = mapped_column(Date, nullable=False)
    report_week: Mapped[str] = mapped_column(String(16), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    sizing: Mapped[float | None] = mapped_column(Double)
    input_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    regime_component_set_id: Mapped[str | None] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="evaluated"
    )
    # evaluated -> proposed -> {accepted | modified | overridden | expired | superseded} -> executed
    tax_cost_arithmetic: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    note: Mapped[str | None] = mapped_column(Text)
    prior_id: Mapped[int | None] = mapped_column(
        ForeignKey("recommendations.id", name="fk_recommendations_prior_id")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class ReportPack(Base):
    """One weekly report-pack job row. `report_week` (ISO week,
    e.g. `2026-W33`) is the PK — the idempotent weekly key, matching Phase 16's
    `portfolio_snapshots`/`recommendations` convention. `status` tracks the
    build lifecycle (building -> complete | failed); `attempts` is the retry
    counter the worker checks against `REPORT_PACK_MAX_ATTEMPTS` before leaving
    a terminal `failed` row that closes the gate for that week. `sections` is
    the assembled report body; `sources` is per-chapter provenance (engine,
    fetched_at, stale flag, error)."""

    __tablename__ = "report_packs"

    report_week: Mapped[str] = mapped_column(String(8), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="building"
    )
    # building -> complete | failed
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    sections: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    sources: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    pdf_path: Mapped[str | None] = mapped_column(String)
    narrated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    factor_run_id: Mapped[str | None] = mapped_column(String)
    intel_job_id: Mapped[str | None] = mapped_column(String)


class ShadowVerdict(Base):
    """One per-(week, symbol, tool) shadow-grading row (Phase 17;
    writer is the report worker). `direction` is the grading direction (+1/-1, NULL =
    ungradeable — not every tool/verdict pair can be graded, see the per-tool
    gradeability rules)."""

    __tablename__ = "shadow_verdicts"
    __table_args__ = (
        UniqueConstraint(
            "report_week", "symbol", "tool", name="uq_shadow_verdicts_week_symbol_tool"
        ),
        Index("ix_shadow_verdicts_report_week", "report_week"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    report_week: Mapped[str] = mapped_column(String(8), nullable=False)
    symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    tool: Mapped[str] = mapped_column(String(32), nullable=False)
    verdict: Mapped[str] = mapped_column(String(16), nullable=False)
    # add_watch | hold | trim_watch | flag
    direction: Mapped[int | None] = mapped_column(SmallInteger)
    reading: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    horizon_days: Mapped[int | None] = mapped_column(Integer)
    # Grading horizon in days (Phase 18). Historic rows stay NULL and are read as the engine's fixed
    # `_GRADE_HORIZON_WEEKS * 7` constant.


class Forecast(Base):
    """One issued, immutable forecast (Phase 18; writers arrive in
    ). `issued_by` names the writer (`shadow:<tool>` | `human` this
    phase; `advisor:<rule_id>` is reserved for Phase 19 — no writer produces
    it yet). `statement` must be a sentence a stranger can judge true/false
    unaided; for a `>`-comparator forecast it ends with "(exactly zero = no
    decision)". `target`/`resolution_rule` together are everything the
    resolver needs and nothing else.

    **PIT (point-in-time) triple** — `published_at` (when the underlying
    evidence came into existence: for a `daily_bars:` forecast, the
    timestamp of the last daily bar the reading used, NOT the fetch time),
    `first_seen_at` (when Catallax's worker tick first observed that
    evidence) and `available_at` (when this row itself became usable by any
    downstream decision — its persistence time). The invariant
    `published_at <= first_seen_at <= available_at` is enforced by the
    issuing service AND by a DB CHECK constraint, since it is a correctness
    property of the ledger, not of one writer. This triple, plus
    `resolution_source`, is the point-in-time contract Phase 20's
    `market_events` extends — defined once, here.

    Append-only: rows are never UPDATEd once issued; `status` moves
    pending -> resolved | abstained | void via `outcomes`, not by editing
    this row."""

    __tablename__ = "forecasts"
    __table_args__ = (
        UniqueConstraint(
            "issued_by",
            "subject",
            "decision_date",
            "horizon_days",
            name="uq_forecasts_issuer_subject_date_horizon",
        ),
        Index("ix_forecasts_status", "status"),
        Index("ix_forecasts_due_date", "due_date"),
        Index("ix_forecasts_report_week", "report_week"),
        Index(
            "ix_forecasts_issued_by_subject_status", "issued_by", "subject", "status"
        ),
        CheckConstraint(
            "published_at <= first_seen_at AND first_seen_at <= available_at",
            name="ck_forecasts_pit_order",
        ),
        CheckConstraint(
            "probability IS NULL OR (probability >= 0 AND probability <= 1)",
            name="ck_forecasts_probability_range",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    issued_by: Mapped[str] = mapped_column(String(32), nullable=False)
    issuer_version: Mapped[str] = mapped_column(String(64), nullable=False)
    subject_kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # symbol | macro | portfolio; "rule" reserved.
    subject: Mapped[str] = mapped_column(String(64), nullable=False)
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # binary in v1; point/interval reserved.
    probability: Mapped[float | None] = mapped_column(Double)
    distribution: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # reserved
    direction: Mapped[int | None] = mapped_column(
        SmallInteger
    )  # +1/-1, price forecasts
    target: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False)
    decision_date: Mapped[date_type] = mapped_column(Date, nullable=False)
    due_date: Mapped[date_type] = mapped_column(Date, nullable=False)
    report_week: Mapped[str | None] = mapped_column(String(8))
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    resolution_source: Mapped[str] = mapped_column(String(32), nullable=False)
    # "daily_bars:<adapter-source>" | "manual"
    resolution_rule: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    evidence_refs: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="pending"
    )
    # pending -> resolved | abstained | void
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Outcome(Base):
    """One immutable resolution of a `Forecast` (Phase 18; writer
    is the forecast ledger). `forecast_id` is UNIQUE — a forecast resolves once; a
    later restated bar never re-resolves it (policy, not just constraint).
    `hit`/`brier`/`log_score` are NULL until `resolution_status` is
    `matured`, forever NULL on a push, or when the forecast carried no
    `probability` (a directional, non-probabilistic forecast). `attribution`
    judges direction only in v1 — `thesis`/`sizing` are the literal string
    `"n/a"`, waiting on Phase 19's journal links and executed fills
    respectively."""

    __tablename__ = "outcomes"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    forecast_id: Mapped[int] = mapped_column(
        ForeignKey("forecasts.id", name="fk_outcomes_forecast_id"),
        nullable=False,
        unique=True,
    )
    resolved_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    resolution_status: Mapped[str] = mapped_column(String(16), nullable=False)
    # matured | uncovered | manual
    realized: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    hit: Mapped[bool | None] = mapped_column(Boolean)
    brier: Mapped[float | None] = mapped_column(Double)
    log_score: Mapped[float | None] = mapped_column(Double)
    attribution: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    resolver_version: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class JournalEntry(Base):
    """One analyst-journal entry (Phase 19; writer arrives in
    ). `view`/`probability`/`review_date` are the 3-field quick entry;
    `horizon_days` is derived (`review_date - entry_date`) and stored, not
    recomputed, so the emitted `Forecast`'s horizon never drifts from the
    entry that produced it. The remaining fields are progressive disclosure
    behind the quick entry, all nullable. `forecast_id` is nullable only so
    a pack-seeded draft (`source="pack_draft"`, `draft_of_week` set) can
    exist before it is signed and emits a forecast."""

    __tablename__ = "journal_entries"
    __table_args__ = (
        Index("ix_journal_entries_entry_date", "entry_date"),
        Index("ix_journal_entries_status_review_date", "status", "review_date"),
        Index("ix_journal_entries_subject", "subject"),
        CheckConstraint(
            "probability > 0 AND probability < 1",
            name="ck_journal_entries_probability_range",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    entry_date: Mapped[date_type] = mapped_column(Date, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    view: Mapped[str] = mapped_column(Text, nullable=False)
    probability: Mapped[float] = mapped_column(Double, nullable=False)
    review_date: Mapped[date_type] = mapped_column(Date, nullable=False)
    horizon_days: Mapped[int] = mapped_column(Integer, nullable=False)
    subject: Mapped[str] = mapped_column(String(64), nullable=False)
    thesis: Mapped[str | None] = mapped_column(Text)
    evidence: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB
    )  # [{kind, ref, note}]
    invalidation: Mapped[str | None] = mapped_column(Text)
    sizing_note: Mapped[str | None] = mapped_column(Text)
    confidence_note: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="open"
    )
    # open -> reviewed | superseded
    source: Mapped[str] = mapped_column(
        String(24), nullable=False, server_default="human"
    )
    # human | pack_draft
    draft_of_week: Mapped[str | None] = mapped_column(String(8))
    forecast_id: Mapped[int | None] = mapped_column(
        ForeignKey("forecasts.id", name="fk_journal_entries_forecast_id"),
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    review_note: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class Note(Base):
    """Research note / investment thesis as a first-class object (Phase 19,
    schema only; the writer arrives later). `kind` distinguishes a `research_note`
    from a `thesis`; the body fields mirror roadmap §5.3's progressive
    disclosure (one sentence -> why it matters -> action -> risk/
    invalidation -> long-form body). `evidence_refs` links out to any
    existing record kind the pack already knows about."""

    __tablename__ = "notes"
    __table_args__ = (
        Index("ix_notes_kind_status", "kind", "status"),
        Index("ix_notes_slug", "slug"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    slug: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    # research_note | thesis
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, server_default="draft"
    )
    # draft | signed
    one_sentence: Mapped[str] = mapped_column(Text, nullable=False)
    why_it_matters: Mapped[str | None] = mapped_column(Text)
    action: Mapped[str | None] = mapped_column(Text)
    risk_invalidation: Mapped[str | None] = mapped_column(Text)
    body: Mapped[str | None] = mapped_column(Text)
    evidence_refs: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB
    )  # [{kind, id, label}]
    subjects: Mapped[list[str] | None] = mapped_column(JSONB)
    signed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class CockpitSession(Base):
    """One persisted cockpit conversation (Phase 19; writer
    is the cockpit service). `session_key` is the opaque uuid4-hex id the frontend
    addresses; `title` is the first user message, truncated."""

    __tablename__ = "cockpit_sessions"
    __table_args__ = (Index("ix_cockpit_sessions_updated_at", "updated_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    # uuid4 hex
    title: Mapped[str | None] = mapped_column(String(200))
    # first user message, truncated
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class CockpitMessage(Base):
    """One turn of a `CockpitSession` (Phase 19; writer is the
    cockpit service). `tool_calls`/`citations` record what the typed-tool layer
    read and cited for an assistant turn; `refused` flags a turn where the
    assistant declined a write request — a correctness signal we want
    queryable without parsing `content`."""

    __tablename__ = "cockpit_messages"
    __table_args__ = (
        Index("ix_cockpit_messages_session_id", "session_id"),
        Index("ix_cockpit_messages_session_id_id", "session_id", "id"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    session_id: Mapped[int] = mapped_column(
        ForeignKey("cockpit_sessions.id", name="fk_cockpit_messages_session_id"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    # user | assistant | tool
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tool_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB
    )  # [{tool, args, row_count, record_ids}]
    citations: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSONB
    )  # [{kind, id}]
    refused: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    job_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MarketEvent(Base):
    __tablename__ = "market_events"
    __table_args__ = (
        UniqueConstraint(
            "source",
            "feed",
            "source_key",
            "revision",
            name="uq_market_events_source_feed_key_revision",
        ),
        CheckConstraint("revision >= 1", name="ck_market_events_revision_positive"),
        CheckConstraint(
            "published_at <= first_seen_at AND first_seen_at <= available_at",
            name="ck_market_events_pit_order",
        ),
        Index("ix_market_events_published_at", text("published_at DESC")),
        Index("ix_market_events_event_type_published_at", "event_type", "published_at"),
        Index("ix_market_events_story_key_published_at", "story_key", "published_at"),
        Index(
            "ix_market_events_unprocessed_first_seen_at",
            "first_seen_at",
            postgresql_where=text("processed_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str] = mapped_column(String(32), nullable=False)
    feed: Mapped[str] = mapped_column(String(64), nullable=False)
    source_key: Mapped[str] = mapped_column(String(255), nullable=False)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, server_default="1")
    supersedes_id: Mapped[int | None] = mapped_column(
        ForeignKey("market_events.id", name="fk_market_events_supersedes_id")
    )
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    form_type: Mapped[str | None] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(String(500), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    url: Mapped[str] = mapped_column(String(1000), nullable=False)
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=text("clock_timestamp()"),
    )
    published_at_estimated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=text("false")
    )
    entity_hints: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    story_key: Mapped[str | None] = mapped_column(String(40))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class EventEntity(Base):
    __tablename__ = "event_entities"
    __table_args__ = (
        UniqueConstraint(
            "event_id", "kind", "value", name="uq_event_entities_event_kind_value"
        ),
        CheckConstraint(
            "confidence >= 0 AND confidence <= 1",
            name="ck_event_entities_confidence_range",
        ),
        Index("ix_event_entities_symbol", "symbol"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("market_events.id", name="fk_event_entities_event_id"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(16), nullable=False)
    value: Mapped[str] = mapped_column(String(64), nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(32))
    method: Mapped[str] = mapped_column(String(24), nullable=False)
    confidence: Mapped[float] = mapped_column(Double, nullable=False)
    relevance: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, server_default="[]"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Alert(Base):
    __tablename__ = "alerts"
    __table_args__ = (
        UniqueConstraint("event_id", "rule", name="uq_alerts_event_rule"),
        CheckConstraint(
            "disposition IS NULL OR disposition IN ('useful', 'false_positive')",
            name="ck_alerts_disposition",
        ),
        Index("ix_alerts_status_fired_at", "status", "fired_at"),
        Index("ix_alerts_cooldown_key_fired_at", "cooldown_key", "fired_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    event_id: Mapped[int] = mapped_column(
        ForeignKey("market_events.id", name="fk_alerts_event_id"), nullable=False
    )
    rule: Mapped[str] = mapped_column(String(48), nullable=False)
    score: Mapped[float] = mapped_column(Double, nullable=False)
    components: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    why: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    symbol: Mapped[str | None] = mapped_column(String(32))
    relevance_kind: Mapped[str | None] = mapped_column(String(16))
    cooldown_key: Mapped[str] = mapped_column(String(128), nullable=False)
    mode: Mapped[str] = mapped_column(String(12), nullable=False)
    status: Mapped[str] = mapped_column(
        String(12), nullable=False, server_default="open"
    )
    disposition: Mapped[str | None] = mapped_column(String(16))
    snoozed_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AlertDelivery(Base):
    __tablename__ = "alert_deliveries"
    __table_args__ = (
        CheckConstraint(
            "alert_id IS NOT NULL OR (channel = 'audit' AND action = 'suppress')",
            name="ck_alert_deliveries_alert_or_suppress",
        ),
        Index("ix_alert_deliveries_alert_id", "alert_id"),
        Index("ix_alert_deliveries_event_id", "event_id"),
        Index("ix_alert_deliveries_action_attempted_at", "action", "attempted_at"),
    )
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    alert_id: Mapped[int | None] = mapped_column(
        ForeignKey("alerts.id", name="fk_alert_deliveries_alert_id")
    )
    event_id: Mapped[int] = mapped_column(
        ForeignKey("market_events.id", name="fk_alert_deliveries_event_id"),
        nullable=False,
    )
    rule: Mapped[str] = mapped_column(String(48), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    action: Mapped[str] = mapped_column(String(16), nullable=False)
    attempted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ok: Mapped[bool] = mapped_column(Boolean, nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class SourceCursor(Base):
    __tablename__ = "source_cursors"
    source: Mapped[str] = mapped_column(String(32), primary_key=True)
    feed: Mapped[str] = mapped_column(String(64), primary_key=True)
    cursor: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, server_default="{}"
    )
    etag: Mapped[str | None] = mapped_column(String)
    last_modified: Mapped[str | None] = mapped_column(String)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    consecutive_failures: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    dead_letter_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default="0"
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    events_seen: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    events_new: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    events_revised: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    events_duplicate: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )
    events_rejected: Mapped[int] = mapped_column(
        BigInteger, nullable=False, server_default="0"
    )


class DemoSeedMarker(Base):
    """One row, written only by `app.demo_seed` on a database that was empty of every
    seeded table. Its presence (and the per-table row counts it records) is what lets the
    hosted demo backend trust that the DB holds only synthetic sample rows."""

    __tablename__ = "demo_seed_marker"
    __table_args__ = (CheckConstraint("id = 1", name="ck_demo_seed_marker_single"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    counts: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    seeded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
