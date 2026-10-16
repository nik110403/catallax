import { isDemoMode } from "../demo";
// Central help-text registry for the panel ⓘ button. Keyed by widget id; the
// Panel shows `def.help ?? WIDGET_HELP[id]` (a widget can override inline via its
// definition's `help` field — see registry.ts). Plain text; a blank line
// separates paragraphs (rendered with `white-space: pre-line`). Keep each entry
// to "what it shows" + "how to read / use it", a couple of short paragraphs.
//
// Deck line for `<PanelHead>` (Phase 14e, revised 2026-08-15 per design review
// P1): the long-form WIDGET_HELP text is written to enumerate — "what it
// shows" — which reads fine in the ⓘ popover but truncates mid-list or lands
// imperative ("…then Run") when sliced down to a one-line caption. WIDGET_DECK
// is a parallel map of hand-written, 6–10-word MEANING lines — what the panel
// is for, not what's visibly in it — kept deliberately separate from
// WIDGET_HELP so the popover's enumerate-and-explain text is untouched.
// deckLine(id) prefers the explicit deck and only falls back to the old
// first-sentence extraction for an id that has none (there shouldn't be one —
// every WIDGET_HELP key has a WIDGET_DECK entry — but the fallback keeps a
// future widget from rendering an empty caption instead of erroring loudly).
// Signature is unchanged: every existing `deckLine(id)` call site is untouched.
export function deckLine(id: string): string {
  if (isDemoMode() && DEMO_DECK[id]) return DEMO_DECK[id];
  if (WIDGET_DECK[id]) return WIDGET_DECK[id];
  const text = WIDGET_HELP[id];
  if (!text) return "";
  const paragraph = text.split("\n\n")[0];
  const sentence = paragraph.match(/^.*?\.(?=\s|$)/);
  return sentence ? sentence[0] : paragraph;
}

// Hosted demo serves a cached snapshot, not a live feed: decks that promise
// "live"/"right now"/"today" are reworded there (demo mode only — deckLine).
const DEMO_DECK: Record<string, string> = {
  holdings: "How each position performed in the snapshot",
  quotes: "Prices for the symbols tracked, as of the snapshot",
  "markets-board": "How every major asset class traded in the snapshot",
  "market-regime": "Which forces drove the snapshot's risk-on, risk-off tilt",
  "market-cross-asset": "How correlated different asset classes were in the snapshot",
  "lab-scanner": "Which symbols showed a chart pattern in the snapshot",
  "algo-blotter": "Status of every paper order and fill",
  "algo-strategies": "Which signal-driven strategies are configured, and a kill switch",
};

