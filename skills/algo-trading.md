# Algo Trading Hub — paper-only

Lean in-house paper-trading engine behind a `BrokerAdapter` seam. **No live-money path
exists anywhere** — no broker keys, no wallet, paper table-writes only. Live trading is
gated behind a future dedicated auth/security phase.

## Architecture

```
OrderTicket / StrategyPanel (Algo tab, workspaces v12)
        │  /api/v1/algo/* (nginx → fastapi-backend)
        ▼
routers/algo.py ── validates + INSERTs orders (status='new') ── publishes Redis nudge
        │                                                              │
        ▼                                                              ▼
services/algo.py (AlgoService — the OMS)                trading-engine (--profile algo)
  fill math, positions, derived cash, kill switch,      python -m app.algo_worker
  reconciliation                                        CONTINUOUS tick loop (15s, nudge-woken)
        ▲                                                              │
        └──── services/strategy_runner.py (declarative rebalances) ────┘
```

- **Seam:** `backend/app/adapters/broker/` — `BrokerAdapter` ABC (`mark`, `health`),
  factory keys: `paper` (ships; wraps the F2 price fallback chain), `nautilus` +
  `alpaca-paper` (documented unavailable stubs — the vectorbt-stub pattern; a real venue
  is a drop-in class + one factory registration, zero router changes).
- **Worker:** `app/algo_worker.py` — structurally different from backtester/lab/ai
  (those run finite jobs; this holds live state). Tick: heartbeat → kill check (fail
  CLOSED: a failing kill-state read counts as engaged) → claim `new` orders (single-worker
  model, status-guarded UPDATE prevents double-fill) → fill markets → check resting
  limits → mark + daily `algo_account_snapshots` upsert → run due strategies. Every step
  independently try/except-wrapped — never-crash worker.

## Fill model (honest simulation, not microstructure)

- Market: fills at the current adapter quote ± `ALGO_SLIPPAGE_BPS` **adverse both
  directions** (buy pays more, sell receives less) + `ALGO_COMMISSION_PER_ORDER`.
- Limit: rests (`accepted`); each tick a crossing quote (buy: quote ≤ limit, sell:
  quote ≥ limit) fills **at the limit price**.
- Long-only, sell-to-close, cash-constrained; no margin/shorting.
- Cash is **derived** (never stored): `ALGO_STARTING_CASH − Σbuys + Σsells − Σcommission`
  over `algo_fills`. Equity = cash + Σ(qty × mark). One snapshot row per UTC day.
- Rejections always carry a human-readable `reason`: kill switch, notional cap (checked
  at submit AND re-checked at settle against the post-slippage fill price — a quote
  hiccup can't skip it), insufficient cash (incl. commission), insufficient position,
  unpriceable symbol, flood cap, non-finite qty/limit (NaN/inf hard-rejected at the
  pydantic boundary — a NaN fill would poison the cash SUM permanently).

## Strategies (declarative, NO code execution — Phase-9 decision inherited)

`StrategyConfig` JSONB: `{signal_id, universe[≤50], top_n≤20, weighting equal|score,
rebalance daily|weekly|monthly (calendar-boundary, not elapsed-time), max_position_pct,
max_orders_per_rebalance}`. Pre-flight validated at POST (400 `PREFLIGHT_FAILED`).
Runner reads the latest cross-section from the Phase-9 `signals` table → target weights
→ diff vs positions → orders (sells emitted first and NEVER truncated by the order cap —
de-risking wins; buys truncate). The showcase arc: the same factor is backtested
 → graded live (signal scoring) → paper-traded.

## Kill switch

`POST /algo/kill` → Redis flag `catallax:algo:kill` + DB mirror (`algo_state`, survives a
Redis flush) → new orders rejected at API AND worker, resting limits cancelled
(`reason="kill switch"`), strategy runner halts. Worker treats a failing kill-state read
as engaged (fail-closed). `POST /algo/resume` re-opens. UI: StrategyPanel button
(confirm-gated on engage only) + OrderTicket banner with inline Resume.

## Reconciliation

`GET /algo/reconciliation` replays ALL fills from scratch (independent of
`algo_positions` and of the SQL cash aggregate) → per-symbol qty diff + independently
recomputed cash vs stored (`math.isclose`) → `clean: bool`.

## Routes (`/api/v1`)

`POST/GET /algo/orders` · `DELETE /algo/orders/{id}` · `GET /algo/fills|positions|account`
· `POST/GET /algo/strategies` (+ `/{id}/start|pause`, `DELETE`) · `POST /algo/kill|resume`
· `GET /algo/status` (kill, worker heartbeat freshness, broker) · `GET /algo/reconciliation`
· `GET /algo/stream` (SSE, global order/fill/kill events via Redis pub/sub, infinite).

## Data model (migration `0010`)

`algo_orders` (lifecycle `new → accepted → filled|cancelled|rejected`, `strategy_id`
NULL = manual) · `algo_fills` (price/qty/commission/slippage per fill) · `algo_positions`
(qty/avg_cost, updated in the SAME transaction as the fill) · `algo_strategies` (config
JSONB + status) · `algo_account_snapshots` (daily equity curve) · `algo_state` (kill
mirror). No FK constraints (codebase convention; single-writer worker owns integrity).

## Env (`ALGO_*`, see `.env.example`)

`TICK_SECONDS=15` · `STARTING_CASH=100000` · `SLIPPAGE_BPS=5` · `COMMISSION_PER_ORDER=1.0`
· `MAX_ORDER_NOTIONAL=25000` · `MAX_OPEN_ORDERS=50` · `HEARTBEAT_SECONDS=10` ·
`STALE_SECONDS=120` · `BROKER=paper`.

## Honest limitations

- Quote-based simulation: no order book, no partial fills, no market impact; weekend/
  after-hours "fills" use the last available quote.
- Strategies share the single paper book (no per-strategy sub-accounts in v1).
- Rebalance cadence is calendar-boundary; a crash between order emission and the
  `last_rebalance_at` stamp re-emits next tick (documented v1 tradeoff).
- Single-worker deployment assumed (compose runs one `trading-engine`); the claim model
  is not multi-replica-safe (same as the Phase-9 worker's documented posture).
- Day P&L = change vs the latest prior-day snapshot; null until a second day of history.
