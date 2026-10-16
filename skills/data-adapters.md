# Data Adapters (Catallax)

All price/market data sources sit behind one interface. Route handlers and
services depend only on the interface — swapping or adding a source never
touches consumers. Implementation lands in Phase 2; this is the contract.

## Interface

```python
# backend/app/adapters/base.py
from abc import ABC, abstractmethod
from datetime import datetime
from decimal import Decimal
from pydantic import BaseModel

class Quote(BaseModel):
    symbol: str
    price: Decimal
    currency: str
    as_of: datetime
    source: str            # adapter name, e.g. "yfinance"

class Candle(BaseModel):
    ts: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None

class SymbolMatch(BaseModel):
    symbol: str
    name: str
    asset_type: str        # "equity" | "etf" | "crypto" | ...

class PriceSourceAdapter(ABC):
    name: str              # registry key, e.g. "coingecko"

    @abstractmethod
    async def get_quote(self, symbol: str) -> Quote: ...

    @abstractmethod
    async def get_history(
        self, symbol: str, start: datetime, end: datetime, interval: str
    ) -> list[Candle]: ...

    @abstractmethod
    async def search(self, query: str) -> list[SymbolMatch]: ...
```

All adapters return these normalized models — consumers never see
source-specific payloads.

## Implementations

### yfinance (`adapters/yfinance_adapter.py`)
- Library is blocking → every call wrapped in `asyncio.to_thread(...)`.
- Covers equities/ETFs/FX **and historic crypto** (the free history backbone).
- Symbols pass through unchanged (`AAPL`, `VWCE.DE`) **except** bare crypto
  tickers in `settings.crypto_yf_symbol_set` (`BTC`, `ETH`, `SOL`, …), which
  `_yf_symbol()` maps to Yahoo's USD pair (`BTC` → `BTC-USD`) so they resolve to
  the coin, not a same-named equity. Anything already carrying a `-`/`=`/`.`
  suffix is left untouched. Tunable via `CRYPTO_YF_SYMBOLS` (comma list, env).
- `YFINANCE_SYMBOL_ALIASES` (`SYM=YAHOO,…`, env) is checked first and wins over
  both rules — for suffix-less listings (`SAP=SAP.DE`) and holdings stored under
  a CoinGecko id (`EXMPL-1=EXMPL1-USD`). Bars stay keyed by the original symbol.