// Explicit deck copy, one per WIDGET_HELP id (kept 1:1 — see deckLine above).
// 6–10 words, sentence case, no trailing period, meaning not enumeration.
export const WIDGET_DECK: Record<string, string> = {
  "net-worth": "Holdings plus ledger cash, tracked over time",
  holdings: "How each position is performing right now",
  allocation: "Where concentration risk actually sits in the portfolio",
  "account-tree": "Cash and bank balances rolled up by account",
  "currency-breakdown": "How exposed your cash is to each currency",
  quotes: "Live prices for the symbols you're tracking",
  news: "A running feed of financial headlines",
  "markets-board": "How every major asset class is trading today",
  "market-composite": "A quick gut check on the market's mood",
  "macro-heatmap": "Which economies are running hot or cooling off",
  "recession-signals": "Whether the yield curve is flashing a recession warning",
  "econ-calendar": "Which economic releases are due and when",
  "yield-curve": "Whether short rates are pricing in trouble ahead",
  "risk-xray": "Where portfolio risk actually comes from",
  "risk-var": "The loss you could plausibly take in a bad day",
  "fx-drag": "How much of your return is just currency moves",
  "stress-test": "How today's portfolio would fare in past crises",
  "signal-scoreboard": "Which signals have actually predicted returns, ranked",
  "signal-grade-card": "Whether one signal's edge is real or noise",
  "backtest-lab": "Where a trading idea gets tested before real money",
  "price-chart": "A closer technical read on one symbol at a time",
  "geo-map": "Where geopolitical and market-moving events are happening",
  "macro-series": "How one economic indicator has moved over time",
  "rate-spreads": "The two spreads' history, plotted against zero",
  "backtest-report": "Whether a finished strategy actually beat its benchmark",
  "market-regime": "Which forces are driving today's risk-on, risk-off tilt",
  "market-cross-asset": "How correlated different asset classes are right now",
  "market-rotation": "Which sectors and factors are gaining relative strength",
  "market-rrg": "Which way each group is rotating, not just where",
  "market-volatility": "Whether volatility itself is signalling calm or stress",
  "market-conditions": "How tight or loose money currently is",
  "market-valuation": "Whether stocks look cheap or expensive by history",
  "market-sentiment": "How fearful or greedy the market currently is",
  "market-breadth": "Whether a move is broad-based or a handful of names",
  "research-fundamentals": "One company's financial health, straight from its filings",
  "research-valuation": "Whether one stock is priced cheap or rich",
  "research-scorecard": "Whether a company's books hold up to scrutiny",
  "research-technicals": "One stock's trend and momentum at a glance",
  "research-screen": "Which companies in the universe actually meet the bar",
  "lab-factor": "Whether a factor has real, out-of-sample predictive power",
  "lab-models": "Whether a trained model actually generalises out of sample",
  "lab-scanner": "Which symbols are showing a chart pattern right now",
  "ai-news": "Headlines enriched with sentiment, geography, and a grounded summary",
  "ai-intel-runner": "Where AI news enrichment gets triggered and tracked",
  "ai-status": "Whether the AI enrichment provider is actually reachable",
  "algo-ticket": "Manual order entry into the paper trading book",
  "algo-blotter": "Live status of every paper order and fill",
  "algo-positions": "What the paper book holds and how it's doing",
  "algo-strategies": "Which signal-driven strategies are live, and a kill switch",
  "home-intro": "Where to start, and what this terminal actually is",
  "report-view": "This week's full advisor report, decision to decision",
  "report-archive": "Every past weekly report, one click to reopen",
  "advisor-scorecard": "Whether following the advisor actually beat doing nothing",
};

