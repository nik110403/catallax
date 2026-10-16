# Portfolio Risk analytics (Catallax, Phase 8.5)

The Risk lens turns the holdings you already track into derived analytics —
**computed, not fetched**. No new data source: holdings come from
`WealthfolioBridge.holdings()`, daily prices from the F0 `HistoryStore`
(Postgres-backed, idempotent yfinance backfill). Engine: `services/risk.py`
(`RiskEngine`); routes: `routers/risk.py`. Everything is EUR.

## Pipeline

```
WF holdings (EUR weights)            F0 daily history (per symbol, via HistoryStore)
        │                                   │
        ▼                                   ▼
  resolve symbol → (price_symbol, source, quote_ccy)   ──►  EUR price series
        │                                                   (price_local / EUR{CCY}=X)
        ▼                                                       │
  weights + coverage           align to ^GSPC trading calendar  ▼
        └──────────────►  SIMPLE EUR return matrix R (T×N) ◄────┘
                                   │
   vol · beta · maxDD · HHI · VaR/CVaR · risk-contrib · fx-drag · stress
```

The whole computed result is one **bundle** cached in Redis (key = holdings
sha256 + lookback + today's date, `RISK_CACHE_TTL_SECONDS`=6h). All four
endpoints slice the same bundle, so `/xray`, `/var`, `/fx-drag` share one build.

## Symbol resolution (config, never an exporter field)

The WF exporter gives **no native currency** per holding, so resolution +
currency are explicit config — `RISK_SYMBOL_MAP` (same colon/comma idiom as
`portfolio_commodity_map`): `WF_SYMBOL:price_symbol:source:quote_currency`.

Precedence: **override** (the map) → **skip-list** (`RISK_SKIP_SYMBOLS`,
stablecoins/junk; excluded from the return matrix but still counted toward
value) → **rules**: `CRYPTO → {SYM}-USD / yfinance / USD`, `EQUITY → bare /
yfinance / USD`. EU/CH/UK listings (their `.L/.DE/.SW/.PA` suffix + quote ccy
can't be inferred from the bare exporter symbol) **must** be in the map —
verified: `VOD→VOD.L`(GBP), `SIE→SIE.DE`(EUR), `NOVN→NOVN.SW`(CHF),
`SAP→SAP.DE`(EUR), `AIR→AIR.PA`(EUR). `currency_source` (`mapped` vs
`inferred-default`) is surfaced so the UI never presents an inferred split as
authoritative. **quote_currency is the currency the PRICE SERIES is denominated
in** (drives the EUR conversion); for index ETFs this is the listing currency,
not the look-through economic currency (a labelled assumption).

For an explicit `yfinance` override, both its source and `price_symbol` are
authoritative native Yahoo values. Risk marks the adapter native and leaves
canonical history storage to `HistoryStore`, whose Yahoo key resolution also
bypasses aliases for a deliberate bare mapping such as `SOL:SOL:yfinance:USD`.
It therefore remains Yahoo `SOL` even when the holding class is `CRYPTO`.
Unmapped crypto holdings still resolve to their normal Yahoo USD pairs.

## Return construction

- Lookback `RISK_LOOKBACK_DAYS` (504 calendar ≈ 1y trading). All reads via
  `HistoryStore` (Redis/Postgres cached; only cold symbols hit yfinance).
- **EUR conversion:** `EUR{CCY}=X` quotes CCY-per-EUR → `price_eur =
  price_local / fx`. FX forward-filled across holidays, bounded
  (`RISK_FX_FFILL_MAX_DAYS`).
- **Master calendar = the equity benchmark (^GSPC) trading days.** Every series
  is reindexed onto it → crypto weekend bars drop (no spurious equity returns),
  every asset's date-`t` return is a same-calendar move.
- **Two-tier coverage gate:** an asset needs `≥ RISK_MIN_HISTORY_DAYS` aligned
  returns to enter vol/beta/VaR (dropped ones stay in concentration/exposure
  weights). `coverage.covered_weight_pct` + `by_reason` are always reported so
  partial coverage is honest. Cash (not in `by_asset`) = a zero-vol EUR position.

## The math (judge-panel synthesis)

- **SIMPLE returns** for vol/beta/maxDD/VaR — so portfolio P&L `Σ value_i·r_i`
  and historical-simulation VaR are *exact*. **LOG returns ONLY in fx-drag**
  (below), where the split is additive.
- **Volatility:** `std(r_p, ddof=1) · √RISK_TRADING_DAYS`.
- **Beta:** OLS of `r_p` on a **composite benchmark** `r_b = (w_eq·GSPC_EUR +
  w_cr·BTC_EUR)/(w_eq+w_cr)` weighted by the book's own covered equity/crypto
  shares (a crypto-heavy book is poorly described by the S&P alone). Aux
  `beta_vs_sp500`, `beta_vs_btc`, plus R²/alpha.
- **Max drawdown:** from the `(1+r_p).cumprod()` wealth curve + peak/trough/recovery dates.
- **Concentration:** HHI (`Σ w²`), `effective_n = 1/HHI`, top-1/3/5, per-class —
  over current EUR value weights incl. cash (covers 100% of value).
- **VaR/CVaR (historical simulation):** empirical quantile of the **current
  weights applied to historical EUR P&L** (uncovered + cash = 0). 95% headline +
  99% + a labelled **Gaussian** comparison + `fat_tail_ratio = CVaR/VaR` to
  expose crypto tails. 1-day; `low_confidence` when `n_scenarios <
  RISK_VAR_MIN_SCENARIOS`.
- **Risk contributors:** pairwise-complete covariance + **PSD eigenvalue-clip
  repair** (a ragged 50-asset matrix is routinely non-PSD), contribution `w·(Σw)`.
- **FX drag:** per asset `r_eur = r_local − r_fx` (LOG, additive by construction
  on a single master-aligned `eur = local/fx`; a reconstruction residual >1e-6
  warns — an FX-convention guard). Portfolio split `EUR = local + fx_drag`
  (cross-term reported, ~0 live), broken down by currency. EUR holdings bear 0 drag.
- **Stress (`/risk/stress`):** static class-proxy shocks (`RISK_STRESS_SCENARIOS`:
  class + FX-overlay per scenario) on current weights — works despite shallow
  crypto/ETF history; classes that didn't exist in a window are flagged
  `synthetic`. Behind `RISK_STRESS_ENABLED`.

## Concurrency + caching

Per-series history reads run via **`session_scope()` (one short-lived
AsyncSession each)**, NOT the shared request session — concurrent reads on one
asyncpg session raise "another operation in progress". Bounded by
`RISK_FETCH_CONCURRENCY` (under the yfinance rate limit; a single rate-limited
symbol degrades to `unresolved`, never 500s). The numpy/pandas compute runs in
`asyncio.to_thread`. First cold call backfills (slow, rate-limited); thereafter
`daily_bars` + the 6h bundle cache make it fast.

## Endpoints (all `Envelope[T]` provenance)

`GET /risk/xray` · `GET /risk/var` · `GET /risk/fx-drag` · `GET /risk/stress`.
Any stale upstream series → `Envelope.stale=true`; coverage caveats ride in `warnings`.

## Limitations / honest assumptions

- **No exporter currency** → resolution/currency are config inferences (the
  `assumption` string says so). FX-drag uses the **listing currency**; the
  authoritative fix is to extend the WF exporter `/holdings` with a native-currency
  field (out-of-repo) — then FX-drag look-through becomes exact.
- Coverage is typically below 100% of value (a few obscure tokens / too-new listings
  drop). The headline metrics state the covered weight.

## Adding a metric / widget

1. Add the computation to `_compute` in `services/risk.py` (pure, in the thread).
2. Extend the bundle dict + the relevant Pydantic model; slice it in an engine method.
3. Add the route (`Envelope[Model]`), the client method + types, and a `Risk`-category widget.
4. Update this file.
