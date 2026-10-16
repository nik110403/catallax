"""Paper OMS core — fills, positions, account, kill switch,
reconciliation. Engine heart of the paper-trading algo hub.

Mirrors `BacktestService`/`IntelService`: `AlgoService` holds redis + settings,
no request-scoped DB session — every op opens its own `session_scope()`. Order
lifecycle: the API path (`submit_order`) validates + INSERTs status='new' and
nudges the worker; the off-API `python -m app.algo_worker` (trading-engine,
--profile algo) polls for 'new' orders and resolves each to
filled/accepted/rejected. `cancel_order`/`kill` cancel from the API side.

SINGLE-WORKER ASSUMPTION: `claim_new_orders` is a plain SELECT (no `FOR UPDATE
SKIP LOCKED`) because exactly one `algo_worker` process is expected to run
against this DB (compose runs one `trading-engine` replica). The actual
atomicity/idempotency guard lives in the `UPDATE ... WHERE status='new'` (or
'accepted' for resting limits) transition inside `_settle`/`_reject_new`/
`_accept_resting` — even a concurrent re-claim of the same order id is a
no-op, so a second worker replica would not double-fill, but IS NOT a
supported/tested configuration.

Never-raise philosophy on every worker-side method: a failure rejects the
order (with `reason` set) or is logged and skipped — the worker loop must
survive any single order/symbol going wrong.
"""
from __future__ import annotations

import json
import logging
import math
import re
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, field_validator, model_validator
from redis.asyncio import Redis
from sqlalchemy import case, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.adapters.broker.factory import build_broker
from app.config import Settings
from app.db import session_scope
from app.models import AlgoAccountSnapshot, AlgoFill, AlgoOrder, AlgoPosition, AlgoState

logger = logging.getLogger("catallax.algo")

_KILL_KEY = "catallax:algo:kill"          # Redis fast path; DB mirror in algo_state key="kill"
_NUDGE = "catallax:algo:nudge"            # pub/sub channel API -> worker
_EVENTS = "catallax:algo:events"          # pub/sub channel worker -> SSE
_HEARTBEAT_KEY = "catallax:algo:worker_heartbeat"

# Mirrors the signal_id/symbol charset guard used elsewhere (routers/signals.py,
# adapters/backtest/base.py) — a symbol becomes part of a broker lookup, so
# bound the alphabet defensively.
_SYMBOL_RE = re.compile(r"^[A-Z0-9.\-\^=]{1,20}$")

_OPEN_ORDER_STATUSES = ("new", "accepted")


# ── pure fill math (unit-testable without DB) ───────────────────
def fill_price(quote: float, side: str, slippage_bps: float) -> float:
    """Simulated market-order slippage, adverse on both sides: a buy fills
    above the quote, a sell fills below it."""
    adj = slippage_bps / 10000.0
    if side == "buy":
        return quote * (1 + adj)
    return quote * (1 - adj)


def new_avg_cost(old_qty: float, old_avg: float, fill_qty: float, price: float) -> float:
    """Weighted-average cost after adding `fill_qty` @ `price` to an existing
    `old_qty` @ `old_avg` position (buy-side only — sells don't move avg_cost)."""
    total_qty = old_qty + fill_qty
    if total_qty == 0:
        return 0.0
    return (old_qty * old_avg + fill_qty * price) / total_qty


def limit_crosses(side: str, quote: float, limit_price: float) -> bool:
    """A resting limit fills (at the limit price) once the market crosses it:
    buy fills when quote <= limit; sell fills when quote >= limit (at-limit fills)."""
    if side == "buy":
        return quote <= limit_price
    return quote >= limit_price


def notional_ok(qty: float, price: float, cap: float) -> bool:
    """True when qty * price is within the configured notional cap. Used both
    as the submit-time fast-fail (when a quote is available) and as the
    settle-time recheck (which always has a fill price, even when the
    submit-time quote was unavailable)."""
    return qty * price <= cap