export const WIDGET_HELP: Record<string, string> = {
  "net-worth":
    "Your net worth over time — your imported portfolio plus the bank/wallet cash tracked in the Beancount ledger.\n\nThe big figure is the latest value; the area chart is its history. Use the All / Investments toggle to switch between full net worth (holdings + cash) and just market-priced investments. 6M / 12M / 24M sets the lookback. The chips split the total into INVEST (holdings), PORTFOLIO CASH (cash held in the portfolio's accounts), and BANK (ledger cash).",
  holdings:
    "Every position in your imported portfolio — quantity, latest price, market value and unrealised P&L — under a summary header.\n\nSwitch between a per-asset and a per-account view in the header. Values are in EUR; P&L is coloured green/red by sign.",
  allocation:
    "How your imported portfolio is split — a donut plus a legend.\n\nToggle By class (crypto / equity / ETF / cash …) or By account. Each slice shows its weight % and EUR value. Use it to spot concentration at a glance.",
  "account-tree":
    "The Beancount ledger's account balances as an indented, multi-currency tree.\n\nParent accounts roll up their children. This reflects the real bank/cash ledger — investment lots live in the Holdings widget, not here.",
  "currency-breakdown":
    "Your ledger holdings grouped by currency — units, reporting-currency value, and each currency's share as a bar.\n\nUse it to read EUR / USD / GBP exposure.",
  quotes:
    "A live quote table for a watchlist of symbols, streamed over WebSocket.\n\nEdit the symbol list in the header. Prices flash green/red on moves; a muted value means the feed is missing that symbol — not a price drop.",
  news:
    "Aggregated finance headlines from the configured RSS feeds, polled periodically.\n\nClick a headline to open the source; the source and age show beneath each. This is the markets news feed — it lives on the Markets tab.",
  "markets-board":
    "A cross-asset board — global indices, FX, commodities, bond yields and crypto — each with its % change.\n\nUse the group selector (or All) to filter. Green/red is the day's move. Data is one batched yfinance + CoinGecko pull, cached server-side.",
  "market-composite":
    "A single risk-on / risk-off read, computed from the indices' average % change plus advance/decline breadth.\n\nGreen leans risk-on, red risk-off. A quick market-mood gauge, not a trading signal.",
  "macro-heatmap":
    "A multi-country grid of macro indicators (CPI, core, PPI, unemployment, real GDP), year-over-year, colour-coded against economic thresholds.\n\nRows are indicators, columns countries; hotter colours flag readings worth attention. Free econdb data — an empty cell means the source rate-limited and will self-fill on recovery.",
  "recession-signals":
    "A composite recession gauge from the US yield curve: the 10y-2y and 10y-3m spreads plus their 1-month trend.\n\nAn inverted (negative) spread has historically preceded recessions. Read the flag and the two spreads together.",
  "econ-calendar":
    "Upcoming US economic releases (FRED).\n\nNeeds the free OPENBB_FRED_API_KEY set; without it the panel shows a needs-key note. Use it to see which prints are due.",
  "yield-curve":
    "A snapshot of the government yield curve (US Treasuries or EU bunds) across maturities, with the 10y-2y / 10y-3m spreads and an inversion flag.\n\nSet the region in the widget config. A downward-sloping (inverted) curve is the classic recession warning.",
  "risk-xray":
    "A risk decomposition of your portfolio: beta (vs S&P + BTC), annualised volatility, max drawdown, concentration (HHI / effective N), exposure by currency / geography / asset class, and the top risk contributors.\n\nEverything is computed from daily history, not fetched. Use it to see where portfolio risk actually sits.",
  "risk-var":
    "Value-at-Risk and Conditional VaR at 95% and 99%, by historical simulation, with a Gaussian fat-tail comparison.\n\nVaR95 ≈ the loss you'd exceed about one day in twenty; CVaR is the average loss beyond it. A fat-tail ratio above 1 means real tails are heavier than a normal distribution implies.",
  "fx-drag":
    "Splits each holding's EUR return into its local-currency return and the FX contribution.\n\nShows how much of your performance is the asset moving vs. the euro moving. Currency is inferred from the symbol map — a stated modelling assumption, since the portfolio source reports in EUR only.",
  "stress-test":
    "Applies static historical shock scenarios (GFC, COVID, crypto winter, rate shock) to your current portfolio weights.\n\nEach row is the modelled hit under that scenario. A what-if on today's book, not a forecast.",
  "signal-scoreboard":
    "A leaderboard of every logged signal, ranked by ICIR / Rank-IC — how well its scores predicted realised forward returns.\n\nGrading is lookahead-clean (entry strictly after the signal date). Use the seed button to log the reference factors; click a row to open its grade card.",
  "signal-grade-card":
    "A deep grade for one signal: Rank-IC over time, the quintile long/short spread, and ICIR / t-stat.\n\nA significant positive t-stat (≳2) means the signal genuinely discriminates. All graded against F0 forward returns, settle-buffered against late restatement.",
  "backtest-lab":
    "Configure and run a signal-driven backtest off the main API: pick a signal, universe, rebalance, quantile, costs — then Run.\n\nProgress streams live; the equity curve and metric bundle appear inline when it finishes. Pre-flight rejects bad specs before anything runs. Recent runs are listed below.",
  "price-chart":
    "A candlestick price chart for one symbol, with source / interval controls.\n\nType a symbol (or add one via ⌘K). Daily bars are persisted server-side; intraday is passthrough. Used for technical reads.",
  "geo-map":
    "An interactive world map with toggleable layers: financial hubs (economic), sanctions, trade routes, and live USGS earthquakes.\n\nToggle layers with the checkboxes; click a feature for a popup. Pan and zoom like any map.",
  "macro-series":
    "A single macro time series (FRED or econdb) — CPI, GDP, rates and more — as a line chart.\n\nPick the series and transform (e.g. year-over-year) in config. FRED series need the free FRED key; econdb series are keyless.",
  "rate-spreads":
    "The history of the 10y-2y and 10y-3m Treasury spreads, drawn against a zero baseline.\n\nCrossing below zero (inversion) is the recession signal. Read it alongside the Rates tab's yield curves.",
  "backtest-report":
    "The full result of one finished backtest: equity curve vs. benchmark, drawdown, and the qlib metric bundle (cum / annualised return, volatility, Sharpe, info ratio, max drawdown, turnover, mean IC).\n\nMetrics are lookahead-clean and cost-adjusted. Read Sharpe, info ratio and max drawdown together — not return alone.",
  "market-regime":
    "A composite market regime score (−1 = full risk-off, +1 = full risk-on) built from momentum, breadth, credit, and volatility components. The headline state badge and score are computed from free FRED + yfinance history — no API key required beyond the optional FRED key already configured.\n\nRead the per-component diverging bars to see which drivers are pushing the regime. The score history chart shows how the composite has evolved. A RECESSION badge lights up when the Sahm rule threshold is breached.",
  "market-cross-asset":
    "A correlation matrix (heatmap, blue = negative / red = positive) across a basket of asset classes — equities, bonds, commodities, USD, credit, and crypto — computed over a rolling window from F0 history.\n\nThe snapshot strip above shows 1D / 1W / 1M / YTD % changes per symbol. The key ratios (equity/bond, gold/USD, etc.) and an average pairwise correlation sparkline give a quick read on diversification and risk-on/off tilt.",
  "market-rotation":
    "A return heatmap table for sector ETFs and factor ETFs: columns are 1D / 1W / 1M / 3M / 6M / YTD return plus RS-vs-SPY over 3 months. Cell color intensity scales with magnitude — deep green = strong outperformance, deep red = underperformance.\n\nRows are grouped by sector and factor. Use the RS column to spot which groups are gaining or losing relative strength versus the benchmark.",
  "market-rrg":
    "A Relative Rotation Graph (RRG): x-axis is the RS-ratio (relative strength vs benchmark), y-axis is RS-momentum (rate of change of that ratio). The four quadrants are Leading (top-right), Weakening (bottom-right), Lagging (bottom-left), and Improving (top-left).\n\nEach series shows a tail of weekly observations so you can see the direction of travel. Data is computed from F0 history against a configurable benchmark. Symbols rotating clockwise through the quadrants follow the classic RRG cycle.",
  "market-volatility":
    "A volatility regime dashboard: the big VIX level and its historical percentile, the term structure state (contango = calm / backwardation = stress) with the VIX3M ratio, realized volatility, the implied-minus-realized premium, and the MOVE bond volatility index.\n\nAll data is from free FRED and yfinance history. The VIX history chart shows the full lookback. A VIX above 25 flags elevated stress; backwardation in the term structure is the classic short-vol warning.",
  "market-conditions":
    "A tile grid of financial conditions indicators — credit spreads, the yield curve, dollar index, liquidity proxies, and more — each showing its current value, a centred z-score bar (negative = accommodative, positive = tight), and a historical percentile.\n\nThe trend arrow shows the direction of the most recent move. Color follows risk direction: for risk-off indicators a high z-score tiles red; for risk-on indicators it tiles green. Computed from free FRED + yfinance history.",
  "market-valuation":
    "Equity valuation metrics — CAPE (Shiller P/E), trailing P/E, the Buffett indicator (market-cap/GDP), and real 10-year yield — each with its current value and a 0-100 historical percentile bar. Red bar = expensive vs history, green = cheap.\n\nMetrics marked 'pending v1.1' require a data source not yet wired; the note explains which. All available metrics use free FRED + yfinance history — no additional keys needed beyond the optional FRED key.",
  "market-sentiment":
    "CNN Fear & Greed composite score (0-100), its seven component drivers, and CFTC Commitments of Traders positioning for key futures markets.\n\nThe gauge shows the current score with a 0-100 gradient bar; scores ≤45 are red (fear), neutral is grey, ≥55 are green (greed). The sparkline shows ~4 months of daily history. The component section breaks down the seven F&G drivers (momentum, strength, breadth, put/call, volatility, junk bond demand, safe haven demand). The COT table shows net speculator positioning per asset class — a positive z-score bar (green) means positioning is historically bullish relative to the past year; negative (red) means bearish. The percentile column shows where current net positioning ranks vs the prior year.",
  "market-breadth":
    "NYSE/Nasdaq composite advance-decline breadth for each exchange (NYSE, NASDAQ, NYSE American, NYSE Arca), sourced from WSJ. The primary market row is emphasised.\n\nColumns: A/D (raw advances/declines), BREADTH% (advances as a share of advancing+declining issues — above 50% = more advancers, green; below 50% = more decliners, red), NET H−L (new 52-week highs minus lows, signed green/red), U/D VOL (up-volume divided by down-volume, >1 = buying dominates), and TRIN (Arms index — ≈1 neutral, >1 selling pressure, <1 buying pressure).\n\nThe A/D line section accumulates net advances over time as a sparkline. McClellan oscillator and summation index appear once at least 40 sessions have been logged; until then a warming-up note shows the progress.",
  "research-fundamentals":
    "Annual financial statements for one equity — income statement, balance sheet, cash flow, and margins — pulled from yfinance. Type a ticker in the header and press Enter to switch symbols. Columns are fiscal years (newest first, up to five); rows are keyed line items in the company's reporting currency.\n\nStatement magnitudes are shown compactly (B/M/k); margins and returns rows show percentages and are coloured green when positive, red when negative.",
  "research-valuation":
    "Live price, market cap, and key valuation multiples for one equity — P/E, P/B, P/S, EV/EBITDA, dividend yield, ROE, ROA, and more — pulled from yfinance. Type a ticker in the header and press Enter to switch symbols.\n\nEach tile shows the label and formatted value. Return-oriented metrics (ROE, ROA, FCF yield, dividend yield) are coloured green when positive and red when negative; plain multiples stay neutral.",
  "research-scorecard":
    "Three forensic accounting scores for one equity. Piotroski F-Score (0–9): higher is better — 7–9 flags a financially strong company, 0–2 suggests weakness or elevated short interest. Altman Z-Score: Z > 2.99 is in the Safe zone, 1.81–2.99 is the Grey Zone, Z < 1.81 signals financial distress. Beneish M-Score: a score above −2.22 flags elevated earnings-manipulation risk; below −2.22 is considered low risk.\n\nType a ticker in the header and press Enter to switch symbols. Component rows show each check that passed (✓ green), failed (✗ red), or could not be computed (– muted). Warnings at the bottom flag missing inputs.",
  "research-technicals":
    "Price, 50-day SMA (blue), and 200-day SMA (purple) line chart for one equity, built from the F0 daily price history. Type a ticker in the header and press Enter to switch symbols.\n\nThe stat strip below the chart shows: the last close price; distance from the 200-DMA (positive = above, green; negative = below, red); the 52-week low–high range with a position bar showing where the current price sits; RSI-14 (above 70 = overbought/red, below 30 = oversold/green); and a trend badge (uptrend green / downtrend red / mixed).",
  "research-screen":
    "Screen the F0 equity universe by fundamental and forensic filters. Set Min F-Score, Max P/E, Min Dividend Yield (%), and Min Z-Score, then press Apply to filter. The header shows how many companies pass out of the total universe.\n\nResults are sorted by Piotroski F-Score (highest first). F ≥ 7 is green / F ≤ 2 is red; Z > 2.99 is green (safe) / Z < 1.81 is red (distress); M > −2.22 is amber (elevated earnings-manipulation risk). Hover a row to see the company name and sector.",
  "lab-factor":
    "Factor research (Quant/ML Lab, Phase 11). Pick a computed factor (momentum, reversal, low-vol, trend, RSI, volume) and a forward horizon, then Run: the study runs OFF the main API in the heavy qlib-lab worker over the lab universe's F0 history.\n\nThe result is the classic alpha-factor evaluation — the cross-sectional Rank-IC each month, its mean / IR / t-stat / hit-rate, the IC time series, and the quantile-bucket forward returns with the top-minus-bottom long-short spread. |IC mean| ≳ 0.03 with IR ≳ 0.3 is a respectable factor; the t-stat tells you if it is distinguishable from noise.\n\nNeeds --profile lab running.",
  "lab-models":
    "ML model zoo (Quant/ML Lab, Phase 11). Train a return-prediction model on the cross-sectional factor panel with a time-ordered holdout (no shuffle — no leakage). Engine sklearn-ensemble (XGBoost + RandomForest) ships and always runs; deep-zoo (torch MLP / LSTM / Transformer) runs if torch is in the image.\n\nRuns OFF-API with live progress. Results: test Rank-IC, R², hit-rate, the top-minus-bottom decile spread, and averaged feature importances. Rank-IC is the metric that matters — > 0 on the holdout means the model has out-of-sample signal.\n\nNeeds --profile lab running.",
  "lab-scanner":
    "Technical scanner (Quant/ML Lab, Phase 11). Detects chart patterns across the lab universe over the F0 OHLCV backbone — engulfing / hammer-star candles, Keltner-channel breaks, volume spikes, crash / moonshot moves, and bull / bear traps. This is synchronous (no worker needed).\n\nEach hit shows the matched patterns and a net bias score (sum of +1 bullish / −1 bearish). Rows sort by |score|. Tick Emit to POST the detections as Phase-9 scan_<pattern> signals so they feed the signal scoreboard's Rank-IC grading.",
  "ai-news":
    "RSS headlines enriched by the ai-runner worker through Claude Code: sentiment, a country tag (for the geo-map), topic labels, and a grounded summary.\n\nThe small dot marks whether the summary is grounded in the article text or unverifiable. Only http(s) links are clickable — anything else renders as plain text. Run Enrich now (Intel Runner) to populate this feed.",
  "ai-intel-runner":
    "Triggers a news-enrichment run off the main API — the ai-runner worker (--profile ai) pulls RSS, sends each story through Claude Code, and writes back sentiment / topics / summaries.\n\nEnrich now enqueues the job; progress streams live below via SSE. When it finishes, refresh AI News to see the results.",
  "ai-status":
    "Health check for the Claude Code provider used by the ai-runner worker.\n\nThe API process itself always uses the no-op provider (it never calls an LLM inline) — this card reflects the worker's configured provider and whether it's currently reachable.\n\nOperationally: the worker only runs in the ai-runner container, started with --profile ai. OFF means the API's own provider is the no-op stub (ai-runner isn't running or isn't configured); ON/DOWN reflect the worker's actual provider and its last health check.",
  "algo-ticket":
    "Manual order entry against the paper trading OMS. Set symbol, side, type (market/limit) and quantity — a debounced live quote previews the current price as you type the symbol.\n\nSubmitting validates and enqueues the order; a structured error (bad symbol, kill switch engaged, etc) shows as the error line. A red banner appears when the kill switch is engaged — new orders will be rejected until it's resumed from the Strategy Panel.",
  "algo-blotter":
    "Live order and fill history for the paper OMS. Orders are colour-coded by status (filled green / rejected red / accepted amber / cancelled grey) with the rejection reason shown inline; Cancel is available while an order is still new or accepted.\n\nThe panel subscribes to the algo event stream (SSE) so fills and status changes refresh immediately, with a 15-second poll as a fallback.",
  "algo-positions":
    "Open paper-trading positions — quantity, average cost, current mark, and unrealised P&L (green/red by sign) — under an account strip of cash, equity, and day P&L.\n\nThe sparkline plots the account's daily equity snapshots so you can see the paper book's trajectory at a glance.",
  "algo-strategies":
    "Signal-driven strategy management for the paper OMS. Each strategy ranks a logged signal's cross-section on a cadence (daily/weekly/monthly) and rebalances into its top-N names; start/pause/delete control an existing strategy, and the form below creates a new one over a comma-separated universe.\n\nThe engine dot reflects the trading-engine worker's heartbeat. KILL SWITCH immediately halts all strategies from submitting new orders (confirmed before firing); RESUME re-enables them once you're ready.",
  "home-intro":
    "The Home tab's welcome banner — the CATALLAX wordmark, a one-line description of what this terminal is, and a start-here row that jumps straight to Personal, Markets, or Market Research.\n\nIn demo mode a note reminds you the figures come from a sample portfolio, not actual holdings. Press ⌘K anywhere to jump directly to any tab, widget, or symbol.",
  "report-view":
    "The full weekly advisor report, in reading order: a one-line narration state (or the AI narration itself, when one ran), What happened (NAV, cash, regime, held positions), Where we stand (every rule's evaluation this week — fired or not), What to do next (proposed actions you can Accept or Override, any refusal as an alarm, and near-miss rules that evaluated close to firing but didn't), and the Scorecard (actual-vs-do-nothing performance with its full N/depth/p-floor).\n\nType a report week (e.g. \"2026-W33\") in the header, or leave it blank for the newest report. Accept/Override require a written reason and are recorded permanently — there is no undo. Disabled in demo mode.",
  "report-archive":
    "Every week that has a report, newest first. Click one to open a Weekly Report panel pinned to that week (the same \"open a new panel\" pattern the command palette's GO <SYMBOL> uses for a price chart).",
  "advisor-scorecard":
    "The two-arm performance scorecard: your actual (possibly rebalanced) portfolio's time-weighted return against a frozen do-nothing baseline over the same weeks, plus the rule-evaluation counts and each rule's status.\n\nEvery figure carries its own honesty labels next to it — n_effective (how many weeks the two arms actually diverged; a tie is not evidence), depth (how many snapshot weeks exist at all), and a p-floor (the best possible significance a sign test over n_effective pairs could ever report, not an actual p-value). Read the return convention (one click) before trusting the TWR figures — it states the one real caveat: external deposits/withdrawals aren't separated from investment return yet.",
};