- **One mapping, every Yahoo path (2026-09-28).** The rules live in
  `adapters/yahoo_symbols.py::to_yahoo_symbol` — `_yf_symbol()` delegates to it, and
  so do `FundamentalsAdapter._build` (`SAP` statements come from `SAP.DE`) and the
  class-aware resolvers `risk.RiskEngine._resolve` / `report_worker._resolve_symbols`
  (`crypto=True` hint: alias first, else `<SYM>-USD`). Never call `yf.Ticker(raw)`.
  `is_crypto_symbol(sym, settings, asset_class=…)` makes `FundamentalsAdapter.statements`
  skip coins with **no Yahoo call** — it returns empty statements plus
  `not_applicable` (stale=False) instead of the old `HTTP 404 Quote not found for
  symbol: <SYM>`. **Class beats spelling (review 225d5f4):** when the caller knows the
  holding's Wealthfolio class (`statements(sym, asset_class=…)`; the report pack's
  `chapter_holdings` threads it through `ResearchEngine.fundamentals/valuation/scores`)
  that class decides — an equity ticked `SOL` gets real statements from Yahoo `SOL`
  (`to_yahoo_symbol(..., crypto=False)` skips the `CRYPTO_YF_SYMBOLS` lookup; aliases
  still win). Only an unknown class (Research tab lookups) falls back to the ticker
  heuristic: in `CRYPTO_YF_SYMBOLS` or aliased onto a `-USD` ticker. Class `OTHER`
  (the exporter's default for a missing type) counts as unknown. Prices follow the
  same rule: `YFinanceAdapter(..., crypto=crypto_hint(asset_class))` binds the hint
  at construction (`ResearchEngine._price_series`), so an equity `SOL` is never
  re-mapped onto `SOL-USD`. `RISK_SKIP_SYMBOLS` defaults to `USDC` only — aliased
   coins are priced, not skipped. `RiskEngine` passes each holding's class hint to
   yfinance; `HistoryStore` derives the shared canonical key. Equity `SOL` stays
   `SOL`, while FX and benchmark fetches remain class-unknown. A
   `HistoryStore` asks every adapter for a public `history_symbol()` before it
   constructs Redis/DB keys or writes bars. `YFinanceAdapter` resolves this once
   with its class hint; the original request ticker reaches Yahoo, preventing an
   alias chain from remapping an already-canonical storage key. A `RISK_SYMBOL_MAP`
   yfinance override is authoritative: Risk marks its configured `price_symbol` as
   native, so explicit bare `SOL:SOL:yfinance:USD` fetches and stores Yahoo `SOL`
   even if the holding is classed `CRYPTO` and `SOL=SOL-USD` is configured; an
   unmapped crypto `SOL` remains `SOL-USD`.
  `services/markets.py`'s batched `yf.download` uses a fixed list of Yahoo-native
  index/FX tickers and needs no mapping.

### CoinGecko (`adapters/coingecko_adapter.py`)
- `httpx.AsyncClient` against the free REST API.
- Maintains a symbol→coingecko-id map (e.g. `BTC` → `bitcoin`), cached in Redis.
- Crypto only; `asset_type` always `"crypto"`.
- **Reserved for live "new data"** — the Markets-board snapshot
  (`services/markets.py::_fetch_crypto`, current price + 24h change). Historic
  crypto OHLC now comes from yfinance (above), so the free tier's rate budget
  isn't spent on backfill. A 429 on the live snapshot serves the last-good board
  from Redis (`markets:crypto`, long TTL); if none exists the crypto group is
  omitted until the limit clears.

### Stooq (`adapters/stooq_adapter.py`) — equity fallback

- Free EOD CSV, no key. Light last-quote (`/q/l/`) + daily history (`/q/d/l/`).
- Symbol mapping: bare `AAPL` → `aapl.us` (US default); `.`/`^`-qualified symbols
  pass through. `asset_type` = `equity`.
- `search` returns `[]` — Stooq is a **price fallback only**; yfinance owns search.

## Factory & Registration

```python
# backend/app/adapters/factory.py
ADAPTERS: dict[str, type[PriceSourceAdapter]] = {
    "yfinance": YFinanceAdapter,
    "coingecko": CoinGeckoAdapter,
    "stooq": StooqAdapter,
}
```

Default source comes from `PRICE_SOURCE_DEFAULT`; per-request override via
query param where it makes sense. The factory is exposed as the
`get_price_adapter` dependency (see `skills/fastapi-patterns.md`).

### Fallback chains

- With **no** `?source=`, `get_price_adapter` returns a `FallbackAdapter` over
  the configured chain for the default source's asset class
  (`PRICE_FALLBACK_CHAIN_EQUITY` / `_CRYPTO`, comma lists of source ids).
- The wrapper walks the chain: `SymbolNotFound`/`UpstreamError` → skip to next;
  `RateLimitExceeded` (local or upstream 429) → exponential backoff + jitter
  (`PRICE_BACKOFF_BASE_SECONDS`, `PRICE_BACKOFF_MAX_RETRIES`) then next. Fails
  only after every source fails.
- An explicit `?source=<id>` pins a **single** adapter (no fallback — an
  intentional override). Children keep their own cache + rate limit; the wrapper
  only sequences them, and the served `Quote.source` reflects whoever served it.

## How to Add or Swap a Source

1. Implement `PriceSourceAdapter` in `backend/app/adapters/<name>_adapter.py`.
2. Add it to `ADAPTERS` in the factory.
3. Add its env vars to `.env.example` (API key, `<NAME>_RATE_LIMIT_PER_MIN`).
4. Update this file (implementation notes + rate limit section).

Consumers (routes, widgets, cache) require zero changes.

## Rate Limit Convention

- Every adapter declares `<NAME>_RATE_LIMIT_PER_MIN` in `.env.example`.
- Enforcement: Redis-backed token bucket keyed `ratelimit:<adapter-name>`,
  applied inside the adapter base class — handlers never think about it.
- On limit exhaustion the adapter raises `RateLimitExceeded` (a `CatallaxError`)
  → translated to the standard error envelope with HTTP 429.
- Quotes are cached in the `price_cache` table + Redis with per-asset-type TTLs
  so bursts hit cache, not the upstream.
- **Stale-quote fallback (F2 pattern):** every successful quote is also written to
  a longer `quote:stale:<source>:<SYM>` key (`QUOTE_STALE_TTL_SECONDS`, default
  30 min). When the fresh cache has expired and the next fetch raises
  `RateLimitExceeded` (local window or upstream 429), `get_quote` serves the last
  good quote instead of failing the symbol — keeps the WS ticker live through
  CoinGecko's aggressive free-tier limits. Hard failures (`UpstreamError`/
  `SymbolNotFound`) still propagate so a fallback chain advances to the next source.

## Implementation status (Phase 2 — built)

