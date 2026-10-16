"""Declarative strategy runner — the paper-algo hub's rebalance loop.

`AlgoStrategy.config` (JSONB) is a `StrategyConfig`: NO code execution (the same
decision `BacktestConfig` made in Phase 9) — a strategy is "rank `signal_id`'s
latest cross-section over `universe`, hold the top `top_n`, rebalance on
`cadence`". `StrategyRunner.run_due()` is the off-API worker's per-tick entry
point: for every `status='active'` strategy whose cadence has elapsed, it reads
the latest signal scores, turns them into target weights, diffs those targets
against the CURRENT (strategy-agnostic — v1 shares one paper book across all
active strategies, see module docstring in `services/algo.py`) positions/equity,
and submits the resulting orders through `AlgoService.submit_order` — one
`try/except` per order (a single rejection must not abort the rest of the
rebalance) inside one `try/except` per strategy (one strategy's bad config/data
must not block its siblings).

Pure math lives in four free functions (`target_weights`, `diff_to_orders`,
`cap_orders`, `rebalance_due`) so the rebalance arithmetic is unit-testable
without a DB.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, Field, ValidationError
from redis.asyncio import Redis
from sqlalchemy import func, select, update

from app.adapters.broker.factory import build_broker
from app.config import Settings
from app.db import session_scope
from app.models import AlgoStrategy, SignalRow
from app.services.algo import AlgoService, OrderSpec, _SYMBOL_RE

logger = logging.getLogger("catallax.strategy_runner")

_EPSILON = 1e-9


class StrategyConfig(BaseModel):
    """The `algo_strategies.config` JSONB shape — validated structurally (this
    model) at every `run_due` tick, and pre-flight (`StrategyRunner.validate`,
    signal_id existence + universe symbol charset) at strategy creation."""

    signal_id: str
    universe: list[str] = Field(min_length=1, max_length=50)
    top_n: int = Field(default=5, ge=1, le=20)
    weighting: Literal["equal", "score"] = "equal"
    rebalance: Literal["daily", "weekly", "monthly"] = "weekly"
    max_position_pct: float = Field(default=0.25, gt=0, le=1)
    max_orders_per_rebalance: int = Field(default=20, ge=1)


def _floor4(x: float) -> float:
    return math.floor(x * 10000.0) / 10000.0


# ── pure functions (unit-tested without a DB) ───────────────────
def target_weights(
    scores: dict[str, float], top_n: int, weighting: str, max_position_pct: float
) -> dict[str, float]:
    """Rank `scores` desc, keep the top `top_n`, weight them `equal` or
    `score` (positive-shifted score, normalized), then cap each at
    `max_position_pct` and redistribute the capped excess across the
    still-uncapped symbols ONCE (a cap that binds hard enough that
    `top_n * max_position_pct < 1` wins over reaching sum=1 — the redistribution
    pass simply has nowhere left to put the excess)."""
    if not scores or top_n <= 0:
        return {}
    selected = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)[:top_n]
    n = len(selected)

    if weighting == "equal":
        raw = {sym: 1.0 / n for sym, _ in selected}
    else:  # "score"
        floor = min(v for _, v in selected)
        shifted = {sym: (v - floor + _EPSILON) for sym, v in selected}
        total = sum(shifted.values())
        raw = {sym: shifted[sym] / total for sym in shifted}

    capped = {sym: min(w, max_position_pct) for sym, w in raw.items()}
    excess = sum(raw[sym] - capped[sym] for sym in raw)
    if excess > _EPSILON:
        uncapped = [sym for sym in raw if raw[sym] < max_position_pct - _EPSILON]
        uncapped_total = sum(capped[sym] for sym in uncapped)
        if uncapped_total > 0:
            for sym in uncapped:
                share = capped[sym] / uncapped_total
                capped[sym] = min(capped[sym] + excess * share, max_position_pct)
    return capped


def diff_to_orders(
    targets: dict[str, float],
    positions: dict[str, float],
    marks: dict[str, float],
    equity: float,
    min_notional: float = 50.0,
) -> list[tuple[str, str, float]]:
    """Diff `targets` (weight, 0..1) against current `positions` (qty) to a
    list of `(symbol, side, qty)` orders — sells BEFORE buys (free up cash
    first), dust deltas (< `min_notional`) skipped, symbols missing a mark (or
    with a zero/negative mark — division defense) skipped entirely (logged).
    Buy qty is the floor-to-4-decimals target position size
    (`weight * equity / mark`); sell qty is the full position delta needed to
    reach the target (0 for a symbol dropped from `targets`)."""
    sells: list[tuple[str, str, float]] = []
    buys: list[tuple[str, str, float]] = []
    for symbol in sorted(set(targets) | set(positions)):
        mark = marks.get(symbol)
        if mark is None or mark <= 0:  # guard: a zero/negative mark would ZeroDivisionError or invert below
            logger.warning("diff_to_orders: no usable mark for %s; skipping", symbol)
            continue
        weight = targets.get(symbol, 0.0)
        current_qty = positions.get(symbol, 0.0)
        target_qty = _floor4(weight * equity / mark) if weight > 0 else 0.0
        delta = target_qty - current_qty
        if abs(delta) < _EPSILON:
            continue
        if abs(delta) * mark < min_notional:
            continue
        if delta > 0:
            buys.append((symbol, "buy", delta))
        else:
            sells.append((symbol, "sell", abs(delta)))
    return sells + buys


def cap_orders(
    sells: list[tuple[str, str, float]], buys: list[tuple[str, str, float]], max_orders: int
) -> list[tuple[str, str, float]]:
    """Cap the total order count at `max_orders` by truncating from the BUY end
    only — sells are never truncated (de-risking wins: exiting a position
    always beats opening/adding one, per the strategy's mandate). If `sells`
    alone already meets or exceeds `max_orders`, no buys are emitted at all."""
    return sells + buys[: max(0, max_orders - len(sells))]


def rebalance_due(last: datetime | None, cadence: str, now: datetime) -> bool:
    """Calendar-boundary cadence (not elapsed-time): `daily` fires once the
    calendar date has changed, `weekly` once the ISO week has changed,
    `monthly` once the (year, month) has changed. `last=None` always fires
    (a strategy that has never rebalanced is always due)."""
    if last is None:
        return True
    if cadence == "daily":
        return last.date() != now.date()
    if cadence == "weekly":
        return last.isocalendar()[:2] != now.isocalendar()[:2]
    if cadence == "monthly":
        return (last.year, last.month) != (now.year, now.month)
    raise ValueError(f"unknown cadence '{cadence}'")


class StrategyRunner:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings

    async def validate(self, cfg: StrategyConfig) -> list[dict[str, str]]:
        """Pre-flight (structured field errors, 400 PREFLIGHT_FAILED at the
        creation route) — DB-backed checks pydantic can't express: `signal_id`
        must have logged rows, `universe` symbols must be valid order symbols."""
        errors: list[dict[str, str]] = []
        async with session_scope() as db:
            n_rows = (await db.execute(
                select(func.count()).where(SignalRow.signal_id == cfg.signal_id)
            )).scalar_one()
        if not n_rows:
            errors.append({"field": "signal_id", "error": f"no signals logged for '{cfg.signal_id}'"})

        bad_symbols = [s for s in cfg.universe if not _SYMBOL_RE.match(s)]
        if bad_symbols:
            errors.append({
                "field": "universe",
                "error": f"invalid symbol(s): {', '.join(bad_symbols)}",
            })
        return errors

    async def run_due(self, algo: AlgoService) -> int:
        """Called each worker tick. Kill switch on -> skip entirely (no
        strategy is read, let alone rebalanced). Returns the total number of
        orders successfully submitted (queued) across all due strategies."""
        if await algo.kill_state():
            return 0

        async with session_scope() as db:
            rows = (await db.execute(
                select(AlgoStrategy).where(AlgoStrategy.status == "active")
            )).scalars().all()

        now = datetime.now(timezone.utc)
        emitted = 0
        for row in rows:
            try:
                emitted += await self._run_one(row, algo, now)
            except Exception:  # noqa: BLE001 — one bad strategy must not block its siblings
                logger.exception("strategy_runner: strategy %s ('%s') failed", row.id, row.name)
        return emitted

    async def _run_one(self, row: AlgoStrategy, algo: AlgoService, now: datetime) -> int:
        try:
            cfg = StrategyConfig(**row.config)
        except ValidationError as exc:
            logger.warning("strategy_runner: strategy %s ('%s') has invalid stored config: %s",
                            row.id, row.name, exc)
            return 0

        if not rebalance_due(row.last_rebalance_at, cfg.rebalance, now):
            return 0

        scores = await self._latest_scores(cfg.signal_id, cfg.universe)
        if not scores:
            logger.info("strategy_runner: strategy %s ('%s') has no '%s' scores for its universe, skipping",
                         row.id, row.name, cfg.signal_id)
            return 0

        targets = target_weights(scores, cfg.top_n, cfg.weighting, cfg.max_position_pct)

        position_rows = await algo.positions()
        positions = {p["symbol"]: p["qty"] for p in position_rows}
        broker = build_broker(self._settings.algo_broker, self._settings)
        marks = await broker.mark(sorted(set(targets) | set(positions)))
        account = await algo.account()

        orders = diff_to_orders(targets, positions, marks, account["equity"])
        sells = [o for o in orders if o[1] == "sell"]
        buys = [o for o in orders if o[1] == "buy"]
        # Sells are never truncated by max_orders_per_rebalance — only buys are
        # (de-risking wins; see `cap_orders` docstring).
        capped = cap_orders(sells, buys, cfg.max_orders_per_rebalance)

        emitted = 0
        for symbol, side, qty in capped:
            try:
                await algo.submit_order(OrderSpec(symbol=symbol, side=side, qty=qty), strategy_id=row.id)
                emitted += 1
            except Exception as exc:  # noqa: BLE001 — a rejected order must not abort the rebalance
                logger.warning("strategy_runner: strategy %s order %s %s %.4f rejected: %s",
                                row.id, side, symbol, qty, exc)

        # Order emission (above) and this last_rebalance_at stamp (below) are NOT atomic —
        # a crash between them re-emits the same rebalance next tick. Acceptable v1 paper
        # tradeoff (no real capital at risk); would need a single transaction for live trading.
        async with session_scope() as db:
            await db.execute(
                update(AlgoStrategy).where(AlgoStrategy.id == row.id).values(last_rebalance_at=now)
            )
            await db.commit()
        return emitted

    async def _latest_scores(self, signal_id: str, universe: list[str]) -> dict[str, float]:
        async with session_scope() as db:
            max_date = select(func.max(SignalRow.as_of_date)).where(
                SignalRow.signal_id == signal_id
            ).scalar_subquery()
            rows = (await db.execute(
                select(SignalRow.symbol, SignalRow.value).where(
                    SignalRow.signal_id == signal_id,
                    SignalRow.symbol.in_(universe),
                    SignalRow.as_of_date == max_date,
                )
            )).all()
        return {r.symbol: r.value for r in rows}
