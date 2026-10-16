# Signal self-scoring core (Phase 9, deliverable #1)

The honest heart of a backtester, built **before** the VectorBT engine: log emitted
trading/quant signals and grade them against **realised forward returns**, computed
(never fetched) from the F0 daily-history backbone. No new data source.

Read this before touching `backend/app/services/signals.py`,
`backend/app/routers/signals.py`, or the signal widgets.

## The model

A **signal** is a continuous, sign-normalised **cross-sectional score** at a decision
date: *"at `as_of_date` T, `symbol` scored `value`"* within a `signal_id` family
(higher value ⇒ higher expected forward return). It is **not** a trade, a target
weight, or a P&L — that is the engine's job (deliverable #5). One row per
`(signal_id, as_of_date, symbol)`, `UNIQUE` → re-emitting a cross-section is an
idempotent upsert.

Table `signals` (Alembic `0005`): `id` (Integer PK), `signal_id` (≤64), `as_of_date`
(Date), `symbol` (≤32, **F0-priceable** via `(symbol, source)`), `source` (≤32),
`value` (DOUBLE — a score, not money), `meta` (JSONB, emission params, never read by
the grader), `created_at`. Grades are **never stored** — see below.

## The lookahead rule (the cardinal invariant)

A backtester's one unforgivable sin is lookahead. The maturation rule lives entirely
in `_forward(dates, closes, T, h, settle)`:

- **entry** = the first F0 bar with `bar_date STRICTLY > T` (you decide at T's close
  but can only act on the *next* bar). Strict `>`, never `>=`.
- **exit** = `entry + h` by **per-symbol trading-bar position** (so weekends/holidays
  and the 5-day-equity vs 7-day-crypto calendars each use their own spacing). `h` is
  in trading bars, not calendar days.
- **matured** only if the exit bar exists **and is settled**: `(n-1 - exit_idx) ≥
  signal_settle_days`. `history.py::_backfill` finalises provisional closes via
  `on_conflict_do_update` ("Re-fetched bars may finalise a provisional close"), so the
  tail bar is untrustworthy — never grade off it. `< today` alone is **not** enough.
- `fwd_return = close[exit] / close[entry] − 1`.
- Not-yet-matured rows are **pending**, missing-bar rows **uncovered** — both are
  **counted, never silently dropped**, and never enter any aggregate.

The signal value is fixed at emission and stored, so the grader can't recompute it
from future data. The reference generators are themselves lookahead-clean: momentum at
T uses `closes[i-skip] / closes[i-skip-lb]` with `i = last bar ≤ T`; low-vol uses the
window ending at `i`. Unit test: `backend/tests/test_signals_scoring.py`.

## The grade

Lazy + pure (recomputed from F0 so a derived number can never go stale against a
restated bar), cached in Redis. Per `signal_id`, given `horizon` H and `quantiles` Q:

- **Primary — cross-sectional Rank-IC**: per date T, `RankIC_T = corr(rank(value_i),
  rank(fwd_i))` over the names with a matured window (`pandas .rank(method="average")`
  + `numpy.corrcoef` — Spearman with no scipy; **not** copied from risk.py, which has
  no rank code). Aggregated over the N graded dates: `ICIR = mean(RankIC_T) /
  std(RankIC_T, ddof=1)`, `t = ICIR·√N`, two-sided p via `math.erfc(|t|/√2)`,
  `significant = |t| ≥ 2 AND not low_confidence`. Cross-sectional-then-time-aggregated,
  **never pooled** (pooling understates SE under contemporaneous cross-sectional
  correlation and fakes significance).
- **Economic check** — quintile (Q-bucket) **long/short spread** (top−bottom mean fwd
  return, demeaned by construction), its t-stat, **monotonicity** (corr of bucket index
  vs bucket mean return), and the **cumulative L/S equity** (`∏(1+spread)−1`).
- **Secondary** — **hit-rate** (per-date median-demeaned sign agreement) with a
  **Wilson 95% lower bound** (small-N honesty); long vs short basket mean fwd return.
- **Pooled fallback** (`methodology = "pooled (non-independent; indicative only)"`) when
  no date clears `signal_min_names` breadth — loudly flagged, never substituted silently.
- **Honesty flags**: `low_confidence` when `N_dates < signal_min_dates`;
  `overlapping_windows` warning when H exceeds the decision-date spacing (autocorrelated
  IC → anti-conservative t); and for the seeded `xs_*` demo, **survivorship** (today's
  WF book applied to past dates) + **lumpy-universe** (tiny, BTC-dominated → descriptive,
  not research-grade edge) warnings ride the Envelope.

