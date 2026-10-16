# Equity Research & Screening (Phase 10b)

Single-name fundamental deep-dive + a universe screen. Read this before touching
`services/research.py`, `adapters/fundamentals_adapter.py`, or `routers/research.py`.

## Architecture

```
yfinance ── FundamentalsAdapter ──┐
                                  ├─ ResearchEngine ── routers/research.py ── /api/v1/research/*
F0 daily_bars ── HistoryStore ────┘   (pure math)
```

- **FundamentalsAdapter** (`adapters/fundamentals_adapter.py`): statements +
  profile from yfinance, normalised + Redis-cached. See `skills/data-adapters.md`.
- **ResearchEngine** (`services/research.py`): pure analytics. Statements come
  pre-cached from the adapter; price (market value + technicals) from the F0
  backbone via `HistoryStore` + `session_scope()` (the risk.py read pattern). Each
  public method returns `(PydanticModel, meta)`; the router wraps `Envelope`.
- Reached only behind the FastAPI boundary; the frontend calls `/api/v1/research/*`.
- **No new compose profile / service / dep** — yfinance is already the F0 dep. The
  OpenBB `equity` router was deliberately NOT mounted (it wraps the same yfinance,
  bigger image, extra hop). Decision logged in STRATEGY Phase 10b.

## Endpoints

| Route | Returns |
|---|---|
| `GET /research/{symbol}/fundamentals` | income / balance / cashflow by fiscal year + derived margins/ROE/ROA |
| `GET /research/{symbol}/valuation` | price, market cap, P/E P/B P/S EV/EBITDA FCF-yield Div-yield ROE ROA |
| `GET /research/{symbol}/scores` | Piotroski F / Altman Z / Beneish M with per-component breakdown |
| `GET /research/{symbol}/technicals` | last, SMA50/200, dist-from-200dma, 52w range, RSI-14, trend + a price series |
| `GET /research/screen` | the config universe scored, filtered by typed query params |

`symbol` is constrained at the router by `Path(pattern=r"^[A-Za-z0-9.\-^=]+$",
max_length=24)` (yfinance tickers use `. - ^ =`). Screen filters are **explicit
typed Query params** — no arbitrary dict from the client reaches the engine.

## The three forensic scores (pure, reproducible)

All computed in Python so every number is auditable (Phase 12's LLM only narrates
these facts, never invents them). When a statement line item is missing, the
dependent component is `passed=None` / the ratio `None` — never silently zero — and
a warning is surfaced.

### Piotroski F-Score (0–9, higher = stronger)
Nine binary tests over the two most recent fiscal years (t, t-1). Profitability:
positive ROA, positive OCF, ΔROA > 0, OCF > net income (accruals). Leverage/liquidity:
ΔLTD/assets < 0, Δcurrent-ratio > 0, no share dilution. Efficiency: Δgross-margin > 0,
Δasset-turnover > 0. **Convention:** ROA / asset-turnover use *current-year* total
assets (a reproducible simplification of Piotroski's average-assets original) — noted
in the component detail.

### Altman Z-Score (original manufacturing model)
`Z = 1.2·X1 + 1.4·X2 + 3.3·X3 + 0.6·X4 + 1.0·X5` where X1=working capital/assets,
X2=retained earnings/assets, X3=EBIT/assets, X4=market value of equity/total
liabilities, X5=sales/assets. **Market value of equity** = market cap (yfinance
`info.marketCap`, else shares × F0 close, listing currency). Bands: **Z > 2.99 safe ·
1.81–2.99 grey · < 1.81 distress**. (The Z'' EM/non-manufacturing variant is a future
refinement; the original Z is the canonical surface.)

### Beneish M-Score (8-variable earnings-manipulation model)
`M = −4.84 + 0.92·DSRI + 0.528·GMI + 0.404·AQI + 0.892·SGI + 0.115·DEPI − 0.172·SGAI
+ 4.679·TATA − 0.327·LVGI`, each ratio over (t, t-1). EBITDA for DEPI uses
depreciation = income `Reconciled Depreciation` else cashflow `Depreciation And
Amortization`; TATA = (net income − operating cash flow) / total assets. **M > −2.22
⇒ elevated manipulation risk.**

## Screening

`screen(filters)` evaluates `research_universe_map[:research_screen_max_symbols]`
with bounded concurrency (`research_fetch_concurrency` Semaphore), Redis-caches the
scored rows for the day (`research_cache_ttl_seconds`), then applies the filters in
plain Python. A row whose metric is `None` is excluded by any *binding* filter on
that metric. Filter keys: `min_f_score, min_z_score, max_m_score, max_pe, min_pe,
max_pb, max_ps, min_div_yield, min_roe, min_market_cap`.

## Config (env, all overridable — no hardcoding)
`FUNDAMENTALS_CACHE_TTL_SECONDS`, `RESEARCH_CACHE_TTL_SECONDS`,
`RESEARCH_FETCH_CONCURRENCY`, `RESEARCH_HISTORY_LOOKBACK_DAYS`,
`RESEARCH_SCREEN_MAX_SYMBOLS`, `RESEARCH_UNIVERSE` (`SYM:Label,…`).

## Honest limitations
- yfinance fundamentals are the free tier: occasional gaps / restatements; ETFs &
  crypto have no statements (the scores degrade to "incomplete data", flagged).
- Valuation ratios are point-in-time (no historical percentile band yet — a future
  enhancement mirroring the 10a CAPE percentile).
- The screen universe is a curated large-cap default; widen via `RESEARCH_UNIVERSE`.
