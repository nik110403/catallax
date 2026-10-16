# Backtester — engine framework + off-API job lifecycle

Read this before touching anything under `backend/app/adapters/backtest/`,
`backend/app/services/backtest.py`, `backend/app/worker.py`, or the Backtest tab.

The backtester is a **framework, not a single engine** (STRATEGY invariant: "a
backtesting framework"). A `BacktestEngineAdapter` ABC + factory mirror
`PriceSourceAdapter`; engine #1 (`vectorized`) is a reimplemented numpy/pandas
cross-sectional engine; `vectorbt` is a registered drop-in stub (engine #2) that
proves the seam. The **strategy is declarative** — it references a logged
`signal_id` (the join surface to the signal self-scoring core); there is **no code
execution** (a Python-upload sandbox is an unacceptable RCE surface on a no-auth LAN).

## The strategy (what engine #1 does)

A `BacktestConfig` = `{signal_id, universe?, start, end, rebalance(W/M/Q),
quantile, long_short, weighting(equal/score), costs_bps, benchmark_symbol?,
rf_annual}`. Each rebalance date T (the signal's own `as_of_date`s, downsampled to
the cadence):

1. Rank the logged cross-section of `signal_id` at T (higher score = more bullish).
2. Long the top `1/quantile` bucket (short the bottom iff `long_short`).
3. **Enter on the first bar STRICTLY after T** — `bisect.bisect_right(master, T)`,
   the exact primitive the signal grader (`services/signals.py::_forward`) uses.
   **This is the single source of truth for entry timing — never write a second
   one.** The rebalance bar's own close is never traded on.
4. Hold to the next rebalance's entry (fixed target weights between rebalances =
   daily-rebalanced-to-basket; portfolio daily return = weighted mean of held
   names' daily returns).
5. Charge `costs_bps` on the **two-way** turnover `Σ|w_new − w_prev|` on the entry
   bar (reported turnover is one-way = ½ of that).
6. **Exclude the unsettled tail**: `last_settled = n−1−signal_settle_days`; a
   rebalance whose entry > `last_settled` is `pending` and dropped from the equity
   curve + settled metrics + IC (daily_bars finalises provisional closes via
   `on_conflict_do_update`, so an unsettled tail bar can restate). Same maturation
   rule as the grader.

Prices are aligned on a master calendar (union of all panel dates) with a bounded
forward-fill (`pandas.ffill`), so a non-trading day for one name contributes a 0
return, not a gap. Reads come from the F0 backbone (`HistoryStore.get_daily`) — no
new data source.

### Metric bundle (pinned — `vectorized.py::_metrics`)
`cum_return = equity[-1]−1` · `ann = equity[-1]^(252/n_days)−1` · `ann_vol =
std(daily, ddof=1)·√252` · `sharpe = (mean(daily)−rf/252)/std·√252` ·
`info_ratio = mean(active)/std(active)·√252` (active = port − benchmark daily;
**null + warning** if no benchmark) · `max_drawdown = min(equity/cummax−1)` ·
`turnover_annualised` (one-way, total traded/years) · `mean_ic` = mean
cross-sectional Rank-IC of score vs realised held return, **reusing the grader's
`_rank_ic`** (injected, not reimplemented). Benchmark defaults to a **synthetic
equal-weight-universe** buy-and-hold (loudly warned); set `benchmark_symbol` for a
real one.

### Honesty caveats (NEVER buried)
Computed and surfaced on `report.warnings` + `Envelope.warnings` (rendered as
amber chips): thin cross-section, single-name dominance (BTC-heavy book),
today's-holdings-on-past-dates survivorship (any `xs_` signal over the WF book),
synthetic benchmark, dropped unsettled tail, long/short borrow-not-modelled, stale
F0 series. An unqualified Sharpe over a tiny book is dishonest.

## Off-API job lifecycle (never blocks the main API)

`backtests` table (migration `0006`) is BOTH the job record and the result (1:1).
`job_id` is a **server-minted UUID** — the public route handle + the Redis progress
channel (so a client can't subscribe-hijack another job's stream).

- **API path** (`routers/backtests.py` + `BacktestService`): `POST /backtests`
  runs **pre-flight `validate()`** (structured field errors → 400 `PREFLIGHT_FAILED`
  **before** the engine is ever invoked), then INSERTs a `queued` row + `LPUSH`es
  the job id. The handler returns immediately — zero compute in the request path.
- **Worker** (`python -m app.worker`, compose service `backtester`, `--profile
  backtest`, same `./backend` image, default network, no host port): a non-blocking
  `LMOVE` reliable queue (queue → processing list) + `Semaphore(MAX_CONCURRENCY)`.
  Per job: **atomic claim** (`UPDATE … WHERE status='queued' RETURNING` — two
  workers can't double-run) → heartbeat task → READ-ONLY F0 gather (own
  `session_scope()` per series, `TRANSIENT` 429/502 fails the job rather than
  backtesting over a holey panel) → engine in `asyncio.to_thread` → persist result.
  **Crash recovery**: boot-time `recover()` requeues orphaned processing-list ids +
  `running` rows with a stale heartbeat. The processing entry is always `LREM`'d.
- **F0 SAFETY**: the worker writes ONLY the `backtests` table — no write path to
  `signals`; it adds no new `daily_bars` write beyond the shared idempotent
  `HistoryStore` backfill. A worker bug can't corrupt the backbone.
- **SSE** (`GET /backtests/{id}/stream`): subscribes the Redis channel
  `catallax:bt:progress:{id}`, emits current DB state first (subscribe-before-read
  closes the completion race), then live deltas, a `: ping` heartbeat every 15s, and
  terminates on done/failed. `X-Accel-Buffering: no` lets nginx stream it with no
  config change. Intra-compute progress bridges the engine thread → loop via
  `loop.call_soon_threadsafe`.

Routes: `POST /backtests`, `GET /backtests` (list), `GET /backtests/{id}`,
`GET /backtests/{id}/report` (`Envelope[BacktestReport]`), `GET /backtests/{id}/stream`.

## Adding a new engine

1. Subclass `BacktestEngineAdapter` (`adapters/backtest/base.py`): implement
   `run(config, panels, signal_rows, benchmark_panel, settle_days, rank_ic,
   progress)` — **pure compute, no I/O** (the worker does the gather + hands frozen
   numpy panels in; `rank_ic` is injected so the adapter layer never imports the
   service layer). REUSE the `bisect_right` entry rule — do not write a second
   timing path.
2. Register it in `adapters/backtest/factory.py::_ENGINES`.
3. **vectorbt note**: it does the accounting well but enforces neither the
   strictly-after-T entry nor the settle-buffer exclusion (you'd hand-roll that glue
   anyway), and its numba/pandas pins risk a resolver fight in the shared `./backend`
   image — so it ships in its OWN worker image when enabled. The stub is the
   documented slot.

## Env

All `BACKTEST_*` in `.env.example` (engine, costs, quantile, window/rebalance/symbol
caps, worker concurrency fetch concurrency, heartbeat/stale crash-recovery
timings, poll interval, result TTL). Jobs flow via Redis, not HTTP — there is no
`BACKTESTER_URL`. Migrations: alembic head `0006`. See `skills/docker-services.md`
for the compose service, `skills/signal-scoring.md` for the join surface.
