from datetime import date
from functools import lru_cache

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings


def _parse_key_float_map(raw: str) -> dict[str, float]:
    """Parse "key:value,key2:value2,..." → {key: float(value)}, invalid tokens
    dropped. Case-preserving — callers normalise casing at lookup time."""
    out: dict[str, float] = {}
    for tok in raw.split(","):
        k, _, v = tok.partition(":")
        k, v = k.strip(), v.strip()
        if not k or not v:
            continue
        try:
            out[k] = float(v)
        except ValueError:
            pass
    return out


def _parse_sym_label(raw: str) -> list[tuple[str, str]]:
    """Parse "SYM:Label,SYM2:Label2,..." → [(sym, label)], order preserved."""
    out: list[tuple[str, str]] = []
    for tok in raw.split(","):
        tok = tok.strip()
        if not tok:
            continue
        sym, _, label = tok.partition(":")
        sym = sym.strip()
        if sym:
            out.append((sym, label.strip() or sym))
    return out


class Settings(BaseSettings):
    """All runtime configuration comes from environment variables (.env)."""

    postgres_host: str = "postgres"
    postgres_port: int = 5432
    postgres_user: str
    postgres_password: str
    postgres_db: str

    redis_host: str = "redis"
    redis_port: int = 6379
    redis_password: str
    redis_db: int = 0

    fava_base_url: str = "http://fava:5000/fava"

    # ── Price adapters ──────────────────────────────
    price_source_default: str = "yfinance"
    coingecko_base_url: str = "https://api.coingecko.com/api/v3"
    # Bare crypto tickers that resolve to Yahoo's "<SYM>-USD" pair when fetched
    # through yfinance (the free history backbone) — so historic crypto comes from
    # the same source as every other asset class, not CoinGecko. CoinGecko stays
    # the live markets-board source ("new data") only. Override via env.
    crypto_yf_symbols: str = (
        "BTC,ETH,SOL,ADA,XRP,BNB,DOT,DOGE,LTC,MATIC,AVAX,LINK,TRX,ATOM"
    )
    # Explicit symbol -> Yahoo ticker aliases ("SAP=SAP.DE,EXMPL-1=EXMPL1-USD"),
    # checked before the crypto rule — for suffix-less listings and CoinGecko-id
    # holdings that Yahoo can't resolve as-is. Override via env.
    yfinance_symbol_aliases: str = ""
    yfinance_rate_limit_per_min: int = 30
    coingecko_rate_limit_per_min: int = 30
    # Quote cache TTLs (seconds), keyed by broad asset class
    quote_cache_ttl_equity: int = 60
    quote_cache_ttl_crypto: int = 30
    # Stale-quote fallback window (seconds): when an upstream rate-limits (429) and
    # the fresh cache has expired, the last good quote is served from this longer
    # key instead of failing the symbol. Keeps the ticker live through CoinGecko's
    # aggressive free-tier limits (the F2 stale-cache pattern).
    quote_stale_ttl_seconds: int = 1800

    # ── Price-history store (Foundations F0) ──────────────────
    # Daily-bar series are read from Postgres (`daily_bars`) and Redis-cached;
    # backfill from upstream only fires on a cache miss with incomplete/stale
    # coverage. TTL bounds how often a miss can re-hit upstream per range.
    history_cache_ttl_seconds: int = 3600
    # Tolerated gap (days) between the newest stored bar and the requested end
    # before a tail backfill fires — covers weekend/holiday gaps so a "missing
    # today" bar (which won't exist until close) doesn't trigger a pointless call.
    history_staleness_days: int = 4
    # Max queryable history span (days); bounds upstream fetch + DB write volume
    # per request (~10y). Requests beyond this are rejected 400.
    history_max_range_days: int = 3650

    # ── Adapter resilience (Foundations F2) ───────────────────
    # Ordered fallback chains per asset class (comma lists of source ids). The
    # factory walks the chain, failing only after every source fails. Empty =
    # fall back to `price_source_default` alone.
    price_fallback_chain_equity: str = "yfinance,stooq"
    price_fallback_chain_crypto: str = "coingecko"
    stooq_base_url: str = "https://stooq.com"
    stooq_rate_limit_per_min: int = 30
    # Exponential backoff on upstream 429 (seconds): base * 2**attempt + jitter.
    price_backoff_base_seconds: float = 0.5
    price_backoff_max_retries: int = 2

    # ── News aggregator ─────────────────────────────
    # Comma-separated RSS feed URLs; split via `news_feed_urls`.
    news_feeds: str = (
        "https://feeds.a.dj.com/rss/RSSMarketsMain.xml,"
        "https://www.investing.com/rss/news.rss,"
        "https://finance.yahoo.com/news/rssindex"
    )
    news_cache_ttl_seconds: int = 300
    news_max_items: int = 50

    # ── Ledger bridge ───────────────────────────────
    ledger_cache_ttl_seconds: int = 120
    # Ledger file path as seen inside the backend container (./data mounted
    # read-only); used only to stat mtime for cache invalidation.
    beancount_file: str = "/data/ledger/main.bean"
    # Default reporting currency for converted ledger figures.
    reporting_currency: str = "EUR"

    # ── WebSocket ticker ────────────────────────────
    ticker_interval_seconds: int = 10
    # Security review (2026-08-15): the ASGI share-gate middleware can't see
    # inside a websocket scope's own frames, so `routers/ws.py` authenticates
    # itself and then bounds what one connection can cost: a hard concurrent-
    # connection cap and a max lifetime after which the server closes the
    # socket (client just reconnects, same as any other idle-timeout).
    ws_max_connections: int = 20
    ws_max_lifetime_seconds: int = 4 * 60 * 60

    # ── Portfolio engine ────────────────────────────
    # commodity -> "class:price_symbol:source" — drives asset-class allocation
    # and live-quote enrichment for any non-cash commodity LOTS in the ledger.
    # Empty by default (the ledger is assumed to hold cash only; investment lots
    # live in the net-worth source); add entries here if the ledger records
    # security/crypto positions.
    portfolio_commodities: str = ""
    # Currencies treated as cash (excluded from the holdings list).
    cash_currencies: str = "EUR,USD,GBP"

    # ── Markets overview ────────────────────────────
    # One cached cross-asset blob; this TTL bounds the upstream burst rate.
    markets_cache_ttl_seconds: int = 60
    # USGS earthquake feed (free, no key) proxied for the geo-map natural layer.
    usgs_quakes_url: str = (
        "https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/2.5_day.geojson"
    )

    # ── Portfolio Risk lens (Phase 8.5) ──────────────────────
    # Derived risk analytics over a Wealthfolio portfolio, computed from
    # the F0 daily-history backbone (no new data source). All returns are EUR.
    risk_lookback_days: int = 504  # ~2y calendar (~365 trading) of return history
    risk_max_holdings: int = 500  # defensive cap on the priced universe per request
    risk_min_history_days: int = 60  # min aligned obs to enter cov/beta/VaR
    risk_trading_days: int = 252  # annualisation factor
    risk_var_confidence: float = 0.95
    risk_var_min_scenarios: int = 120  # below this, VaR flagged low_confidence
    risk_fetch_concurrency: int = 6  # bounded gather over per-symbol history reads
    risk_fx_ffill_max_days: int = 3  # forward-fill FX across holidays, bounded
    risk_cache_ttl_seconds: int = 21600  # 6h — risk recomputes ~daily (daily bars)
    risk_bench_equity: str = "^GSPC"  # equity benchmark leg (USD → EUR)
    risk_bench_crypto: str = "BTC-USD"  # crypto benchmark leg (USD → EUR)
    risk_stress_enabled: bool = True
    # WF symbol → "price_symbol:source:quote_currency". Override-first; unmapped
    # CRYPTO → "{SYM}-USD"/USD, unmapped EQUITY → bare/USD (verified yfinance
    # resolution). EU/CH/UK listings MUST be mapped (their quote ccy + exchange
    # suffix can't be inferred from the bare exporter symbol). quote_currency is
    # the currency the PRICE SERIES is denominated in (drives the EUR conversion).
    risk_symbol_map: str = (
        "SAP:SAP.DE:yfinance:EUR,BMW:BMW.DE:yfinance:EUR,"
        "NOVN:NOVN.SW:yfinance:CHF,AIR:AIR.PA:yfinance:EUR"
    )
    # Symbols excluded from the risk universe (stablecoins / junk / delisted) but
    # still counted toward portfolio value (so covered-weight % stays honest).
    risk_skip_symbols: str = "USDC"
    # Stress scenarios: "name|EQUITY:-0.5,CRYPTO:-0.8,FX_USD:0.1,SYNTH:crypto"
    # semicolon-separated. Class shocks + per-currency FX overlay; SYNTH flags
    # asset classes that didn't exist in the window (proxy, not actual replay).
    risk_stress_scenarios: str = (
        "GFC 2008|EQUITY:-0.55,CRYPTO:-0.80,FX_USD:0.10,SYNTH:CRYPTO;"
        "COVID 2020|EQUITY:-0.34,CRYPTO:-0.50,FX_USD:0.03;"
        "Crypto Winter 2022|EQUITY:-0.20,CRYPTO:-0.75;"
        "Rates Shock 2022|EQUITY:-0.25,CRYPTO:-0.60,FX_USD:0.05"
    )

    # ── Portfolio Advisor — weekly rule engine + sell gate ──
    # Deterministic, config-driven rules over the synced portfolio (RiskEngine +
    # LedgerBridge + MarketResearchEngine regime). No trade is ever placed —
    # every evaluation is a recommendation row for a human to accept/override.
    # Gates the lifespan tick that runs the advisor weekly.
    advisor_enabled: bool = True
    # Advisor tick poll cadence — NOT the report cadence (that's the ISO-week
    # boundary via `services.advisor_report.report_due`, mirroring
    # `strategy_runner.rebalance_due`). This just bounds how long a missed
    # week boundary can go undetected after the container comes back up.
    advisor_tick_interval_seconds: int = 3600

    # "CLASS:upper_frac" — a single position's max share of NAV before the
    # weight-band-trim rule proposes trimming it back to this edge (hysteresis:
    # never trims past the edge to some lower target). Crypto gets its own
    # (wider) band so a volatile class does not trigger every week. Configurable.
    advisor_weight_bands: str = (
        "CRYPTO:0.40,EQUITY:0.30,ETF:0.30,COMMODITY:0.15,OTHER:0.20"
    )
    # Comma list of symbols held as market-priced positions but treated as CASH
    # by the advisor: joins rule 2's liquid measure,
    # excluded entirely from rule 1's weight-band evaluation (not trimmable, not
    # counted in any class weight, not an "unclassified" audit row — one summary
    # row instead) and from the sell gate's weight vector (their value is simply
    # left out, same effect as `recompute_with_weights` imputing leftover as cash).
    advisor_cash_equivalent_symbols: str = "USDC,USDT,DAI,EURC"
    # Cash floor = max(life-floor €, regime-band % of NAV). Life floor multiplier
    # × trailing-12mo avg monthly Expenses (LedgerBridge.query_rows BQL).
    # 0 disables the life-floor leg — rule 2 then targets the regime cash band
    # only; set >0 to re-enable 6×-expenses coverage. At 0, NO ledger call is
    # made by rule 2 at all (design decision: WF-only, investment-only —
    # bank/ledger cash is excluded from advisor input) — the
    # expenses BQL, staleness computation, and the clearing-accounts guard below
    # all exist only to protect this ledger-derived leg, so all three go dormant
    # together with it.
    advisor_life_floor_multiplier: float = 0.0
    # "regime_state:band_frac" (regime states from MarketResearchEngine.regime()).
    advisor_cash_bands: str = "risk-off:0.15,neutral:0.10,risk-on:0.06"
    # Accounts that must sum to exactly 0 before the cash-floor rule will size
    # anything (guards against a nonzero clearing balance). Comma list of ledger account names.
    # Only checked when the life floor is active (multiplier > 0) — see above.
    advisor_clearing_accounts: str = ""
    # Rule 2's cost gate: a liquidity raise is
    # NOT ΔCVaR-gated, but it must still respect
    # cost. If total (tax + venue) cost of the achievable raise exceeds this
    # fraction of the raise amount, the rule does NOT propose — it emits an
    # evaluated/hold row stating the arithmetic instead (e.g. a fixed venue
    # cost that dwarfs a small raise). Applied per leg too: any single
    # leg below the per-row materiality floor (ADVISOR_MATERIALITY_EUR /
    # _NAV_PCT) is dropped from the trim before this fraction is computed.
    advisor_cost_fraction_max: float = 0.10
    # DCA base contribution (reporting currency/period). 0 = rule DORMANT (no
    # investable income by default) — evaluates to a
    # pure no-op and emits no rows at all. >0 activates with no code change.
    advisor_dca_base: float = 0.0
    # "regime_state:scale_factor" applied to the DCA base once activated.
    advisor_dca_regime_scale: str = "risk-off:1.2,neutral:1.0,risk-on:0.8"
    # Weeks the sell-gate benefit (Δ weekly CVaR95) is scaled to, via sqrt-time
    # (i.i.d. weekly-return assumption) — the scaling assumption string this
    # implies is echoed in every gate row.
    advisor_review_horizon_weeks: int = 12
    # Sell gate fires iff benefit_eur > cost_eur * this ratio.
    advisor_benefit_cost_ratio: float = 1.5
    # Materiality floor for any proposed trim/sell: BOTH must hold.
    advisor_materiality_eur: float = 50.0
    advisor_materiality_nav_pct: float = 0.5
    # Weeks a rule the user OVERRODE stays quiet, unless its benefit arithmetic
    # has since moved by more than the materiality floor above.
    advisor_cooldown_weeks: int = 4
    # "CLASS:annual_frac" a-priori expected-return assumptions, used only to net
    # the opportunity cost of a trim (give-up) out of the sell-gate benefit.
    advisor_expected_return_map: str = (
        "CRYPTO:0.08,EQUITY:0.06,ETF:0.06,COMMODITY:0.03,CASH:0.02,OTHER:0.04"
    )
    # "CLASS:mode:value" per-asset-class venue transaction cost proxy (Holdings
    # carries no per-position venue). mode=flat → € per order; mode=pct → × trim value.
    advisor_venue_cost_map: str = "CRYPTO:pct:0.005,EQUITY:flat:1.0,ETF:flat:1.0,COMMODITY:flat:1.0,OTHER:flat:1.0"
    # Flat tax rate on capital gains (configurable; 0 = no tax modelled).
    advisor_tax_rate: float = 0.0
    # Fraction of a gain offset by available loss carryforward.
    # 0 = none assumed (conservative default; no loss-tracking exists yet).
    advisor_loss_offset_factor: float = 0.0
    # RNG seed for the v1.5 grader's paired-path block-bootstrap resampling
    # (design-draft.md §grader). UNUSED in v1 — the grader itself (paired-path
    # evaluator, migration 0012) is explicitly deferred (see the forecast ledger notes) — reserved
    # now so the eventual grader is deterministic/reproducible from day one
    # instead of retrofitting a seed after an unseeded run has already shipped.
    advisor_bootstrap_seed: int = 42

    # ── Weekly Report Pack — self-contained report-worker ──
    # Gates the report-worker's weekly build tick (later task owns the tick).
    report_pack_enabled: bool = True
    # Retry ceiling — the worker stops retrying a week's build once `attempts`
    # hits this, leaving a terminal `failed` row that closes the gate for the
    # week (no infinite retry loop on a permanently-broken week).
    report_pack_max_attempts: int = 5
    # Wall-clock ceiling (seconds) for one report-pack build job end to end.
    report_pack_job_timeout_seconds: int = 1200
    # HARD wall-clock bound (seconds) on the pack's whole in-process news-intel
    # enrich task (fetch + LLM calls + persist). It runs as a task independent
    # of the factor study and is clamped at use to job_timeout -
    # claude_code_timeout, so a slow LLM can never eat the factor study's cap
    # (a slow ~46s/call starved it). See report_worker._run_intel_enrichment.
    report_pack_intel_budget_seconds: int = 600
    # Wall-clock ceiling (seconds) for the narration sub-step alone.
    report_pack_narrate_timeout_seconds: int = 300
    # Max size (bytes) of the assembled facts payload handed to narration —
    # guards against an unbounded prompt.
    report_pack_facts_max_bytes: int = 65536
    # Minimum weeks of shadow_verdicts history required before a shadow-graded
    # tool's verdicts count toward a promotion decision —
    # the calendar-history maturity/low-confidence gate. The forward-return
    # grading horizon is a SEPARATE fixed constant (shadow.py's
    # _GRADE_HORIZON_WEEKS = 4) — deliberately decoupled from this setting.
    report_pack_shadow_grade_min_weeks: int = 4
    # RSI zone (Wilder RSI-14) that mutes a technicals verdict: a golden cross
    # at/above this RSI is overbought (muted to hold), a death cross at/below
    # the oversold threshold is likewise muted.
    report_pack_shadow_rsi_overbought: float = 70.0
    report_pack_shadow_rsi_oversold: float = 30.0
    # Piotroski F-Score (0-9) bands that drive the forensic tool's verdict
    # (direction is always NULL — forensic is excluded from grading).
    report_pack_shadow_piotroski_weak: float = 3.0
    report_pack_shadow_piotroski_strong: float = 7.0
    # Seconds between report-worker ticks — the worker's local
    # `_TICK_INTERVAL_SECONDS` constant switches to this setting; also drives the
    # compose healthcheck's stale-heartbeat window (3x this value).
    report_pack_tick_interval_seconds: int = 300

    # ── Forecast ledger ──────────────────────────────
    # Max forecasts resolved per report-worker tick — bounds one tick's
    # resolver batch.
    forecast_resolver_batch_size: int = 200
    # Max gap, in days, between a forecast's `decision_date` and the entry
    # bar AND between its `due_date` and the exit bar; beyond this the
    # forecast resolves as `uncovered` rather than `matured`.
    forecast_resolver_stale_days: int = 10
    # Effective graded history (cross-sectional-n-adjusted) below which an
    # issued probability carries a `prior_note` evidence ref rather than
    # standing alone.
    forecast_probability_min_n: int = 8
    # Reliability bin count for varying-probability (human) issuers in the
    # Calibration chapter; constant-p (machine/shadow) issuers get
    # discrimination stats + a climatological Brier reference instead.
    report_pack_calibration_bins: int = 5
    # How many weeks back `calibration()` reads.
    report_pack_calibration_window_weeks: int = 104
    # Weeks after `as_of` an untouched `proposed` recommendation expires.
    # 0 = fall back to `advisor_review_horizon_weeks`.
    recommendation_expiry_weeks: int = 0
    # Floor on `decision_date` for any forecast `ForecastLedger.issue()` accepts
    # (the spec "no retro-dated forecasts") — a row dated before this is
    # rejected into `errors`, never inserted, even when a human forces an
    # old week's pack.
    forecast_ledger_start_date: date = date(2026, 8, 31)

    # ── Phase 19 — analyst journal ──────────────────────────────
    # Fallback horizon (days) when the quick entry omits a review date.
    journal_default_horizon_days: int = 30
    # Reject sub-day horizons — the resolver works off daily bars.
    journal_min_horizon_days: int = 1
    # Ledger survival policy ceiling on a journal entry's horizon.
    journal_max_horizon_days: int = 365
    # Cap on pack-seeded drafts per Monday tick.
    journal_draft_max_per_week: int = 5
    # Where redacted note exports land.
    notes_export_dir: str = "/data/reports/notes"
    # Comma-separated account labels to remove from shareable note PDFs.
    notes_account_names: str = ""
    # true = a surviving privacy-scan pattern fails the export.
    notes_privacy_scan_strict: bool = True
    # Off by default — merging this phase must not turn an LLM loop on in
    # any deployment by accident; switched on deliberately per host.
    cockpit_enabled: bool = False
    # Agentic loop cap inside one cockpit turn.
    cockpit_max_turns: int = 8
    # Hard cap, in seconds, on one cockpit turn.
    cockpit_timeout_seconds: int = 180
    # Oldest-first trim when building cockpit context.
    cockpit_max_session_messages: int = 40
    # Comma-separated tool-name allowlist passed to the provider.
    # Defaults to the full `cockpit_tools.TOOLS` registry (sorted) —
    # a literal here, not an import of
    # `services/cockpit_tools`, to avoid a config<->services import cycle
    # (cockpit_tools imports `Settings` from this module).
    cockpit_allowed_tools: str = (
        "forecasts,journal_entries,market_regime,notes,portfolio_exposure,"
        "provenance,recommendations,report_packs,risk_metrics"
    )

    # ── Event intelligence ───────────────────────────
    events_enabled: bool = False
    events_tick_interval_seconds: int = Field(default=900, ge=300)
    events_http_timeout_seconds: int = 20
    events_adapter_timeout_seconds: int = 120
    events_user_agent: str = "Catallax/1.0 (self-hosted)"
    events_max_items_per_feed: int = 100
    events_dead_letter_after: int = 5
    events_cb_feeds: str = "ECB=https://www.ecb.europa.eu/press/rss/press.html,Fed=https://www.federalreserve.gov/feeds/press_all.xml"
    events_ir_feeds: str = ""
    sec_edgar_user_agent: str = ""
    sec_edgar_rate_limit_per_sec: int = Field(default=5, ge=1, le=10)
    sec_edgar_forms: str = "4,8-K,6-K,SC 13D,SC 13G"
    alerts_cooldown_minutes: int = 240
    alerts_min_score: float = 0.6
    alerts_quiet_hours: str = "22-07 UTC"  # configurable
    alerts_corroboration_window_minutes: int = 120
    alerts_ntfy_url: str = ""
    alerts_ntfy_token: SecretStr = SecretStr("")
    alerts_ntfy_max_bytes: int = 4000

    # ── Market Research (Phase 10a) — top-down / cross-asset ──
    # All ETF/index history comes from the F0 yfinance backbone; FRED series from
    # the openbb-data MacroAdapter (OPENBB_FRED_API_KEY must be set). The bundle is
    # cached in Redis (6h default) and shared by all 7 slice endpoints.
    market_research_cache_ttl_seconds: int = 21600
    market_research_lookback_days: int = 504
    market_research_fetch_concurrency: int = 8
    market_research_corr_window: int = 90
    market_research_vol_percentile_window: int = 252
    market_research_rrg_smooth: int = (
        5  # weekly EMA span applied to RS before normalization
    )
    market_research_rrg_norm_weeks: int = (
        14  # rolling z-score window (in WEEKS) for RS-Ratio/Momentum
    )
    market_research_rrg_tail_weeks: int = 8
    market_research_benchmark: str = "SPY"
    market_research_cross_asset: str = (
        "SPY:US Large Cap,QQQ:Nasdaq 100,IWM:US Small Cap,EFA:DM ex-US,EEM:EM,"
        "TLT:Long Treasury,IEF:7-10y Treasury,LQD:IG Credit,HYG:HY Credit,"
        "GLD:Gold,DBC:Commodities,USO:Oil,UUP:US Dollar,BTC-USD:Bitcoin"
    )
    market_research_sectors: str = (
        "XLK:Technology,XLF:Financials,XLE:Energy,XLV:Health Care,XLI:Industrials,"
        "XLU:Utilities,XLP:Staples,XLY:Discretionary,XLB:Materials,XLRE:Real Estate,"
        "XLC:Communication"
    )
    market_research_factors: str = (
        "MTUM:Momentum,VLUE:Value,QUAL:Quality,USMV:Low Vol,SIZE:Size,"
        "IVW:Growth,IVE:Value Blend"
    )
    market_research_fred_series: str = (
        "VIXCLS,BAMLH0A0HYM2,BAMLC0A0CM,T10YIE,T10Y2Y,NFCI,ANFCI,"
        "DFII10,DTWEXBGS,M2SL,WALCL,USREC,SAHMREALTIME,NCBEILQ027S,GDP"
    )
    market_research_vol_symbols: str = "^VIX,^VIX3M,^MOVE"
    market_research_regime_weights: str = (
        "trend:0.2,breadth:0.2,vol:0.15,credit:0.2,curve:0.15,momentum:0.1"
    )
    # ── Sentiment / positioning (widget 8) + CAPE valuation external sources ──
    # All free, keyless. CNN Fear & Greed carries the put/call sub-indicator, so a
    # separate (gated) CBOE/AAII scrape is not needed. CAPE comes from multpl.com
    # (the maintained Shiller mirror; the Yale ie_data.xls is no longer updated).
    sentiment_cache_ttl_seconds: int = 21600  # CNN F&G + CFTC COT (6h)
    shiller_cache_ttl_seconds: int = 86400  # Shiller CAPE — monthly data (24h)
    cnn_feargreed_url: str = (
        "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
    )
    cftc_cot_url: str = "https://publicreporting.cftc.gov/resource/6dca-aqww.json"
    shiller_cape_url: str = "https://www.multpl.com/shiller-pe/table/by-month"
    # Market breadth (widget 9, v1.2) — WSJ markets diary (free, unauthenticated).
    breadth_cache_ttl_seconds: int = 14400  # 4h; refreshes through the session
    wsj_marketsdiary_url: str = (
        "https://www.wsj.com/market-data/stocks/marketsdiary?id=%7B%22application%22%3A%22WSJ%22"
        "%2C%22marketsDiaryType%22%3A%22diaries%22%7D&type=mdc_marketsdiary"
    )
    market_research_breadth_primary: str = "NYSE"  # market for the A/D line + McClellan
    market_research_breadth_history_days: int = (
        504  # accumulated series read-back window
    )
    # In-backend daily snapshot persist (the diary is upsert-on-read; without this,
    # any session nobody opens the tab on is lost forever). 0 disables.
    breadth_autofetch_interval_seconds: int = (
        21600  # 6h > 4h cache TTL → fresh diary each run
    )
    market_research_cot_markets: str = (
        "13874A:S&P 500,209742:Nasdaq 100,124603:Dow Jones,043607:10Y UST,"
        "088691:Gold,099741:EUR/USD,1170E1:VIX"
    )
    # COT codes whose underlying is INVERSE to risk appetite: net-long spec = risk-OFF
    # (e.g. VIX — net-long volatility = more fear). Colors/z-direction flip for these.
    market_research_cot_inverse: str = "1170E1"  # VIX

    @property
    def market_research_cot_market_map(self) -> list[tuple[str, str]]:
        """[(cftc_contract_market_code, label)] for the COT basket, order preserved."""
        return _parse_sym_label(self.market_research_cot_markets)

    @property
    def market_research_cot_inverse_set(self) -> set[str]:
        return {
            c.strip() for c in self.market_research_cot_inverse.split(",") if c.strip()
        }

    @property
    def market_research_cross_asset_map(self) -> list[tuple[str, str]]:
        """[(symbol, label)] for the cross-asset basket, order preserved."""
        return _parse_sym_label(self.market_research_cross_asset)

    @property
    def market_research_sectors_map(self) -> list[tuple[str, str]]:
        return _parse_sym_label(self.market_research_sectors)

    @property
    def market_research_factors_map(self) -> list[tuple[str, str]]:
        return _parse_sym_label(self.market_research_factors)

    @property
    def market_research_fred_list(self) -> list[str]:
        return [
            s.strip() for s in self.market_research_fred_series.split(",") if s.strip()
        ]

    @property
    def market_research_vol_list(self) -> list[str]:
        return [
            s.strip() for s in self.market_research_vol_symbols.split(",") if s.strip()
        ]

    @property
    def market_research_regime_weight_map(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for tok in self.market_research_regime_weights.split(","):
            k, _, v = tok.partition(":")
            k = k.strip()
            if k and v.strip():
                try:
                    out[k] = float(v.strip())
                except ValueError:
                    pass
        return out

    # ── Equity Research & Screening (Phase 10b) ──────────────
    # Single-name fundamentals via the FundamentalsAdapter (yfinance statements —
    # already a dep, no image rebuild) paired with the F0 price backbone for market
    # value + technicals. The forensic scores (Piotroski F / Altman Z / Beneish M)
    # are pure reproducible math in services/research.py. The screen iterates this
    # config universe (env-overridable, no hardcoding).
    fundamentals_cache_ttl_seconds: int = 86400  # statements update quarterly (24h)
    fundamentals_fetch_timeout_seconds: int = (
        30  # bound a hung yfinance fetch (frees the semaphore/request)
    )
    research_cache_ttl_seconds: int = 21600  # computed screen rows (6h, daily key)
    research_fetch_concurrency: int = (
        6  # bounded F0/statement fan-out (risk.py precedent)
    )
    research_history_lookback_days: int = (
        504  # ~2y daily bars for SMA200 + 52w range + RSI
    )
    research_screen_max_symbols: int = 60  # defensive cap on the screened universe
    research_universe: str = (
        "AAPL:Apple,MSFT:Microsoft,GOOGL:Alphabet,AMZN:Amazon,META:Meta Platforms,"
        "NVDA:NVIDIA,TSLA:Tesla,JPM:JPMorgan,V:Visa,JNJ:Johnson & Johnson,"
        "WMT:Walmart,XOM:Exxon Mobil,KO:Coca-Cola,PG:Procter & Gamble,HD:Home Depot,"
        "BAC:Bank of America,DIS:Disney,NFLX:Netflix,INTC:Intel,AMD:AMD,"
        "PFE:Pfizer,CSCO:Cisco,ORCL:Oracle,ADBE:Adobe"
    )

    @property
    def research_universe_map(self) -> list[tuple[str, str]]:
        """[(symbol, label)] for the screening universe, order preserved."""
        return _parse_sym_label(self.research_universe)

    @property
    def lab_universe_map(self) -> list[tuple[str, str]]:
        """[(symbol, label)] for the Quant/ML Lab universe, order preserved."""
        return _parse_sym_label(self.lab_universe)

    # ── Signal self-scoring core (Phase 9, deliverable #1) ────
    # Logs emitted cross-sectional scores (`signals` table) and grades them LAZILY
    # against realised forward returns from the F0 daily-history backbone — no new
    # data source. Primary grade = cross-sectional Rank-IC → ICIR → t-stat.
    signal_fetch_concurrency: int = (
        6  # bounded gather over per-symbol F0 reads (risk.py precedent)
    )
    signal_default_horizon_bars: int = 21  # forward holding (trading bars ≈ 1 month)
    signal_default_quantiles: int = 5  # L/S spread buckets (quintiles over ~40 names)
    signal_min_names: int = 5  # min cross-section breadth for a date to yield an IC
    signal_min_dates: int = (
        8  # below this, ICIR/t-stat flagged low_confidence (no verdict)
    )
    # Settle buffer: grade only once the exit bar is at least this many days in the
    # past. history.py finalises provisional closes via on_conflict_do_update, so a
    # forward return read the instant the exit bar first prints can later restate.
    signal_settle_days: int = 2
    signal_seed_months: int = (
        24  # months of monthly decision dates the reference seed builds
    )
    signal_momentum_lookback_bars: int = 252  # 12-1 momentum formation window
    signal_momentum_skip_bars: int = (
        21  # skip the most recent month (reversal) — the "1" in 12-1
    )
    signal_lowvol_lookback_bars: int = (
        63  # trailing window for the low-vol anomaly factor
    )
    signal_grade_ttl_seconds: int = 21600  # 6h — grades recompute ~daily (daily bars)
    signal_seed_enabled: bool = (
        True  # gate POST /signals/seed (the reference generators)
    )

    # ── Backtester (Phase 9, engine) ──────────────────────────
    # A backtesting FRAMEWORK (BacktestEngineAdapter ABC + factory), engine #1 a
    # reimplemented numpy/pandas cross-sectional signal-driven quantile rebalance.
    # Jobs run OFF the main API in the `backtester` worker (--profile backtest),
    # claimed off a Redis queue, results persisted to the `backtests` table. The
    # worker is structurally READ-ONLY on the F0 daily_bars/signals backbone.
    backtest_default_engine: str = "vectorized"  # factory key for engine #1
    backtest_default_costs_bps: float = 10.0  # one-way transaction cost on turnover
    backtest_default_quantile: int = 5  # top-bucket fraction = 1/quantile (quintile)
    backtest_max_window_days: int = 3650  # config validator cap (~10y)
    backtest_min_rebalances: int = (
        3  # below this a backtest is too short to be meaningful
    )
    backtest_max_symbols: int = 500  # defensive cap on the priced universe per job
    backtest_max_concurrency: int = 2  # worker in-flight job cap (Semaphore)
    backtest_fetch_concurrency: int = (
        6  # per-job F0 series fan-out (risk/signal precedent)
    )
    backtest_heartbeat_seconds: int = (
        10  # worker heartbeat cadence (crash-recovery liveness)
    )
    backtest_stale_seconds: int = (
        90  # a 'running' job older than this is reclaimed on worker boot
    )
    backtest_poll_timeout_seconds: int = (
        5  # BRPOPLPUSH block timeout in the worker loop
    )
    backtest_result_ttl_days: int = 0  # 0 = keep results forever (no auto-prune)
    backtest_max_queued: int = (
        50  # reject new POSTs when this many jobs are queued/running (flood guard)
    )

    # ── Quant / ML Research Lab (Phase 11, --profile lab) ─────
    # A factor-research + ML model-zoo capability isolated in the heavy `qlib-lab`
    # container (torch/xgboost/qlib). Jobs run OFF the main API in `python -m
    # app.lab_worker` exactly like the backtester: claimed off a Redis queue,
    # results persisted to the `lab_runs` table. The worker is structurally
    # READ-ONLY on the F0 daily_bars backbone. The model engine is a factory ABC
    # (LabModelEngine) — `sklearn-ensemble` (XGBoost+RF) ships + runs; `qlib-zoo`
    # (LSTM/Transformer) is registered and runs iff pyqlib imports in the image.
    lab_default_model_engine: str = (
        "sklearn-ensemble"  # factory key for the shipped engine
    )
    lab_max_concurrency: int = (
        1  # worker in-flight job cap (training is heavy; Semaphore)
    )
    lab_heartbeat_seconds: int = (
        10  # worker heartbeat cadence (crash-recovery liveness)
    )
    lab_stale_seconds: int = 600  # a 'running' job older than this is reclaimed on worker boot (train is slow)
    lab_poll_timeout_seconds: int = 5  # queue poll idle cadence in the worker loop
    lab_max_queued: int = (
        20  # reject new POSTs when this many jobs are queued/running (flood guard)
    )
    lab_fetch_concurrency: int = (
        6  # per-job F0 series fan-out (risk/signal/backtest precedent)
    )
    lab_history_lookback_days: int = (
        1825  # ~5y of daily bars pulled per symbol for features/targets
    )
    lab_max_symbols: int = 60  # defensive cap on the priced universe per lab job
    lab_min_train_rows: int = (
        200  # below this a model train is too small to be meaningful
    )
    lab_horizon_days: int = 21  # forward-return prediction horizon (1 trading month)
    # Factor/IC + ML feature/training universe (reuse the research large-cap default).
    lab_universe: str = (
        "AAPL:Apple,MSFT:Microsoft,GOOGL:Alphabet,AMZN:Amazon,NVDA:Nvidia,"
        "META:Meta,TSLA:Tesla,JPM:JPMorgan,V:Visa,JNJ:Johnson & Johnson,"
        "WMT:Walmart,PG:Procter & Gamble,XOM:Exxon,UNH:UnitedHealth,HD:Home Depot,"
        "MA:Mastercard,KO:Coca-Cola,PEP:PepsiCo,COST:Costco,ADBE:Adobe"
    )

    # ── AI intel worker (Phase 12, --profile ai) — mirrors the backtester/lab job model.
    ai_max_concurrency: int = 2  # worker in-flight enrich-job cap (Semaphore)
    ai_heartbeat_seconds: int = 10  # worker heartbeat cadence (crash-recovery liveness)
    ai_stale_seconds: int = (
        300  # a 'running' job older than this is reclaimed on worker boot
    )
    ai_poll_timeout_seconds: int = 5  # queue poll idle cadence in the worker loop
    ai_max_queued: int = (
        10  # reject new POSTs when this many jobs queued/running (flood guard)
    )
    ai_enrich_interval_seconds: int = (
        0  # >0 = lifespan auto-enrich cadence; 0 disables (manual only)
    )
    ai_enrich_max_articles: int = (
        40  # per-run cap on articles enriched (bounds claude calls)
    )

    # ── Paper-trading algo hub (Phase 13, --profile algo) ─────
    # Off-API `python -m app.algo_worker` (trading-engine container) ticks the
    # paper book on this cadence: claims new orders, resolves fills against the F0
    # quote/history backbone, rebalances active strategies, and snapshots the
    # account. algo_broker selects the fill-simulation adapter (paper = the only
    # engine for now; the seam exists for a future live-broker adapter).
    algo_tick_seconds: int = 15  # worker main-loop cadence
    algo_starting_cash: float = 100000.0  # seed cash for a fresh paper book
    algo_slippage_bps: float = 5.0  # simulated fill slippage on market orders
    algo_commission_per_order: float = 1.0  # flat per-fill commission
    algo_max_order_notional: float = 25000.0  # reject an order above this notional
    algo_max_open_orders: int = 50  # flood guard on resting/queued orders
    algo_stale_seconds: int = (
        120  # worker heartbeat staleness threshold used by /algo/status
    )
    algo_broker: str = "paper"  # fill-simulation adapter factory key

    # ── Macro & Economic dashboard (Phase 8, OpenBB Path B) ───
    # OpenBB Platform REST API runs as the isolated `openbb-data` sidecar
    # (internal network only). The MacroAdapter reaches it over HTTP; the
    # frontend never touches it directly. Macro data is daily/monthly, so it is
    # cached hard (hours, not the 60s markets TTL).
    openbb_api_url: str = "http://openbb-data:6900"
    openbb_http_timeout: float = 20.0
    macro_cache_ttl_seconds: int = 21600  # 6h — macro series move daily at most
    # Indicator heatmap set: `root:transform` comma list. transform `toya` =
    # year-over-year %, `level` = raw level (both arrive as fractions from econdb).
    macro_indicator_set: str = "CPI:toya,CORE:toya,PPI:toya,URATE:level,RGDP:toya"
    # Countries for the heatmap columns (econdb country names; keyless provider).
    macro_indicator_countries: str = (
        "united_states,euro_area,germany,united_kingdom,japan,china"
    )
    # Default yield-curve / rates region (US | EU).
    macro_default_region: str = "US"
    # FRED is a free key (https://fredaccount.stlouisfed.org). When unset, the
    # keyless providers (federal_reserve / ecb / econdb) still serve curves +
    # indicators; fred_series / cpi / the release calendar light up once it's set.
    # Flows to the openbb-data container (series/cpi) AND to the one direct FRED
    # call the backend makes itself: the release calendar (see below).
    openbb_fred_api_key: str | None = None
    # Release calendar goes to FRED directly (one call) because OpenBB's fred
    # calendar provider fans out per-release requests and reliably times out.
    fred_releases_dates_url: str = "https://api.stlouisfed.org/fred/releases/dates"

    # ── Net worth source: Wealthfolio (portfolio) ────────
    # Net worth comes from Wealthfolio, NOT the sample ledger: the
    # ledger here is sample data, while Wealthfolio holds the market-priced
    # multi-account portfolio. A read-only exporter sidecar in the wealthfolio
    # stack serves the absolute EUR net-worth series the WF REST API doesn't.
    wealthfolio_networth_url: str = "http://stub-exporter:8089"
    wealthfolio_cache_ttl_seconds: int = 300

    # ── LLM provider seam (Foundations F3) ────────────────────
    # Reserved now, no feature yet: the default no-op provider calls no model.
    # Phase 12 plugs a real provider (Ollama-local-first) in via the factory.
    llm_provider: str = "noop"
    llm_base_url: str | None = None
    llm_model: str | None = None
    llm_api_key: str | None = None

    # ── Claude Code provider — headless `claude -p` under the F3 seam.
    # Runs ONLY in the off-API `ai-runner` worker (has the binary + mounted creds);
    # the API container keeps llm_provider=noop.
    claude_code_bin: str = "claude"  # CLI binary name/path inside the ai-runner image
    claude_code_credentials_path: str | None = (
        None  # host ~/.claude dir, mounted :ro (env-driven)
    )
    claude_code_timeout_seconds: int = 120  # hard cap on one `claude -p` invocation
    claude_code_max_turns: int = 1  # single-shot narration; no agentic tool loop

    # ── Phase 14 — beta gates (demo mode + share path) ──────────
    # DEMO_MODE swaps every money-bearing endpoint to synthetic fixtures so the app
    # can be shared without exposing live portfolio/ledger data.
    demo_mode: bool = False
    owner_name: str = ""  # greeting + privacy-scan token; empty disables both; blanked in demo mode
    # Empty passphrase = gate disabled (single-user localhost default). SecretStr so
    # an accidental `Settings` repr/dump never prints it — every read site calls
    # `.get_secret_value()` explicitly (never rely on SecretStr's own truthiness).
    share_passphrase: SecretStr = SecretStr("")
    share_session_days: int = 30
    # Off by default: the app is served over plaintext HTTP on its direct port
    # (a supported loopback/LAN deployment), and a `secure` cookie is silently
    # dropped by the browser over plain HTTP — turning this on unconditionally
    # would break login there. Set true only when a TLS-terminating edge sits
    # in front of nginx (security review, 2026-08-15).
    share_cookie_secure: bool = False

    @field_validator("share_passphrase")
    @classmethod
    def _ck_share_passphrase(cls, v: SecretStr) -> SecretStr:
        # Below this length, a login gate is brute-forceable within the login
        # rate limiter's 10-attempts/15-min window (see auth.py's _LOGIN_*).
        min_length = 12
        value = v.get_secret_value()
        if value and len(value) < min_length:
            raise ValueError(
                "SHARE_PASSPHRASE must be empty (gate disabled) or at least "
                f"{min_length} characters — a short passphrase is brute-forceable "
                "within the login rate limiter's window"
            )
        return v

    @property
    def news_feed_urls(self) -> list[str]:
        return [u.strip() for u in self.news_feeds.split(",") if u.strip()]

    @property
    def portfolio_commodity_map(self) -> dict[str, dict[str, str | None]]:
        out: dict[str, dict[str, str | None]] = {}
        for tok in self.portfolio_commodities.split(","):
            parts = [p.strip() for p in tok.split(":")]
            if len(parts) >= 2 and parts[0]:
                out[parts[0].upper()] = {
                    "asset_class": parts[1] or "other",
                    "symbol": parts[2] if len(parts) > 2 and parts[2] else parts[0],
                    "source": parts[3] if len(parts) > 3 and parts[3] else None,
                }
        return out

    @property
    def cash_currency_list(self) -> list[str]:
        return [c.strip().upper() for c in self.cash_currencies.split(",") if c.strip()]

    # econdb transforms we accept (mirrors the route-layer allowlist so an
    # operator-supplied MACRO_INDICATOR_SET can't smuggle an arbitrary token
    # into the upstream query string).
    _MACRO_TRANSFORMS = {"toya", "level", "tpop", "index"}

    @property
    def macro_indicators(self) -> list[tuple[str, str]]:
        """(indicator_root, transform) pairs for the heatmap. transform `level`
        means no econdb transform (raw level); unknown tokens clamp to `level`."""
        out: list[tuple[str, str]] = []
        for tok in self.macro_indicator_set.split(","):
            parts = [p.strip() for p in tok.split(":")]
            if parts and parts[0]:
                transform = parts[1].lower() if len(parts) > 1 and parts[1] else "level"
                if transform not in self._MACRO_TRANSFORMS:
                    transform = "level"
                out.append((parts[0].upper(), transform))
        return out

    @property
    def macro_country_list(self) -> list[str]:
        return [
            c.strip() for c in self.macro_indicator_countries.split(",") if c.strip()
        ]

    @property
    def risk_symbol_overrides(self) -> dict[str, dict[str, str]]:
        """WF symbol → {price_symbol, source, currency}. Same colon/comma idiom as
        `portfolio_commodity_map`."""
        out: dict[str, dict[str, str]] = {}
        for tok in self.risk_symbol_map.split(","):
            parts = [p.strip() for p in tok.split(":")]
            if len(parts) >= 4 and parts[0]:
                out[parts[0].upper()] = {
                    "price_symbol": parts[1],
                    "source": parts[2] or "yfinance",
                    "currency": (parts[3] or "USD").upper(),
                }
        return out

    @property
    def risk_skip_set(self) -> set[str]:
        return {
            s.strip().upper() for s in self.risk_skip_symbols.split(",") if s.strip()
        }

    @property
    def risk_stress_list(self) -> list[dict[str, object]]:
        """Parse the stress-scenario env string into structured scenarios."""
        out: list[dict[str, object]] = []
        for chunk in self.risk_stress_scenarios.split(";"):
            chunk = chunk.strip()
            if not chunk or "|" not in chunk:
                continue
            name, _, body = chunk.partition("|")
            class_shocks: dict[str, float] = {}
            fx_shocks: dict[str, float] = {}
            synthetic: list[str] = []
            for tok in body.split(","):
                k, _, v = tok.partition(":")
                k, v = k.strip().upper(), v.strip()
                if not k or not v:
                    continue
                if k == "SYNTH":
                    synthetic.append(v.upper())
                elif k.startswith("FX_"):
                    try:
                        fx_shocks[k[3:]] = float(v)
                    except ValueError:
                        pass
                else:
                    try:
                        class_shocks[k] = float(v)
                    except ValueError:
                        pass
            out.append(
                {
                    "name": name.strip(),
                    "class_shocks": class_shocks,
                    "fx_shocks": fx_shocks,
                    "synthetic": synthetic,
                }
            )
        return out

    # ── Portfolio Advisor ──────────────────────
    @property
    def advisor_weight_band_map(self) -> dict[str, float]:
        return {
            k.upper(): v
            for k, v in _parse_key_float_map(self.advisor_weight_bands).items()
        }

    @property
    def advisor_cash_band_map(self) -> dict[str, float]:
        return {
            k.lower(): v
            for k, v in _parse_key_float_map(self.advisor_cash_bands).items()
        }

    @property
    def advisor_clearing_accounts_list(self) -> list[str]:
        return [
            a.strip() for a in self.advisor_clearing_accounts.split(",") if a.strip()
        ]

    @property
    def advisor_cash_equivalent_symbols_set(self) -> set[str]:
        return {
            s.strip().upper()
            for s in self.advisor_cash_equivalent_symbols.split(",")
            if s.strip()
        }

    @property
    def advisor_dca_regime_scale_map(self) -> dict[str, float]:
        return {
            k.lower(): v
            for k, v in _parse_key_float_map(self.advisor_dca_regime_scale).items()
        }

    @property
    def advisor_expected_return_class_map(self) -> dict[str, float]:
        return {
            k.upper(): v
            for k, v in _parse_key_float_map(self.advisor_expected_return_map).items()
        }

    @property
    def advisor_venue_cost_class_map(self) -> dict[str, tuple[str, float]]:
        """CLASS → (mode, value); mode is "flat" (€/order) or "pct" (× trim value)."""
        out: dict[str, tuple[str, float]] = {}
        for tok in self.advisor_venue_cost_map.split(","):
            parts = [p.strip() for p in tok.split(":")]
            if len(parts) == 3 and parts[0] and parts[1] in ("flat", "pct"):
                try:
                    out[parts[0].upper()] = (parts[1], float(parts[2]))
                except ValueError:
                    pass
        return out

    def fallback_chain(self, asset_type: str) -> list[str]:
        """Ordered source ids to try for an asset class. Falls back to the
        configured default source when no chain is set for the class."""
        raw = (
            self.price_fallback_chain_crypto
            if asset_type == "crypto"
            else self.price_fallback_chain_equity
        )
        chain = [s.strip() for s in raw.split(",") if s.strip()]
        return chain or [self.price_source_default]

    @property
    def crypto_yf_symbol_set(self) -> frozenset[str]:
        """Bare crypto tickers (upper) that yfinance must fetch as `<SYM>-USD`."""
        return frozenset(
            s.strip().upper() for s in self.crypto_yf_symbols.split(",") if s.strip()
        )

    @property
    def yfinance_symbol_alias_map(self) -> dict[str, str]:
        """`YFINANCE_SYMBOL_ALIASES` parsed to {SYMBOL (upper): yahoo_ticker}."""
        pairs = (
            p.split("=", 1) for p in self.yfinance_symbol_aliases.split(",") if "=" in p
        )
        return {
            k.strip().upper(): v.strip() for k, v in pairs if k.strip() and v.strip()
        }

    @property
    def database_url(self) -> str:
        return (
            f"postgresql+asyncpg://{self.postgres_user}:{self.postgres_password}"
            f"@{self.postgres_host}:{self.postgres_port}/{self.postgres_db}"
        )

    @property
    def redis_url(self) -> str:
        return (
            f"redis://:{self.redis_password}"
            f"@{self.redis_host}:{self.redis_port}/{self.redis_db}"
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