Caching mirrors the risk bundle: key `signal:grade:{id}:{H}:{Q}:{signature}:{today}`
where `signature = count:max(as_of_date):max(created_at)` (busts on re-seed) and the
`today` suffix rolls the grade daily; 6h TTL, ≤300s self-heal on a transient F0
rate-limit; defensive deserialise.

## The reference seed (real factors over the WF book)

`POST /signals/seed` (gated by `SIGNAL_SEED_ENABLED`) runs two real factors over the
Wealthfolio universe — resolved exactly as the risk lens resolves it
(`RiskEngine._resolve` + `RISK_SYMBOL_MAP`, skip set) — on a monthly decision grid
anchored to the `^GSPC` trading calendar, H=21 (non-overlapping → honest t):

- `xs_mom_252_21` — 12-1 cross-sectional momentum (`close[t-21]/close[t-273]−1`,
  z-scored). Example run: significant RankIC / ICIR / t-stat, positive L/S spread.
- `xs_lowvol_63` — negative trailing-63-bar return stdev (low-vol anomaly, z-scored).
  Example run: weak RankIC / ICIR, **not significant**, negative L/S spread.

Two factors prove the board **discriminates** a real signal from a weak one rather
than rubber-stamping. **Decision-date depth is bounded by F0 history depth for the
benchmark calendar** — if `^GSPC` is shallow in `daily_bars` (a rate-limited burst can
truncate it), the grid is short; deepen it with a direct
`/prices/%5EGSPC/history?from=…` fetch, bust `history:*` + `signal:*` Redis keys, and
re-seed.

## Emission contract (the generic ingress)

`POST /api/v1/signals` — `SignalIn | list[SignalIn]`, idempotent upsert; this is where
Phase-9 strategies and Phase-11 scanners will log. `symbol` must be F0-priceable via
`(symbol, source)`. Batch capped at 50 000 rows. `value` must be finite.

## Routes (`routers/signals.py`, all `/api/v1`)

| Route | Purpose |
|---|---|
| `POST /signals` | emit one/many (idempotent upsert) |
| `GET /signals` | catalog — `signal_id`s + row/symbol/date counts |
| `POST /signals/seed` | run the reference generators (gated) |
| `GET /signals/{id}/grade?horizon=&quantiles=&refresh=` | the grade (Envelope) |
| `GET /signals/scoreboard?horizon=&quantiles=` | leaderboard, sorted by ICIR |
| `GET /signals/{id}/cross-section?as_of=` | raw audit view of one date (lookahead visible) |

Computed responses are `Envelope[T]` so the widgets render provenance + the
honesty warnings.

## Frontend (Signals tab)

`SignalScoreboard.tsx` (leaderboard + a one-click "Seed reference factors" button) and
`SignalGradeCard.tsx` (selector → ICIR/t stats, per-date Rank-IC bars, quintile
monotonicity bars, long/short split, lookahead-methodology footer). Both light,
self-registering, category `Signals`; the tab seeds the default `Daily` workspace
(`workspaces.ts` **v4**); palette code `SIGNALS`.

## Config (`SIGNAL_*`, see `.env.example`)

`SIGNAL_FETCH_CONCURRENCY`, `SIGNAL_DEFAULT_HORIZON_BARS`, `SIGNAL_DEFAULT_QUANTILES`,
`SIGNAL_MIN_NAMES`, `SIGNAL_MIN_DATES`, `SIGNAL_SETTLE_DAYS`, `SIGNAL_SEED_MONTHS`,
`SIGNAL_MOMENTUM_LOOKBACK_BARS`, `SIGNAL_MOMENTUM_SKIP_BARS`,
`SIGNAL_LOWVOL_LOOKBACK_BARS`, `SIGNAL_GRADE_TTL_SECONDS`, `SIGNAL_SEED_ENABLED`.

## How to add a signal

POST scores to `/api/v1/signals` (any external producer), or add a generator in
`services/signals.py` alongside `_build_reference_signals`. Store the **F0-priceable**
symbol + source. The grader is universe-agnostic — the same code scores a Phase-11
S&P-wide cross-section a scanner POSTs. The scope deliberately stops at score→grade so
the Phase-9 VectorBT engine extends the `signals` table without rework.