- Base class is a **template method**: public `get_quote`/`get_history`/`search`
  apply the Redis quote cache + fixed-window rate limit; subclasses implement
  `_fetch_quote`/`_fetch_history`/`_search`.
- Rate limiter: Redis `INCR` on `ratelimit:<name>` with a 60s window; the TTL is
  re-asserted whenever missing so a lost `EXPIRE` cannot wedge the window.
- `asset_type` is **adapter-scope** (yfinance→`equity`, coingecko→`crypto`) — it
  drives the two quote-cache TTL buckets and the persisted `price_cache.asset_type`
  label, and is not per-instrument (an ETF/FX via yfinance is labelled `equity`).
- **yfinance**: quote/history via `asyncio.to_thread`; `search` hits Yahoo's
  autocomplete endpoint over httpx (async, no thread).
- **CoinGecko**: symbol→id resolved via the ranked `/search` endpoint; an exact
  symbol match is cached per-symbol (`coingecko:id:<sym>`, 24h); a non-exact
  fallback is used but **not** cached. `get_history` uses `/coins/<id>/ohlc`,
  which derives candle granularity from the day span and **ignores `interval`**
  (and carries no volume) — do not assume parity with the yfinance interval map.

## Price-history store (Foundations F0)

`price_cache` is a **write-only quote log** (one row per `/prices` hit,
`as_of` ≈ now) — wrong shape for quant work. `daily_bars` (migration `0003`) is
the **read backbone**: one OHLC row per `(symbol, source, bar_date)`.

- `services/history.py::HistoryStore.get_daily(adapter, symbol, start, end)`:
  Redis result cache (`history:<source>:<sym>:<from>:<to>`,
  `HISTORY_CACHE_TTL_SECONDS`) → Postgres read → conditional backfill from
  `adapter.get_history(..., "1d")` (idempotent upsert; intraday candles are
  aggregated to one bar/day) → re-read. Backfill fires only when coverage is
  incomplete or the tail is older than `HISTORY_STALENESS_DAYS`.
- Route: `GET /api/v1/prices/{symbol}/history?from=&to=` (legacy `start`/`end`
  still accepted). `interval=1d` → store path; other intervals → adapter
  passthrough (intraday is not persisted). Body stays `list[Candle]`
  (back-compat); provenance rides `X-Catallax-*` headers.
- Risk/backtest/ML/signal code reads daily bars from here, not from upstream.

## Result envelope + provenance (Foundations F1)

`adapters/envelope.py::Envelope[T]` = `{data, source, fetched_at, cache_hit,
stale, warnings}` (OpenBB `OBBject` pattern). `data` keeps the existing
`Quote`/`Candle`/`SymbolMatch` shapes. `HistoryStore` returns an `Envelope`;
the history route surfaces its provenance as `X-Catallax-Source` /
`-Fetched-At` / `-Cache-Hit` / `-Stale` / `-Warnings` headers so the body shape
stays backward-compatible. Future adapters (Phase 8 `MacroAdapter`) reuse the
envelope directly.

## Market-research sources (Phase 10a)

These are standalone adapter classes (not `PriceSourceAdapter`s — same shape as
`MacroAdapter`): Redis-cached, keyless, with a long-lived `:stale` fallback key so
a transient upstream blank serves the last good payload instead of poisoning the
TTL. Reached only by `MarketResearchEngine`; the UI only via `/api/v1/market/*`.

### SentimentAdapter (`adapters/sentiment_adapter.py`)
- `fear_greed()` → CNN Fear & Greed (`cnn_feargreed_url`): composite 0-100 score +
  rating + prev close/1w/1m/1y + the seven sub-indicators (momentum, strength,
  breadth, **put/call**, volatility, junk-bond demand, safe-haven demand) + ~180-pt
  history. The put/call sub-indicator is why no separate (now-gated) CBOE/AAII
  scrape is needed.