def replay_cash(fills: list[Any], starting_cash: float) -> float:
    """Cash from a fill replay: starting cash - buys + sells - commission,
    computed by walking `fills` (each exposing .side/.qty/.price/.commission)
    directly — independent of `_compute_cash`'s SQL SUM aggregate, so the
    reconciliation endpoint gets a genuinely second, cross-checking figure."""
    cash = starting_cash
    for f in fills:
        if f.side == "buy":
            cash -= f.qty * f.price
        else:
            cash += f.qty * f.price
        cash -= f.commission
    return cash


def pick_day_pnl_baseline(snapshots_desc: list[Any], today: Any) -> Any | None:
    """Pick the day_pnl baseline: the latest snapshot strictly before `today`,
    from a list of objects exposing `.snapshot_date`, already ordered
    newest-first (desc). Returns None if no prior-day snapshot exists, in
    which case `day_pnl` is reported as null rather than diffed against
    today's own (same-day) snapshot."""
    return next((s for s in snapshots_desc if s.snapshot_date < today), None)


class OrderSpec(BaseModel):
    """Request body for `POST /algo/orders`."""

    symbol: str
    side: Literal["buy", "sell"]
    type: Literal["market", "limit"] = "market"
    qty: float
    limit_price: float | None = None

    @field_validator("symbol")
    @classmethod
    def _ck_symbol(cls, v: str) -> str:
        v = v.strip().upper()
        if not _SYMBOL_RE.match(v):
            raise ValueError(f"invalid symbol '{v}'")
        return v

    @field_validator("qty")
    @classmethod
    def _ck_qty(cls, v: float) -> float:
        if not math.isfinite(v) or v <= 0:
            raise ValueError("qty must be a finite number > 0")
        return v

    @model_validator(mode="after")
    def _ck_limit_price(self) -> "OrderSpec":
        if self.type == "limit" and self.limit_price is None:
            raise ValueError("limit_price is required when type='limit'")
        if self.limit_price is not None and (not math.isfinite(self.limit_price) or self.limit_price <= 0):
            raise ValueError("limit_price must be a finite number > 0")
        return self


def _order_dict(row: AlgoOrder) -> dict[str, Any]:
    return {
        "id": row.id, "symbol": row.symbol, "side": row.side, "type": row.type,
        "qty": row.qty, "limit_price": row.limit_price, "status": row.status,
        "reason": row.reason, "strategy_id": row.strategy_id,
        "created_at": row.created_at.isoformat(),
    }


def _fill_dict(row: AlgoFill) -> dict[str, Any]:
    return {
        "id": row.id, "order_id": row.order_id, "symbol": row.symbol, "side": row.side,
        "price": row.price, "qty": row.qty, "commission": row.commission,
        "slippage_bps": row.slippage_bps, "filled_at": row.filled_at.isoformat(),
    }


def _demo_readonly() -> HTTPException:
    """The ONE 403 shape for every algo mutation blocked under DEMO_MODE — shared
    by `AlgoService`'s own mutating methods and `routers/algo.py`'s strategy CRUD
    handlers (which have no service-layer seam of their own)."""
    return HTTPException(status_code=403, detail={
        "code": "DEMO_MODE_READONLY", "message": "Not available in demo mode",
    })