- `cot()` → CFTC Commitments of Traders (Socrata `cftc_cot_url`, legacy
  futures-only `6dca-aqww`): non-commercial (large-spec) **net** positioning for the
  `market_research_cot_markets` basket (code:label — E-mini S&P, Nasdaq, Dow, 10Y
  UST, Gold, EUR/USD, VIX), each with net, %OI, 1y z-score, percentile, 1w change.
  Markets keyed by stable `cftc_contract_market_code` (names drift; codes don't).
  Codes in `market_research_cot_inverse` (default VIX `1170E1`) are flagged
  `inverse=True` — their underlying is inverse to risk appetite (net-long = risk-off),
  so the UI flips the net color + z-direction for them.
- TTL `sentiment_cache_ttl_seconds` (6h). Both need a browser User-Agent.
- AAII bull/bear and a standalone CBOE put/call feed were evaluated and **dropped**:
  both 403 without a login as of 2026-06. CNN F&G covers the same signal free.

### BreadthAdapter (`adapters/breadth_adapter.py`)
- `diary()` → WSJ markets-diary JSON (`wsj_marketsdiary_url`, free/unauthenticated):
  per-market (NYSE / Nasdaq / NYSE American / NYSE Arca) advances, declines,
  unchanged, new highs/lows, up/down/total volume, TRIN, plus the session date.
  Redis-cached (`breadth_cache_ttl_seconds`, 4h) + stale fallback.
- The feed gives only today/prev/week-ago, not a series — so
  `MarketResearchEngine._breadth_diary_and_history` **persists one snapshot per
  (session_date, market)** into `market_breadth` (migration 0007, upsert-on-read,
  idempotent) and reads the accumulated primary-market series back. `_compute_breadth`
  then produces instant snapshot tiles (A/D ratio, breadth %, new H−L, up/down vol,
  TRIN) plus oscillators built forward from the series (A/D line, McClellan
  Osc/Summation from ratio-adjusted net advances) — the oscillators `warming_up`
  until ~40 sessions accumulate. No new compose profile/worker.
- **Accumulation no longer depends on tab views (2026-07-04):** a lifespan
  background task in `main.py` calls `persist_breadth_snapshot()` every
  `BREADTH_AUTOFETCH_INTERVAL_SECONDS` (default 6h, 0 disables, floor 600s), and
  the diary's `previousClose` values backfill a one-day gap (labeled the prior
  weekday, only when no row exists in the preceding 4 days — so a market-holiday
  "gap" never duplicates an existing session).
- AAII/CBOE-style constituent breadth (% above 50/200-dma) is NOT here — that needs
  constituent prices; the regime widget keeps its sector-ETF % > 200-dma proxy.

### ShillerAdapter (`adapters/shiller_adapter.py`)
- `cape()` → Shiller CAPE from **multpl.com** (`shiller_cape_url`, "Shiller PE by
  month" table), parsed with the stdlib `bs4` HTML parser (no lxml/xlrd dep). Returns
  full monthly history + latest CAPE + earnings yield (1/CAPE). The Yale
  `ie_data.xls` is no longer updated (ends 2023-09) — multpl is the maintained free
  mirror. TTL `shiller_cache_ttl_seconds` (24h; monthly data).
- Consumed by `_compute_valuation`: CAPE (+ percentile vs full history) and the
  equity risk premium (CAPE earnings yield − 10y real TIPS, `DFII10`).

### FundamentalsAdapter (`adapters/fundamentals_adapter.py`) — Phase 10b
- `statements(symbol)` → normalised income / balance-sheet / cash-flow keyed by
  fiscal-year + a light profile (name, sector, industry, currency, market cap,
  shares, EPS, dividend rate), via **yfinance** (`Ticker.income_stmt /
  balance_sheet / cashflow / info`) run in `asyncio.to_thread` (the lib is
  blocking). yfinance is already the F0 price dep — **no new dep, no OpenBB image
  rebuild** (the chosen alternative to mounting OpenBB's `equity` router, which calls
  the same yfinance underneath; decided 2026-06-26, see STRATEGY 10b).
- yfinance labels statement rows with human strings that drift between tickers, so
  each canonical field is resolved from an ordered **alias list** (first present
  wins) — see `_INCOME_FIELDS` / `_BALANCE_FIELDS` / `_CASHFLOW_FIELDS`. Values stay
  in the listing currency; the engine pairs them with an F0 price in the same
  currency (Altman's market-value term is self-consistent).
- Redis-cached `fundamentals:{symbol}` (`fundamentals_cache_ttl_seconds`, 24h) +
  a 30-day `:stale` fallback. Reached only by `services/research.py`; the UI only
  via `/api/v1/research/*`. The forensic scores (Piotroski / Altman / Beneish) are
  pure reproducible math — see `skills/equity-research.md`.

## Wealthfolio net-worth bridge (`services/wealthfolio.py`) — not a `PriceSourceAdapter`

A separate bridge (not registered in `ADAPTERS`) to the read-only exporter sidecar
(`WEALTHFOLIO_NETWORTH_URL`); see `skills/docker-services.md` for the
deployment contract. Phase 16: `history()`'s exporter contract carries per-day
`{total_value, cash, investments}`, not just `total_value` — `NetWorthPoint.cash`/
`.investments` now thread that split through both real and demo modes.