class AlgoService:
    def __init__(self, redis: Redis, settings: Settings) -> None:
        self._redis = redis
        self._settings = settings

    # ── API side ─────────────────────────────────────────────
    async def submit_order(self, spec: OrderSpec, strategy_id: int | None = None) -> dict[str, Any]:
        if self._settings.demo_mode:
            raise _demo_readonly()
        if await self.kill_state():
            raise HTTPException(status_code=400, detail={
                "code": "KILL_SWITCH_ON",
                "message": "algo kill switch is engaged; resume before submitting orders",
            })

        async with session_scope() as db:
            open_count = (await db.execute(
                select(func.count()).where(AlgoOrder.status.in_(_OPEN_ORDER_STATUSES))
            )).scalar_one()
        if open_count >= self._settings.algo_max_open_orders:
            raise HTTPException(status_code=400, detail={
                "code": "TOO_MANY_OPEN_ORDERS",
                "message": f"open order count ({open_count}) at/above limit ({self._settings.algo_max_open_orders})",
            })

        broker = build_broker(self._settings.algo_broker, self._settings)
        quote = (await broker.mark([spec.symbol])).get(spec.symbol)
        if quote is not None:
            notional = spec.qty * quote
            if notional > self._settings.algo_max_order_notional:
                raise HTTPException(status_code=400, detail={
                    "code": "NOTIONAL_TOO_LARGE",
                    "message": f"estimated notional {notional:.2f} exceeds limit {self._settings.algo_max_order_notional}",
                })

        async with session_scope() as db:
            row = AlgoOrder(
                strategy_id=strategy_id, symbol=spec.symbol, side=spec.side, type=spec.type,
                qty=spec.qty, limit_price=spec.limit_price, status="new",
            )
            db.add(row)
            await db.commit()
            await db.refresh(row)
            result = _order_dict(row)

        await self._nudge()
        await self._publish_event({
            "type": "order", "id": result["id"], "symbol": result["symbol"],
            "status": result["status"], "reason": result["reason"],
        })
        return result

    async def cancel_order(self, order_id: int) -> dict[str, Any]:
        if self._settings.demo_mode:
            raise _demo_readonly()
        async with session_scope() as db:
            res = await db.execute(
                update(AlgoOrder).where(AlgoOrder.id == order_id, AlgoOrder.status.in_(_OPEN_ORDER_STATUSES))
                .values(status="cancelled", reason="cancelled by user")
                .returning(AlgoOrder.id)
            )
            cancelled_id = res.scalar_one_or_none()
            await db.commit()
            if cancelled_id is None:
                row = (await db.execute(select(AlgoOrder).where(AlgoOrder.id == order_id))).scalar_one_or_none()
                if row is None:
                    raise HTTPException(status_code=404, detail={
                        "code": "NOT_FOUND", "message": "order not found",
                    })
                raise HTTPException(status_code=400, detail={
                    "code": "NOT_CANCELLABLE",
                    "message": f"order status is '{row.status}', not cancellable",
                })
            row = (await db.execute(select(AlgoOrder).where(AlgoOrder.id == order_id))).scalar_one()
            result = _order_dict(row)

        await self._publish_event({
            "type": "order", "id": result["id"], "symbol": result["symbol"],
            "status": result["status"], "reason": result["reason"],
        })
        return result

    async def list_orders(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        if self._settings.demo_mode:
            return []
        limit = max(1, min(int(limit), 500))
        async with session_scope() as db:
            q = select(AlgoOrder).order_by(AlgoOrder.created_at.desc()).limit(limit)
            if status:
                q = q.where(AlgoOrder.status == status)
            rows = (await db.execute(q)).scalars().all()
        return [_order_dict(r) for r in rows]

    async def list_fills(self, limit: int = 100) -> list[dict[str, Any]]:
        if self._settings.demo_mode:
            return []
        limit = max(1, min(int(limit), 500))
        async with session_scope() as db:
            rows = (await db.execute(
                select(AlgoFill).order_by(AlgoFill.filled_at.desc()).limit(limit)
            )).scalars().all()
        return [_fill_dict(r) for r in rows]

    async def positions(self) -> list[dict[str, Any]]:
        if self._settings.demo_mode:
            return []
        async with session_scope() as db:
            rows = (await db.execute(select(AlgoPosition).where(AlgoPosition.qty != 0))).scalars().all()
        marks = await self._mark_symbols([r.symbol for r in rows])
        out = []
        for r in rows:
            mark = marks.get(r.symbol)
            unrealized = (mark - r.avg_cost) * r.qty if mark is not None else None
            out.append({
                "symbol": r.symbol, "qty": r.qty, "avg_cost": r.avg_cost,
                "mark": mark, "unrealized_pnl": unrealized,
            })
        return out

    async def account(self) -> dict[str, Any]:
        if self._settings.demo_mode:
            return {
                "cash": 0.0, "equity": 0.0, "positions_value": 0.0, "day_pnl": None,
                "snapshots": [],
            }
        async with session_scope() as db:
            cash = await self._compute_cash(db)
            pos_rows = (await db.execute(select(AlgoPosition).where(AlgoPosition.qty != 0))).scalars().all()
            # desc + limit(90) so the 90 rows kept are the MOST RECENT (asc+limit
            # would keep the oldest once history exceeds 90); reverse below so the
            # returned `snapshots` list stays ascending for charting.
            snap_rows_desc = (await db.execute(
                select(AlgoAccountSnapshot).order_by(AlgoAccountSnapshot.snapshot_date.desc()).limit(90)
            )).scalars().all()
        snap_rows = list(reversed(snap_rows_desc))

        marks = await self._mark_symbols([r.symbol for r in pos_rows])
        positions_value = sum(marks.get(r.symbol, r.avg_cost) * r.qty for r in pos_rows)
        equity = cash + positions_value

        today = datetime.now(timezone.utc).date()
        baseline = pick_day_pnl_baseline(snap_rows_desc, today)
        day_pnl = (equity - baseline.equity) if baseline is not None else None

        return {
            "cash": cash, "equity": equity, "positions_value": positions_value, "day_pnl": day_pnl,
            "snapshots": [
                {"date": s.snapshot_date.isoformat(), "cash": s.cash,
                 "positions_value": s.positions_value, "equity": s.equity}
                for s in snap_rows
            ],
        }

    async def kill(self) -> dict[str, Any]:
        if self._settings.demo_mode:
            raise _demo_readonly()
        await self._set_kill_state(True)
        async with session_scope() as db:
            await db.execute(
                update(AlgoOrder).where(AlgoOrder.status.in_(_OPEN_ORDER_STATUSES))
                .values(status="cancelled", reason="kill switch")
            )
            await db.commit()
        await self._publish_event({"type": "kill", "on": True})
        return {"kill": True}

    async def resume(self) -> dict[str, Any]:
        if self._settings.demo_mode:
            raise _demo_readonly()
        await self._set_kill_state(False)
        await self._publish_event({"type": "kill", "on": False})
        return {"kill": False}

    async def kill_state(self) -> bool:
        try:
            val = await self._redis.get(_KILL_KEY)
            if val is not None:
                return val == "1"
        except Exception as exc:  # noqa: BLE001 — Redis hiccup falls back to the DB mirror
            logger.debug("algo kill_state redis read failed: %s", exc)
        async with session_scope() as db:
            row = (await db.execute(select(AlgoState).where(AlgoState.key == "kill"))).scalar_one_or_none()
        return bool(row.value.get("on")) if row else False

    async def status(self) -> dict[str, Any]:
        kill = await self.kill_state()
        hb_fresh = False
        try:
            hb_raw = await self._redis.get(_HEARTBEAT_KEY)
            if hb_raw:
                hb = json.loads(hb_raw)
                hb_time = datetime.fromisoformat(hb["ts"])
                hb_fresh = (datetime.now(timezone.utc) - hb_time).total_seconds() < self._settings.algo_stale_seconds
        except Exception as exc:  # noqa: BLE001
            logger.debug("algo status heartbeat read failed: %s", exc)
        return {"kill": kill, "worker_heartbeat_fresh": hb_fresh, "broker": self._settings.algo_broker}

    async def reconciliation(self) -> dict[str, Any]:
        """Replay every fill (independent of `algo_positions`) into a from-scratch
        position map + cash figure, then diff against the stored positions table
        and the derived-cash aggregate (`_compute_cash`'s SQL SUM). The replayed
        cash is computed independently via `replay_cash` over the same fill
        list — a genuine cross-check, not a copy of the stored figure."""
        if self._settings.demo_mode:
            return {"clean": True, "positions": [], "cash": {"stored": 0.0, "replayed": 0.0, "match": True}}
        async with session_scope() as db:
            fills = (await db.execute(
                select(AlgoFill).order_by(AlgoFill.filled_at.asc(), AlgoFill.id.asc())
            )).scalars().all()
            stored_positions = (await db.execute(select(AlgoPosition))).scalars().all()
            stored_cash = await self._compute_cash(db)

        replayed: dict[str, dict[str, float]] = {}
        for f in fills:
            pos = replayed.setdefault(f.symbol, {"qty": 0.0, "avg_cost": 0.0})
            if f.side == "buy":
                pos["avg_cost"] = new_avg_cost(pos["qty"], pos["avg_cost"], f.qty, f.price)
                pos["qty"] += f.qty
            else:
                pos["qty"] -= f.qty

        stored_by_symbol = {r.symbol: r for r in stored_positions}
        clean = True
        pos_report = []
        for symbol in sorted(set(stored_by_symbol) | set(replayed)):
            stored_qty = stored_by_symbol[symbol].qty if symbol in stored_by_symbol else 0.0
            replayed_qty = replayed.get(symbol, {"qty": 0.0})["qty"]
            match = abs(stored_qty - replayed_qty) < 1e-6
            clean = clean and match
            pos_report.append({
                "symbol": symbol, "stored_qty": stored_qty, "replayed_qty": replayed_qty, "match": match,
            })

        replayed_cash = replay_cash(fills, float(self._settings.algo_starting_cash))
        cash_match = math.isclose(stored_cash, replayed_cash, rel_tol=1e-9, abs_tol=1e-6)
        clean = clean and cash_match

        return {
            "clean": clean,
            "positions": pos_report,
            "cash": {"stored": stored_cash, "replayed": replayed_cash, "match": cash_match},
        }

    # ── worker side (used by algo_worker only) ────────────────
    async def claim_new_orders(self, worker_id: str) -> list[int]:
        """Return every order id in status='new', oldest first. See the module
        docstring: single-worker assumption — this is a plain SELECT, not a
        `FOR UPDATE SKIP LOCKED` claim; `_settle`/`_reject_new`/`_accept_resting`
        carry the actual atomic `WHERE status=...` transition guard."""
        async with session_scope() as db:
            ids = [r[0] for r in (await db.execute(
                select(AlgoOrder.id).where(AlgoOrder.status == "new").order_by(AlgoOrder.id)
            )).all()]
        logger.debug("algo worker %s claimed %d new order(s)", worker_id, len(ids))
        return ids

    async def execute_order(self, order_id: int) -> None:
        try:
            await self._execute_order(order_id)
        except Exception:  # noqa: BLE001 — worker loop must survive any single-order failure
            logger.exception("algo execute_order %s failed", order_id)
            try:
                await self._reject_new(order_id, "internal error")
            except Exception:  # noqa: BLE001
                logger.exception("algo execute_order %s: reject-on-error also failed", order_id)

    async def _execute_order(self, order_id: int) -> None:
        async with session_scope() as db:
            row = (await db.execute(select(AlgoOrder).where(AlgoOrder.id == order_id))).scalar_one_or_none()
        if row is None or row.status != "new":
            return  # already resolved (idempotent re-claim) or gone

        broker = build_broker(self._settings.algo_broker, self._settings)
        quote = (await broker.mark([row.symbol])).get(row.symbol)
        if quote is None:
            await self._reject_new(order_id, "unpriceable symbol")
            return

        if row.type == "limit":
            if not limit_crosses(row.side, quote, row.limit_price):
                await self._accept_resting(order_id, row.symbol)
                return
            await self._settle(row, row.limit_price, 0.0, "new")
        else:
            price = fill_price(quote, row.side, self._settings.algo_slippage_bps)
            await self._settle(row, price, self._settings.algo_slippage_bps, "new")

    async def check_resting_limits(self) -> None:
        async with session_scope() as db:
            rows = (await db.execute(select(AlgoOrder).where(AlgoOrder.status == "accepted"))).scalars().all()
        if not rows:
            return
        marks = await self._mark_symbols([r.symbol for r in rows])
        for row in rows:
            quote = marks.get(row.symbol)
            if quote is None or not limit_crosses(row.side, quote, row.limit_price):
                continue
            try:
                await self._settle(row, row.limit_price, 0.0, "accepted")
            except Exception:  # noqa: BLE001 — one bad symbol must not kill the sweep
                logger.exception("algo check_resting_limits settle failed for order %s", row.id)

    async def mark_and_snapshot(self) -> None:
        try:
            async with session_scope() as db:
                cash = await self._compute_cash(db)
                pos_rows = (await db.execute(select(AlgoPosition).where(AlgoPosition.qty != 0))).scalars().all()
            marks = await self._mark_symbols([r.symbol for r in pos_rows])
            positions_value = sum(marks.get(r.symbol, r.avg_cost) * r.qty for r in pos_rows)
            equity = cash + positions_value
            today = datetime.now(timezone.utc).date()
            async with session_scope() as db:
                stmt = pg_insert(AlgoAccountSnapshot).values(
                    snapshot_date=today, cash=cash, positions_value=positions_value, equity=equity,
                ).on_conflict_do_update(
                    index_elements=[AlgoAccountSnapshot.snapshot_date],
                    set_={"cash": cash, "positions_value": positions_value, "equity": equity},
                )
                await db.execute(stmt)
                await db.commit()
        except Exception:  # noqa: BLE001 — worker loop must survive a snapshot failure
            logger.exception("algo mark_and_snapshot failed")

    async def heartbeat(self, worker_id: str) -> None:
        try:
            await self._redis.set(_HEARTBEAT_KEY, json.dumps({
                "worker_id": worker_id, "ts": datetime.now(timezone.utc).isoformat(),
            }))
        except Exception as exc:  # noqa: BLE001
            logger.debug("algo heartbeat write failed: %s", exc)

    # ── internal helpers ───────────────────────────────────────
    async def _settle(self, row: AlgoOrder, price: float, slippage_bps: float, from_status: str) -> None:
        """Shared cash/position check + atomic fill transition, used both by a
        fresh market/limit-cross fill (from_status='new') and a resting limit
        crossing later (from_status='accepted'). ONE transaction inserts the
        fill, upserts the position, and moves the order to 'filled' — or, on a
        failed check, moves it straight to 'rejected' with `reason` set."""
        commission = self._settings.algo_commission_per_order
        async with session_scope() as db:
            if not notional_ok(row.qty, price, self._settings.algo_max_order_notional):
                reject, reason = True, "notional exceeds limit at fill"
            elif row.side == "buy":
                cash = await self._compute_cash(db)
                reject = cash < row.qty * price + commission
                reason = "insufficient cash"
            else:
                pos = (await db.execute(
                    select(AlgoPosition).where(AlgoPosition.symbol == row.symbol)
                )).scalar_one_or_none()
                reject = pos is None or pos.qty < row.qty
                reason = "insufficient position"

            if reject:
                claimed = (await db.execute(
                    update(AlgoOrder).where(AlgoOrder.id == row.id, AlgoOrder.status == from_status)
                    .values(status="rejected", reason=reason).returning(AlgoOrder.id)
                )).scalar_one_or_none()
                await db.commit()
                if claimed is None:
                    return  # concurrently resolved already — no-op (single-worker: shouldn't happen)
                await self._publish_event({
                    "type": "order", "id": row.id, "symbol": row.symbol,
                    "status": "rejected", "reason": reason,
                })
                return

            claimed = (await db.execute(
                update(AlgoOrder).where(AlgoOrder.id == row.id, AlgoOrder.status == from_status)
                .values(status="filled").returning(AlgoOrder.id)
            )).scalar_one_or_none()
            if claimed is None:
                return  # concurrently resolved already — no-op (single-worker: shouldn't happen)

            db.add(AlgoFill(
                order_id=row.id, symbol=row.symbol, side=row.side, price=price, qty=row.qty,
                commission=commission, slippage_bps=slippage_bps,
            ))
            await self._apply_fill_to_position(db, row.symbol, row.side, row.qty, price)
            await db.commit()

        await self._publish_event({
            "type": "order", "id": row.id, "symbol": row.symbol, "status": "filled", "reason": None,
        })

    async def _apply_fill_to_position(self, db: AsyncSession, symbol: str, side: str, qty: float, price: float) -> None:
        pos = (await db.execute(select(AlgoPosition).where(AlgoPosition.symbol == symbol))).scalar_one_or_none()
        if pos is None:
            if side == "sell":
                return  # guarded above (insufficient position) — never invent a short
            db.add(AlgoPosition(symbol=symbol, qty=qty, avg_cost=price))
            return
        if side == "buy":
            pos.avg_cost = new_avg_cost(pos.qty, pos.avg_cost, qty, price)
            pos.qty = pos.qty + qty
        else:
            pos.qty = pos.qty - qty
            # avg_cost unchanged on a sell; qty -> 0 keeps the row (audit trail)

    async def _reject_new(self, order_id: int, reason: str) -> None:
        async with session_scope() as db:
            res = await db.execute(
                update(AlgoOrder).where(AlgoOrder.id == order_id, AlgoOrder.status == "new")
                .values(status="rejected", reason=reason).returning(AlgoOrder.symbol)
            )
            symbol = res.scalar_one_or_none()
            await db.commit()
        if symbol is not None:
            await self._publish_event({
                "type": "order", "id": order_id, "symbol": symbol, "status": "rejected", "reason": reason,
            })

    async def _accept_resting(self, order_id: int, symbol: str) -> None:
        async with session_scope() as db:
            res = await db.execute(
                update(AlgoOrder).where(AlgoOrder.id == order_id, AlgoOrder.status == "new")
                .values(status="accepted").returning(AlgoOrder.id)
            )
            resolved = res.scalar_one_or_none()
            await db.commit()
        if resolved is not None:
            await self._publish_event({
                "type": "order", "id": order_id, "symbol": symbol, "status": "accepted", "reason": None,
            })

    async def _compute_cash(self, db: AsyncSession) -> float:
        """Cash = starting cash - buys + sells - commission, from ONE aggregate
        query over `algo_fills` (never re-derived from `algo_positions`)."""
        buys, sells, commission = (await db.execute(
            select(
                func.coalesce(func.sum(case((AlgoFill.side == "buy", AlgoFill.qty * AlgoFill.price), else_=0.0)), 0.0),
                func.coalesce(func.sum(case((AlgoFill.side == "sell", AlgoFill.qty * AlgoFill.price), else_=0.0)), 0.0),
                func.coalesce(func.sum(AlgoFill.commission), 0.0),
            )
        )).one()
        return float(self._settings.algo_starting_cash) - float(buys) + float(sells) - float(commission)

    async def _mark_symbols(self, symbols: list[str]) -> dict[str, float]:
        if not symbols:
            return {}
        broker = build_broker(self._settings.algo_broker, self._settings)
        return await broker.mark(sorted(set(symbols)))

    async def _set_kill_state(self, on: bool) -> None:
        try:
            await self._redis.set(_KILL_KEY, "1" if on else "0")
        except Exception as exc:  # noqa: BLE001 — DB mirror below still lands
            logger.debug("algo kill redis write failed: %s", exc)
        async with session_scope() as db:
            stmt = pg_insert(AlgoState).values(key="kill", value={"on": on}).on_conflict_do_update(
                index_elements=[AlgoState.key], set_={"value": {"on": on}},
            )
            await db.execute(stmt)
            await db.commit()

    async def _nudge(self) -> None:
        try:
            await self._redis.publish(_NUDGE, "1")
        except Exception as exc:  # noqa: BLE001
            logger.debug("algo nudge publish failed: %s", exc)

    async def _publish_event(self, event: dict[str, Any]) -> None:
        try:
            await self._redis.publish(_EVENTS, json.dumps(event))
        except Exception as exc:  # noqa: BLE001
            logger.debug("algo event publish failed: %s", exc)
