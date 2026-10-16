// Mirrors the FastAPI response models. Money/decimal fields arrive as
// strings to preserve precision — parse to number only at the display/chart edge.

export interface Quote {
  symbol: string;
  price: string;
  currency: string;
  as_of: string;
  source: string;
}

// ── Analyst Cockpit ───────────────────────────────
export interface CockpitSession {
  session_key: string;
  title: string | null;
  created_at: string;
  updated_at: string;
}

export interface CockpitCitation {
  kind: string;
  id: number | string;
}

export interface CockpitMessage {
  id: number;
  role: "user" | "assistant";
  content: string;
  citations: CockpitCitation[] | null;
  refused: boolean;
  job_id: string | null;
  created_at: string;
}

export interface CockpitJob {
  status: "queued" | "running" | "done" | "failed";
  phase: string | null;
  frac: number;
  message?: string | null;
}

export interface Candle {
  ts: string;
  open: string;
  high: string;
  low: string;
  close: string;
  volume: string | null;
}

export interface SymbolMatch {
  symbol: string;
  name: string;
  asset_type: string;
}

export interface AccountNode {
  name: string;
  balances: Record<string, string>;
  children: AccountNode[];
}

export interface NetWorthPoint {
  date: string;
  value: string;
  currency: string;
}

export interface CurrencyTotal {
  currency: string;
  units: string;
  value: string;
  reporting_currency: string;
}

export interface NewsItem {
  title: string;
  link: string;
  source: string;
  published: string | null;
  summary: string | null;
}

export interface TickEntry {
  price?: string;
  currency?: string;
  as_of?: string;
  source?: string;
  error?: { code: string; message: string };
}

export interface Holding {
  symbol: string;
  asset_class: string;
  quantity: string;
  cost: string;
  value: string;
  pnl: string;
  pnl_pct: number;
  weight: number;
  currency: string;
  live_price: string | null;
  live_currency: string | null;
  live_source: string | null;
}

export interface AllocationSlice {
  key: string;
  value: string;
  weight: number;
}

export interface Allocation {
  currency: string;
  total: string;
  by_class: AllocationSlice[];
  by_symbol: AllocationSlice[];
}

export interface PortfolioSummary {
  currency: string;
  holdings_value: string;
  cash_value: string;
  total_value: string;
  total_cost: string;
  total_pnl: string;
  total_pnl_pct: number;
}

// ── Portfolio holdings (from the exporter service) ──────────
export interface WfHoldingAsset {
  symbol: string;
  name: string;
  asset_class: string;
  quantity: string;
  value: string;
}

export interface WfHoldingAccount {
  account: string;
  value: string;
  investments: string;
  cash: string;
}

export interface WealthfolioHoldings {
  base: string;
  as_of: string | null;
  investments: string;
  cash: string;
  total: string;
  by_asset: WfHoldingAsset[];
  by_account: WfHoldingAccount[];
}

export interface NetWorthComponents {
  investments: string; // exporter holdings market value
  wf_cash: string; // cash inside exporter accounts
  ledger_cash: string; // liquid bank/wallet cash from the ledger
}

// Net worth = imported portfolio + the ledger's bank/wallet cash.
export type NetWorthScope = "all" | "investments";
export interface CombinedNetWorth {
  currency: string;
  total: string;
  scope?: NetWorthScope;
  components: NetWorthComponents;
  points: NetWorthPoint[];
}

// ── Markets overview ──────────────────────────────
export interface MarketQuote {
  symbol: string;
  label: string;
  price: string;
  change: string;
  change_pct: number;
  currency: string;
}

export interface MarketGroup {
  key: string;
  label: string;
  items: MarketQuote[];
}

export interface MarketComposite {
  score: number;
  label: string; // Risk-On / Neutral / Risk-Off
  advancers: number;
  decliners: number;
  total: number;
}

export interface MarketsOverview {
  as_of: string;
  groups: MarketGroup[];
  composite: MarketComposite;
}

// Minimal GeoJSON the geo-map consumes (USGS quake proxy + static layers).
export interface GeoFeatureCollection {
  type: "FeatureCollection";
  features: Array<{
    type: "Feature";
    geometry: { type: string; coordinates: unknown };
    properties: Record<string, unknown>;
  }>;
}

// ── Result envelope — macro endpoints return data + provenance ──
export interface Envelope<T> {
  data: T;
  source: string;
  fetched_at: string;
  cache_hit: boolean;
  stale: boolean;
  warnings: string[];
}

// ── Macro & Economic dashboard ────────────────────
export interface MacroPoint {
  date: string;
  value: number;
}
export interface MacroSeries {
  series_id: string;
  title: string;
  unit: string; // "% YoY" | "%" | "level"
  points: MacroPoint[];
}
export interface IndicatorCell {
  root: string;
  label: string;
  country: string;
  value: number | null; // fraction; multiply by 100 for %
  date: string | null;
  transform: string; // "toya" (YoY) | "level"
}
export interface IndicatorBoard {
  countries: string[];
  cells: IndicatorCell[];
}
export interface CurvePoint {
  maturity: string; // "3M" / "2Y" / "10Y"
  years: number;
  rate: number; // percent
}
export interface YieldCurve {
  region: string; // US | EU
  date: string | null;
  points: CurvePoint[];
  spread_10y_2y: number | null;
  spread_10y_3m: number | null;
  inverted: boolean;
}
export interface SpreadPoint {
  date: string;
  t10y_2y: number | null;
  t10y_3m: number | null;
}
export interface RateRow {
  key: string;
  label: string;
  value: number | null; // percent
  date: string | null;
}
export interface RatesData {
  latest: RateRow[];
  spreads: SpreadPoint[];
}
export interface CalendarEvent {
  date: string | null;
  country: string | null;
  event: string;
  actual: string | null;
  previous: string | null;
  consensus: string | null;
}
export interface CalendarData {
  events: CalendarEvent[];
  needs_key: boolean;
}

// ── Portfolio Risk lens (Phase 8.5) ─────────────────────────
export interface RiskCoverage {
  n_assets_total: number;
  n_covered: number;
  n_skipped: number;
  covered_weight_pct: number;
  n_observations: number;
  window_start: string | null;
  window_end: string | null;
  by_reason: Record<string, number>;
  cash_weight_pct: number;
  assumption: string;
}
export interface RiskBucket {
  bucket: string;
  weight_pct: number;
  value_eur: number;
}
export interface RiskSlice {
  key: string;
  weight_pct: number;
  value_eur: number;
}
export interface RiskContributor {
  symbol: string;
  contribution_pct: number;
  weight_pct: number;
}
export interface RiskXray {
  base_currency: string;
  as_of: string;
  lookback_days: number;
  n_observations: number;
  total_value_eur: number;
  vol_daily_pct: number | null;
  vol_annual_pct: number | null;
  beta: number | null;
  beta_vs_sp500: number | null;
  beta_vs_btc: number | null;
  benchmark: string;
  r_squared: number | null;
  alpha_annual_pct: number | null;
  max_drawdown_pct: number | null;
  max_drawdown_peak: string | null;
  max_drawdown_trough: string | null;
  max_drawdown_recovery: string | null;
  hhi: number;
  effective_n: number;
  top1_pct: number;
  top3_pct: number;
  top5_pct: number;
  concentration_by_class: RiskSlice[];
  top_holdings: RiskSlice[];
  risk_contributors: RiskContributor[];
  exposure_by_class: RiskBucket[];
  exposure_by_currency: RiskBucket[];
  exposure_by_geo: RiskBucket[];
  coverage: RiskCoverage;
}
export interface VarReport {
  base_currency: string;
  as_of: string;
  method: string;
  confidence: number;
  horizon_days: number;
  n_scenarios: number;
  total_value_eur: number;
  var_eur: number | null;
  var_pct: number | null;
  cvar_eur: number | null;
  cvar_pct: number | null;
  var99_eur: number | null;
  cvar99_eur: number | null;
  gaussian_var_eur: number | null;
  fat_tail_ratio: number | null;
  hist_vs_gaussian: number | null;
  worst_day: string | null;
  worst_day_pct: number | null;
  low_confidence: boolean;
  coverage: RiskCoverage;
}
export interface FxAsset {
  symbol: string;
  currency: string;
  currency_source: string;
  weight_pct: number;
  local_return_pct: number | null;
  eur_return_pct: number | null;
  fx_contrib_pct: number | null;
}
export interface FxBucket {
  currency: string;
  weight_pct: number;
  fx_drag_pct: number;
}
export interface FxDragReport {
  base_currency: string;
  as_of: string;
  lookback_days: number;
  portfolio_eur_return_pct: number | null;
  portfolio_local_return_pct: number | null;
  portfolio_fx_drag_pct: number | null;
  cross_term_pct: number | null;
  by_currency: FxBucket[];
  by_asset: FxAsset[];
  excluded: Array<{ symbol: string; reason: string }>;
  assumption: string;
  coverage: RiskCoverage;
}
export interface StressScenario {
  name: string;
  portfolio_return_pct: number;
  portfolio_pnl_eur: number;
  by_class: RiskSlice[];
  worst: RiskSlice[];
  synthetic_classes: string[];
}
export interface StressReport {
  base_currency: string;
  as_of: string;
  total_value_eur: number;
  mode: string;
  scenarios: StressScenario[];
  assumption: string;
}

// ── Signal self-scoring core ──────────────────────
export interface SignalCatalogEntry {
  signal_id: string;
  n_rows: number;
  n_symbols: number;
  n_dates: number;
  first_date: string | null;
  last_date: string | null;
}
export interface SignalCatalog {
  signals: SignalCatalogEntry[];
}
export interface ICPoint {
  date: string;
  n_names: number;
  rank_ic: number | null;
  pearson_ic: number | null;
  ls_spread_pct: number | null;
}
export interface QuantileBucket {
  bucket: number; // 1 = lowest score, Q = highest
  mean_forward_return_pct: number | null;
  n_obs: number;
}
export interface SignalCoverage {
  n_rows: number;
  n_pairs_matured: number;
  n_pairs_pending: number;
  n_pairs_uncovered: number;
  n_dates_total: number;
  n_dates_graded: number;
  mean_names_per_date: number | null;
  window_start: string | null;
  window_end: string | null;
  by_status: Record<string, number>;
}
export interface SignalGrade {
  signal_id: string;
  as_of: string;
  horizon_bars: number;
  quantiles: number;
  settle_days: number;
  methodology: string;
  n_dates_graded: number;
  mean_rank_ic: number | null;
  mean_ic: number | null;
  icir: number | null;
  t_stat: number | null;
  p_value: number | null;
  significant: boolean;
  low_confidence: boolean;
  ls_spread_mean_pct: number | null;
  ls_spread_t: number | null;
  ls_cumulative_pct: number | null;
  monotonicity: number | null;
  hit_rate: number | null;
  hit_rate_wilson_lb: number | null;
  n_hit_pairs: number;
  long_mean_fwd_pct: number | null;
  short_mean_fwd_pct: number | null;
  per_date: ICPoint[];
  quantile_buckets: QuantileBucket[];
  coverage: SignalCoverage;
  warnings: string[];
}
export interface SignalGradeSummary {
  signal_id: string;
  horizon_bars: number;
  methodology: string;
  n_dates_graded: number;
  mean_rank_ic: number | null;
  icir: number | null;
  t_stat: number | null;
  significant: boolean;
  low_confidence: boolean;
  ls_spread_mean_pct: number | null;
  ls_cumulative_pct: number | null;
  hit_rate: number | null;
  hit_rate_wilson_lb: number | null;
  n_pairs_matured: number;
  n_pairs_pending: number;
}
export interface Scoreboard {
  as_of: string;
  horizon_bars: number;
  quantiles: number;
  rows: SignalGradeSummary[];
  warnings: string[];
}
export interface SignalEmitResult {
  n_received: number;
  n_written: number;
  signal_ids: string[];
}

// ── Market Research (Phase 10a) ─────────────────────────────
export interface RegimeComponent {
  key: string;
  label: string;
  value: number | null;
  score: number | null;
  weight: number;
  contribution: number | null;
}
export interface RegimeHistPoint {
  date: string;
  score: number | null;
}
export interface MarketRegime {
  as_of: string;
  state: string;
  score: number | null;
  components: RegimeComponent[];
  history: RegimeHistPoint[];
  recession_flag: boolean;
  sahm: number | null;
}
export interface CrossAssetSnapshot {
  symbol: string;
  label: string;
  last: number | null;
  chg_1d_pct: number | null;
  chg_1w_pct: number | null;
  chg_1m_pct: number | null;
  chg_ytd_pct: number | null;
}
export interface CorrHistPoint {
  date: string;
  value: number | null;
}
export interface RatioItem {
  key: string;
  label: string;
  value: number | null;
  chg_1m_pct: number | null;
}
export interface CrossAssetReport {
  as_of: string;
  window_days: number;
  symbols: string[];
  snapshot: CrossAssetSnapshot[];
  matrix: (number | null)[][];
  avg_pairwise_corr: number | null;
  avg_corr_history: CorrHistPoint[];
  ratios: RatioItem[];
}
export interface RotationRow {
  symbol: string;
  label: string;
  group: string;
  ret_1d: number | null;
  ret_1w: number | null;
  ret_1m: number | null;
  ret_3m: number | null;
  ret_6m: number | null;
  ret_ytd: number | null;
  rs_vs_bench_3m: number | null;
}
export interface RotationReport {
  as_of: string;
  benchmark: string;
  rows: RotationRow[];
}
export interface RRGTailPoint {
  date: string;
  rs_ratio: number | null;
  rs_momentum: number | null;
}
export interface RRGSeries {
  symbol: string;
  label: string;
  group: string;
  quadrant: string;
  tail: RRGTailPoint[];
}
export interface RRGReport {
  as_of: string;
  benchmark: string;
  tail_weeks: number;
  series: RRGSeries[];
}
export interface VolatilityReport {
  as_of: string;
  vix: number | null;
  vix_percentile: number | null;
  vix3m: number | null;
  term_ratio: number | null;
  term_state: string;
  realized_vol_pct: number | null;
  implied_minus_realized: number | null;
  move: number | null;
  vix_history: { date: string; value: number | null }[];
}
export interface ConditionsTile {
  key: string;
  label: string;
  value: number | null;
  unit: string;
  zscore: number | null;
  percentile: number | null;
  trend: string;
  date: string | null;
  higher_is_risk_off: boolean;
}
export interface ConditionsReport {
  as_of: string;
  tiles: ConditionsTile[];
}
export interface ValuationMetric {
  key: string;
  label: string;
  value: number | null;
  percentile: number | null;
  note: string;
  needs_source: boolean;
}
export interface ValuationReport {
  as_of: string;
  metrics: ValuationMetric[];
}
export interface SentimentComponent {
  key: string;
  label: string;
  score: number | null;
  rating: string | null;
}
export interface FearGreedHistPoint {
  date: string;
  score: number | null;
}
export interface CotRow {
  code: string;
  label: string;
  net: number | null;
  oi: number | null;
  pct_oi: number | null;
  z_1y: number | null;
  percentile: number | null;
  change_1w: number | null;
  inverse: boolean;
  report_date: string | null;
}
export interface SentimentReport {
  as_of: string;
  fg_score: number | null;
  fg_rating: string | null;
  fg_prev_close: number | null;
  fg_prev_week: number | null;
  fg_prev_month: number | null;
  fg_prev_year: number | null;
  fg_components: SentimentComponent[];
  fg_history: FearGreedHistPoint[];
  cot_as_of: string | null;
  cot: CotRow[];
}

export interface BreadthMarket {
  market: string;
  issues: number | null;
  advances: number | null;
  declines: number | null;
  unchanged: number | null;
  breadth_pct: number | null;    // advances/(adv+decl)*100
  ad_ratio: number | null;       // advances/declines
  new_highs: number | null;
  new_lows: number | null;
  net_highs: number | null;      // new_highs - new_lows (+ green, − red)
  updown_vol_ratio: number | null; // adv_volume/decl_volume
  trin: number | null;           // Arms index (≈1 neutral)
}
export interface BreadthHistPoint {
  date: string;
  value: number | null;
}
export interface BreadthReport {
  as_of: string;
  session_date: string | null;
  primary_market: string;
  markets: BreadthMarket[];
  history_days: number;
  warming_up: boolean;       // true while < 40 sessions — oscillators not yet reliable
  ad_line: BreadthHistPoint[];
  mcclellan_osc: number | null;
  mcclellan_summation: number | null;
  mcclellan_history: BreadthHistPoint[];
}

// ── Backtester (Phase 9, engine) ────────────────────────────
// Declarative, signal-driven strategy spec (NO code execution). The strategy IS a
// logged signal_id: rank its cross-section each rebalance, hold the top quantile.
export interface BacktestConfigInput {
  name: string;
  engine?: string;
  signal_id: string;
  universe?: string[] | null;
  start: string; // YYYY-MM-DD
  end: string; // YYYY-MM-DD
  rebalance?: "W" | "M" | "Q";
  quantile?: number;
  long_short?: boolean;
  weighting?: "equal" | "score";
  costs_bps?: number;
  benchmark_symbol?: string | null;
  rf_annual?: number;
}
export interface BacktestJob {
  job_id: string;
  name: string;
  signal_id: string;
  engine: string;
  status: string; // queued | running | done | failed
  progress: number;
  phase: string | null;
  created_at: string;
  updated_at: string;
  has_result: boolean;
  error: { error?: { code: string; message: string } } | null;
  warnings: string[];
}
export interface BacktestList {
  jobs: BacktestJob[];
}
export interface EquityPoint {
  date: string;
  value: number;
}
export interface DrawdownPoint {
  date: string;
  value: number; // drawdown %
}
export interface RebalancePoint {
  date: string;
  entry_date: string | null;
  n_long: number;
  n_short: number;
  turnover: number;
  ic: number | null;
  status: string; // matured | pending
}
export interface BacktestMetrics {
  cum_return_pct: number | null;
  ann_return_pct: number | null;
  ann_vol_pct: number | null;
  sharpe: number | null;
  info_ratio: number | null;
  max_drawdown_pct: number | null;
  turnover_annualised: number | null;
  mean_ic: number | null;
  n_rebalances: number;
  n_matured_periods: number;
  n_pending_periods: number;
  n_names_avg: number | null;
  n_days: number;
  benchmark: string;
}
export interface BacktestReport {
  signal_id: string;
  engine: string;
  name: string;
  window_start: string | null;
  window_end: string | null;
  equity: EquityPoint[];
  drawdown: DrawdownPoint[];
  benchmark_equity: EquityPoint[] | null;
  rebalances: RebalancePoint[];
  metrics: BacktestMetrics;
  warnings: string[];
}

// ── Equity Research & Screening (Phase 10b) ─────────────────
// Single-name fundamentals + forensic scores (Piotroski / Altman / Beneish) +
// technicals from the F0 backbone + a universe screen. Names are Research-prefixed
// to avoid colliding with the Phase-10a market ValuationReport above.
export interface StatementLine {
  key: string;
  label: string;
  values: (number | null)[]; // one per fiscal year, newest first
}
export interface ResearchFundamentals {
  symbol: string;
  name: string;
  sector: string | null;
  industry: string | null;
  currency: string | null;
  fiscal_years: string[];
  income: StatementLine[];
  balance: StatementLine[];
  cashflow: StatementLine[];
  margins: StatementLine[]; // derived %: gross/op/net margin, ROE, ROA per year
}
export interface ValuationRatio {
  key: string;
  label: string;
  value: number | null;
  unit: string; // "x" | "%" | "ratio"
}
export interface ResearchValuation {
  symbol: string;
  name: string;
  currency: string | null;
  price: number | null;
  market_cap: number | null;
  ratios: ValuationRatio[];
}
export interface ScoreComponent {
  key: string;
  label: string;
  passed: boolean | null; // null = input unavailable (not counted / not scored)
  detail: string;
}
export interface ScoreCard {
  key: string; // "piotroski" | "altman" | "beneish"
  label: string;
  score: number | null;
  scale: string; // human band, e.g. "0–9" / "Z > 2.99 safe"
  verdict: string | null;
  components: ScoreComponent[];
}
export interface ResearchScores {
  symbol: string;
  name: string;
  fiscal_years: string[];
  cards: ScoreCard[];
  warnings: string[];
}
export interface TechPoint {
  date: string;
  close: number | null;
  sma50: number | null;
  sma200: number | null;
}
export interface ResearchTechnicals {
  symbol: string;
  last: number | null;
  sma50: number | null;
  sma200: number | null;
  dist_sma200_pct: number | null;
  high_52w: number | null;
  low_52w: number | null;
  pct_of_52w_range: number | null;
  rsi_14: number | null;
  trend: string; // "uptrend" | "downtrend" | "mixed"
  series: TechPoint[];
}
export interface ScreenRow {
  symbol: string;
  name: string;
  sector: string | null;
  price: number | null;
  market_cap: number | null;
  pe: number | null;
  pb: number | null;
  ps: number | null;
  div_yield: number | null;
  roe: number | null;
  f_score: number | null;
  z_score: number | null;
  m_score: number | null;
}
export interface ResearchScreen {
  rows: ScreenRow[];
  universe_size: number;
  filtered: number;
  filters: Record<string, number>;
}
// Optional screen filters (all undefined = full universe). Mirrors the typed
// query params on GET /research/screen.
export interface ScreenFilters {
  min_f_score?: number;
  min_z_score?: number;
  max_m_score?: number;
  max_pe?: number;
  min_pe?: number;
  max_pb?: number;
  max_ps?: number;
  min_div_yield?: number;
  min_roe?: number;
  min_market_cap?: number;
}

// ── Quant / ML Lab ───────────────────────────────
// Off-API factor/model runs (mirror the backtester job shape) + the synchronous
// technical scanner. Lab-prefixed where a name would collide with Phase-9 types.
export interface LabJob {
  job_id: string;
  kind: "factor" | "model";
  name: string;
  engine: string;
  status: string; // queued | running | done | failed
  progress: number;
  phase: string | null;
  created_at: string;
  updated_at: string;
  has_result: boolean;
  error: Record<string, unknown> | null;
  warnings: string[];
}
export interface LabList {
  jobs: LabJob[];
}
export interface FactorConfigInput {
  name: string;
  factor: string;
  universe?: string[] | null;
  start?: string | null;
  end?: string | null;
  horizon_days?: number;
  quantiles?: number;
}
export interface ModelConfigInput {
  name: string;
  engine: string;
  model: string;
  universe?: string[] | null;
  start?: string | null;
  end?: string | null;
  horizon_days?: number;
  features?: string[] | null;
  test_fraction?: number;
}
export interface LabICPoint {
  date: string;
  ic: number | null;
}
export interface LabQuantileReturn {
  quantile: number;
  mean_fwd_ret: number | null;
  n_avg: number | null;
}
export interface FactorReport {
  name: string;
  factor: string;
  horizon_days: number;
  window_start: string | null;
  window_end: string | null;
  n_periods: number;
  n_symbols: number;
  n_obs: number;
  ic_mean: number | null;
  ic_std: number | null;
  ic_ir: number | null;
  ic_tstat: number | null;
  ic_hit_rate: number | null;
  ic_series: LabICPoint[];
  quantile_returns: LabQuantileReturn[];
  long_short_spread: number | null;
  warnings: string[];
}
export interface FeatureImportance {
  feature: string;
  importance: number | null;
}
export interface ModelMetrics {
  test_ic: number | null;
  test_r2: number | null;
  hit_rate: number | null;
  long_short_decile_spread: number | null;
  n_train: number;
  n_test: number;
  n_features: number;
}
export interface ModelReport {
  name: string;
  engine: string;
  model: string;
  horizon_days: number;
  window_start: string | null;
  window_end: string | null;
  metrics: ModelMetrics;
  feature_importance: FeatureImportance[];
  pred_quantile_returns: LabQuantileReturn[];
  warnings: string[];
}
export interface ScanPattern {
  name: string;
  label: string;
  bias: number; // +1 bullish, -1 bearish
  detail: string;
}
export interface ScanRow {
  symbol: string;
  label: string;
  close: number | null;
  score: number;
  patterns: ScanPattern[];
}
export interface ScanReport {
  as_of: string | null;
  n_symbols: number;
  n_hits: number;
  requested: string[];
  rows: ScanRow[];
  emitted: number;
  warnings: string[];
}
export interface LabEngines {
  engines: string[];
  default_engine: string;
  factors: string[];
  scan_patterns: { name: string; label: string }[];
}

// ── AI Intel ─────────────────────────────────────
// Intel-prefixed to avoid colliding with the plain NewsItem (RSS feed) above.
export interface IntelNewsItem {
  link: string;
  title: string;
  source: string | null;
  published: string | null;
  sentiment: "positive" | "neutral" | "negative" | null;
  sentiment_score: number | null;
  topics: string[];
  country: string | null;
  lat: number | null;
  lon: number | null;
  summary: string | null;
  cluster_id: string | null;
  grounding: string;
}
export interface IntelRun {
  job_id: string;
  kind: string;
  status: string; // queued | running | done | failed
  progress: number;
  phase: string | null;
  created_at: string;
  updated_at: string;
  error: Record<string, unknown> | null;
  warnings: string[];
}
export interface IntelStatus {
  provider: string;
  healthy: boolean;
}

// ── Paper-Trading Algo Hub ───────────────────────
// Algo-prefixed to avoid collisions (Phase-11 ICPoint precedent). Mirrors the
// `AlgoService`/router dict shapes exactly (backend/app/services/algo.py,
// backend/app/routers/algo.py) — verified against the live API: orders carry
// no `updated_at` (never tracked), account snapshots key off `date` (not
// `snapshot_date`), and `status()` reports `worker_heartbeat_fresh` (not
// `worker_alive`).
export interface AlgoOrder {
  id: number;
  strategy_id: number | null;
  symbol: string;
  side: "buy" | "sell";
  type: "market" | "limit";
  qty: number;
  limit_price: number | null;
  status: string;
  reason: string | null;
  created_at: string;
}
export interface AlgoOrderSpec {
  symbol: string;
  side: "buy" | "sell";
  type?: "market" | "limit";
  qty: number;
  limit_price?: number | null;
}
export interface AlgoFill {
  id: number;
  order_id: number;
  symbol: string;
  side: string;
  price: number;
  qty: number;
  commission: number;
  slippage_bps: number;
  filled_at: string;
}
export interface AlgoPosition {
  symbol: string;
  qty: number;
  avg_cost: number;
  mark: number | null;
  unrealized_pnl: number | null;
}
export interface AlgoAccountSnapshot {
  date: string;
  cash: number;
  positions_value: number;
  equity: number;
}
export interface AlgoAccount {
  cash: number;
  equity: number;
  positions_value: number;
  day_pnl: number | null;
  snapshots: AlgoAccountSnapshot[];
}
export interface AlgoStrategy {
  id: number;
  name: string;
  config: Record<string, unknown>;
  status: string;
  last_rebalance_at: string | null;
  created_at: string;
}
// POST /algo/strategies body — StrategyConfig fields (strategy_runner.py) + name.
export interface AlgoStrategyCreate {
  name: string;
  signal_id: string;
  universe: string[];
  top_n?: number;
  weighting?: "equal" | "score";
  rebalance?: "daily" | "weekly" | "monthly";
  max_position_pct?: number;
  max_orders_per_rebalance?: number;
}
export interface AlgoStatus {
  kill: boolean;
  worker_heartbeat_fresh: boolean;
  broker: string;
}
export interface AlgoReconciliationPosition {
  symbol: string;
  stored_qty: number;
  replayed_qty: number;
  match: boolean;
}
export interface AlgoReconciliationCash {
  stored: number;
  replayed: number;
  match: boolean;
}
export interface AlgoReconciliation {
  clean: boolean;
  positions: AlgoReconciliationPosition[];
  cash: AlgoReconciliationCash;
}

// ── Marquee ticker watchlist (Phase 14e) ────────────────────
// Persisted editable symbol list backing TickerBar. `source` unset (null)
// means "use the price source default"; labels are derived client-side.
export interface WatchlistItem {
  symbol: string;
  source: string | null;
  sort_order: number;
}
// PUT body — a plain ordered array of symbol/source pairs; the server
// assigns sort_order from array position.
export interface WatchlistItemInput {
  symbol: string;
  source?: string | null;
}

// Session boot (Phase 14a, beta gates). owner_name is null in demo mode (the
// frontend then greets without a name); auth_required/authenticated drive the
// login gate.
export interface Session {
  demo_mode: boolean;
  owner_name: string | null;
  auth_required: boolean;
  authenticated: boolean;
}

// ── Weekly Advisor reports ────────────────────────────
// Assembled fresh on every read (`services/advisor_report.assemble_report`)
// from `portfolio_snapshots` + `recommendations` rows — there is no
// persisted `reports` row/table, so this shape has to be defined once and
// is reused verbatim by the read route. `input_snapshot`/`tax_cost_arithmetic`
// are per-rule free-form JSONB (their keys vary by which of the three rules
// produced the row — weight_band_trim vs cash_floor_band vs dca_modulation)
// so both stay `Record<string, unknown>`; the UI renders whatever keys are
// present rather than assuming a fixed shape.
export interface ReportPosition {
  symbol: string;
  value_eur: number;
  qty: number;
}
// Structured would-fire-at figures for one `rule_evaluations` entry
// (`services/advisor_report.py::_rule_arithmetic`, committed fde86ce/
// 82759ab) — named fields only, absent-tolerant per key (not every rule's
// `input_snapshot`/`tax_cost_arithmetic` carries every field: `symbol` is
// absent on `cash_floor_band` rows; `benefit_eur`/`threshold_eur`/`fires`
// are the sell-gate's own fields, present only on `weight_band_trim` rows).
// `material` is the near-miss SIGNAL: present + `false` on an out-of-band
// hold too small to matter; present + `true` on one blocked only by the
// gate/cooldown; absent entirely on a plain in-band hold.
export interface RuleEvaluationArithmetic {
  symbol?: string;
  trim_value_eur?: number;
  shortfall_eur?: number;
  materiality_eur_threshold?: number;
  materiality_nav_pct_threshold?: number;
  benefit_eur?: number;
  threshold_eur?: number;
  cost_eur?: number;
  fires?: boolean;
  material?: boolean;
}
export interface RuleEvaluation {
  rule_id: string;
  action: string;
  status: string;
  note: string | null;
  // Always present as an object on today's payload (possibly `{}`) but kept
  // optional here defensively — the UI must render `note` verbatim as the
  // fallback and never re-derive these figures itself when a key is absent.
  arithmetic?: RuleEvaluationArithmetic;
}
// One `recommendations` row, as shaped by `routers/reports.py::_rec_dict`.
// `status` is one of evaluated/proposed/accepted/overridden/executed (the
// service layer is the enum authority, not the DB column). Only a
// `status: "proposed"` row is actionable via accept/override.
export interface Recommendation {
  id: number;
  rule_id: string;
  as_of: string;
  report_week: string;
  action: string;
  sizing: number | null;
  status: string;
  note: string | null;
  prior_id: number | null;
  input_snapshot: Record<string, unknown>;
  tax_cost_arithmetic: Record<string, unknown> | null;
}
// The append-only accept/override response: the newly inserted row plus the
// prior row it supersedes (proven byte-for-byte unchanged server-side).
export interface RecommendationTransition {
  new: Recommendation;
  prior: Recommendation;
}
// The two-arm (actual vs do-nothing) performance scorecard
// (`services/scorecard.compute_scorecard`) — every honesty label
// (convention, n_effective, depth_weeks, p_floor_two_sided, arms_coincide)
// is a first-class field the UI must print, never elide.
export interface ScorecardPerformanceMeta {
  n_actual: number;
  n_donothing: number;
  base_composition_assumption: string;
}
export interface ScorecardPerformance {
  convention: string;
  empty: boolean;
  depth_weeks: number;
  n_effective: number;
  p_floor_two_sided: number | null;
  actual_twr: number | null;
  do_nothing_twr: number | null;
  arms_coincide: boolean | null;
  note: string | null;
  meta?: ScorecardPerformanceMeta;
}
export interface ReportScorecard {
  n_evaluated: number;
  n_proposed: number;
  n_refused: number;
  rule_status: Record<string, string>;
  performance: ScorecardPerformance | null;
}
export interface ReportNarration {
  text: string;
  grounding: string;
  model: string;
}
export interface AdvisorReport {
  report_week: string;
  what_happened: {
    nav_eur: number;
    cash_eur: number;
    positions: ReportPosition[];
    regime_state: string | null;
  };
  where_we_stand: {
    positions: ReportPosition[];
    rule_evaluations: RuleEvaluation[];
  };
  what_to_do_next: {
    proposed: Recommendation[];
    refused: Recommendation[];
    rule_status: Record<string, string>;
  };
  scorecard: ReportScorecard;
  narration: ReportNarration | null;
  narrated: boolean;
  narration_absent_reason: string | null;
}

// ── Weekly Report Pack ───────────────────────────────────
// Mirrors backend/app/services/report_pack.py (chapters 2/3/4/5/7),
// backend/app/services/shadow.py (chapter 6) and backend/app/report_worker.py
// (chapter 1, exec_summary) verbatim — real field names only, never invented
// ones. Every present chapter carries `engine` + `how_to_read`; an absent
// one is `{absent:true, reason}`. Demo-mode packs (`routers/reports.py`'s
// `_demo_pack*` helpers) share these same chapter shapes but some fields
// never populate under DEMO_MODE (documented per type below) — the real
// binding constraint is that MarketResearchEngine/ResearchEngine/
// IntelService/RiskEngine have no demo seam, so demo always ships
// exec_summary/market/holdings/quant as absent.
export interface PackAbsent {
  absent: true;
  reason: string;
}
export function isPackAbsent(c: unknown): c is PackAbsent {
  return !!c && typeof c === "object" && (c as PackAbsent).absent === true;
}
interface PackChapterCommon {
  engine: string;
  how_to_read: string;
}

export interface PackPosition {
  symbol: string;
  qty: number;
  value_eur: number;
  weight_pct: number;
  wow_value_delta_eur: number | null;
  wow_weight_delta_pct: number | null;
}
// `var_cvar`/`coverage` come from the live `RiskEngine` bundle in real mode
// (untyped here — the risk bundle's own shape is Phase-8.5's, not this
// chapter's contract) and are `null`/an explicit `{absent:true, reason}` in
// demo mode (RiskEngine has no demo seam).
export interface PackPortfolioChapter extends PackChapterCommon {
  report_week: string;
  prior_report_week: string | null;
  nav_eur: number;
  cash_eur: number;
  positions: PackPosition[];
  var_cvar: Record<string, unknown> | PackAbsent | null;
  coverage: Record<string, unknown> | null;
  scorecard: ScorecardPerformance;
}

// `narration`/`narration_absent_reason`: `report_worker.py` fixes narration being
// write-only (generated, egressed, persisted to `ai_runs` — no read surface
// ever showed it). Mutually exclusive per report (only one is ever set), and
// BOTH optional — a pack built before this field landed, or a demo pack
// (`_demo_pack` never attempts narration), has neither.
export interface PackExecSummaryChapter extends PackChapterCommon {
  nav_eur: number | null;
  wow_pct: number | null;
  regime: MarketRegime | null;
  chapters_present: Record<string, boolean>;
  n_chapters_present: number;
  shadow_counts: Record<string, { n_events: number | null; graded: boolean | null }>;
  headline: string;
  highlights: string[];
  narration?: string;
  narration_absent_reason?: string;
}

export interface PackMarketChapter extends PackChapterCommon {
  regime: MarketRegime | null;
  cross_asset: CrossAssetReport | null;
  rotation: RotationReport | null;
  rrg: RRGReport | null;
  volatility: VolatilityReport | null;
  breadth: BreadthReport | null;
  sentiment: SentimentReport | null;
  valuation: ValuationReport | null;
  econ_calendar_week_ahead: Envelope<CalendarData> | null;
}

export interface PackHoldingForensicDelta {
  delta: number | null;
  vs_report_week: string | null;
  vs_fiscal_year: string | null;
  label: string;
}
export interface PackHoldingSymbolEntry {
  fundamentals: ResearchFundamentals;
  valuation: ResearchValuation;
  forensic: ResearchScores & { deltas: Record<string, PackHoldingForensicDelta> };
  technicals: ResearchTechnicals;
  scan: ScanReport;
  news: IntelNewsItem[];
}
export interface PackHoldingsChapter extends PackChapterCommon {
  // Per-symbol tear sheet; a symbol whose sheet failed to build is
  // `{absent:true, reason}` in its OWN slot only (report_pack.py) — every
  // other held symbol is unaffected.
  symbols: Record<string, PackHoldingSymbolEntry | PackAbsent>;
  symbols_failed: Record<string, string>;
  n_symbols_failed: number;
}

export interface PackQuantCrossSectionRow {
  symbol: string;
  value?: number | null;
  [key: string]: unknown;
}
// The embedded factor payload (`report_worker.py::_quant_work`) is
// `run_factor_study()`'s own `FactorReport.model_dump()` (the SAME Pydantic
// model as the Phase-11 Lab's `FactorReport` above — verified against
// `backend/app/adapters/lab/base.py`), plus additive fields two other call
// sites stamp onto that same dict: `symbol_resolution_failures`/
// `n_symbols_resolution_failed` (`_quant_work`, when some held symbols
// couldn't be resolved to a price series) and `stale`/
// `stale_source_report_week`/`stale_reason` (`_stale_factor_fallback`, when
// this week's quant work timed out and the payload shipped from the last
// completed pack instead).
export interface PackFactorPayload extends FactorReport {
  symbol_resolution_failures?: Record<string, string>;
  n_symbols_resolution_failed?: number;
  stale?: boolean;
  stale_source_report_week?: string;
  stale_reason?: string;
}
export interface PackQuantChapter extends PackChapterCommon {
  factor: PackFactorPayload | PackAbsent;
  scoreboard: Scoreboard;
  cross_section: Record<string, { as_of_date: string; n_universe: number; rows: PackQuantCrossSectionRow[] }>;
}

// ── shadow chapter — statistical honesty fields:
// `nature` (evidence-only label), per-tool `benchmark_hit_rate` (never a raw
// hit-rate alone), `hit_rate_wilson_lb` (small-sample uncertainty), and the
// scoreboard-level `deflation_note` (k tools compared side by side, no
// multiple-testing adjustment) are PROMINENT UI requirements, not footnotes.
export interface ShadowToolVerdict {
  verdict: string;
  direction: number | null;
  rationale: string;
}
// Demo-only: per-symbol current verdict (real chapter_shadow has no
// per-symbol verdict listing, only the aggregate graded scoreboard below).
export interface ShadowSymbolVerdicts {
  technicals?: ShadowToolVerdict;
  patterns?: ShadowToolVerdict;
  forensic?: ShadowToolVerdict;
}
// Audited against BOTH producers (fix round, controller): `graded` is the
// only field every shape guarantees.
//   - real, graded=true (`shadow.py::_score_tool` + `graded=True`): every
//     field below IS present.
//   - real, graded=false (forensic only): `n_events` + `reason` present,
//     nothing else.
//   - demo (`routers/reports.py::_demo_pack_shadow`, every tool always
//     ungraded): ONLY `graded`/`reason` — `n_events` is omitted entirely,
//     not even a zero. A required `n_events: number` here previously
//     rendered `undefined` silently for every demo-mode row.
export interface ShadowToolGrade {
  graded: boolean;
  reason?: string; // present when graded=false (forensic; or any tool in demo mode)
  n_events?: number; // absent under DEMO_MODE — never assume present
  n_graded?: number;
  n_ungraded_null_direction?: number;
  n_pending?: number;
  n_uncovered?: number;
  exited_count?: number;
  hit_rate?: number | null;
  benchmark_hit_rate?: number | null;
  hit_rate_wilson_lb?: number | null;
  cross_sectional?: boolean;
  low_confidence?: boolean;
  overlapping_windows?: boolean;
  warnings?: string[];
  // Fix round 5 (additive, shadow owner — landed mid-pass; re-read
  // `shadow.py::_score_tool` to confirm exact shapes): both present
  // whenever `graded:true` (forensic's `graded:false` early return has
  // neither — hence still optional here), a pack built before this landed
  // has neither either. `pushes` — a zero-metric graded observation
  // (median name itself, cross-sectional; or an exactly-flat absolute
  // return), excluded from both hits and misses, always a real (possibly
  // 0) count once present, never null. `effective_n` — cross-sectional
  // tools' `n_graded // 2` (a tercile pick pairs one add_watch against one
  // trim_watch from the SAME ranking, so `n_graded` double-counts one
  // independent comparison; Wilson LB for those tools is computed on this,
  // not `n_graded`) — `null` for absolute-direction tools (technicals/
  // patterns), which keep `n_graded` for Wilson instead.
  pushes?: number;
  effective_n?: number | null;
}
export interface PackShadowChapter extends PackChapterCommon {
  nature: string;
  grade_min_weeks?: number;
  k?: number;
  deflation_note?: string | null;
  // Fix round 5 (additive, shadow owner): a single as-of timestamp for the
  // whole scoreboard's forward pricing — table-level, not per-tool (`k`/
  // `deflation_note`/`grade_min_weeks`'s own precedent: any new top-level
  // field `scoreboard()` returns lands here via `chapter_shadow`'s `**board`
  // spread). Optional — absent on any pack built before this landed.
  priced_as_of?: string;
  verdicts?: Record<string, ShadowSymbolVerdicts>;
  tools: Record<string, ShadowToolGrade>;
}

export interface MethodologyTool {
  tool: string;
  measures?: string;
  endpoint?: string;
  limitations?: string;
  grading_horizon?: string;
  status?: string; // demo-mode variant: why this tool wasn't exercised
}
export interface PackMethodologyChapter extends PackChapterCommon {
  version: string;
  tools: MethodologyTool[];
}

export interface PackSource {
  engine: string;
  fetched_at?: string;
  stale: boolean;
  error: string | null;
  partial?: Record<string, unknown>;
}

// ── forecast ledger (Phase 18) — real field names read verbatim
// from `backend/app/services/forecasts.py` (`_forecast_dict`,
// `build_shadow_forecasts`), `backend/app/routers/forecasts.py`
// (`_forecast_dict`/`_outcome_dict`, the demo fixtures) and
// `backend/app/report_worker.py::_issue_forecasts` (the pack chapter). One
// `Forecast` shape serves both the pack chapter's issuance summary context
// and the standalone `/forecasts` router; `outcome` is only ever populated
// by `GET /forecasts/{id}` (a joined single-row read) — the list route
// never embeds it (real mode), though the demo fixture's resolved/abstained
// rows do carry one for a representative sample.
export interface Outcome {
  id: number;
  forecast_id: number;
  resolution_status: string; // "matured" | "uncovered" | "manual"
  hit: boolean | null;
  brier: number | null;
  log_score: number | null;
}
export interface ForecastTarget {
  metric?: string;
  comparator?: string;
  threshold?: number;
  kind?: string;
  statement?: string;
}
export interface Forecast {
  id: number;
  issued_by: string; // "shadow:<tool>" | "human"
  issuer_version: string;
  subject_kind: string;
  subject: string;
  statement: string;
  kind: string;
  probability: number;
  direction: number | null;
  target: ForecastTarget;
  horizon_days: number;
  decision_date: string;
  due_date: string;
  report_week: string | null;
  published_at: string;
  first_seen_at: string;
  available_at: string;
  resolution_source: string; // "daily_bars:<source>" | "manual"
  resolution_rule: Record<string, unknown>;
  evidence_refs: { kind: string; [key: string]: unknown }[];
  status: string; // "pending" | "resolved" | "abstained"
  outcome?: Outcome | null;
}
export interface ForecastsListResponse {
  forecasts: Forecast[];
  count: number;
}
export interface ForecastListParams {
  status?: string;
  week?: string;
  issued_by?: string;
  limit?: number;
}

// ── pack chapter: forecasts (worker's `_issue_forecasts`, sections key
// "forecasts") — a small issuance summary, not the ledger itself (that's
// the `/forecasts` router above). On an issuance failure the worker ships
// `{engine, absent:true, reason}` instead (never sinks the pack) — hence
// the `PackAbsent` union, same pattern as every other chapter here.
export interface ForecastIssuanceError {
  issued_by: string | null;
  subject: string | null;
  reason: string;
}
export interface PackForecastsChapter extends PackChapterCommon {
  issued: number;
  duplicates: number;
  skipped: { covered: number; ungradeable: number };
  issuance_errors: ForecastIssuanceError[];
  pending_total: number;
  next_due: string | null;
}

// ── pack chapter: calibration (`shadow.py::_reliability_bins`/
// `_calibration_summary`/`calibration`/`chapter_calibration`, sections key
// "calibration"). `bins` is `null` for a near-constant-probability issuer
// (B2 — machine issuers) with `bins_note` explaining why; a human issuer
// with varying probability gets real bins. A chapter-build failure logs and
// SKIPS the key entirely (by design) — there is no `PackAbsent` variant
// here, only "key present" vs "key absent from `sections`".
export interface CalibrationBin {
  bin: string;
  lo: number;
  hi: number;
  n: number;
  mean_p: number | null;
  hit_rate: number | null;
  wilson_lb: number | null;
}
export interface CalibrationSummary {
  n: number;
  effective_n: number;
  brier_mean: number | null;
  brier_ref: number | null;
  brier_coin: number;
  benchmark_brier: number | null;
  brier_skill: number | null;
  log_score_mean: number | null;
  hit_rate: number | null;
  benchmark_hit_rate: number | null;
  wilson_lb: number | null;
  pushes: number;
  abstained: number;
  abstention_rate: number | null;
}
export interface CalibrationGroup {
  issued_by: string;
  horizon_days: number;
  summary: CalibrationSummary;
  bins: CalibrationBin[] | null;
  bins_note: string | null;
  pending: number;
  priced_as_of: string;
  low_confidence: boolean;
  deflation_note: string | null;
}
export interface PackCalibrationChapter extends PackChapterCommon {
  groups: CalibrationGroup[];
}

// ── pack chapter + read route: decisions (`services/decisions.py::
// DecisionProjection`, sections key "decisions", also `GET
// /reports/{week}/decisions` directly). `funding_source`/`risk_budget_impact`
// are tagged unions over the SAME real branches the service docstring
// documents — `"kind"`/`"unavailable"` are read verbatim off the backend
// dict, never invented. Same "key absent from `sections` on failure, no
// `PackAbsent`" rule as calibration above.
export type DecisionFundingSource =
  | { kind: "none"; reason: string }
  | { kind: "clearing_cash"; account: string; amount_eur: number | null; available_eur: number; covers: boolean }
  | { kind: "proceeds"; symbols: string[]; est_proceeds_eur: number | null; to: string; lots: string }
  | { kind: "unknown"; reason: string };
export type DecisionRiskBudgetImpact =
  | { cvar95_now: number; cvar95_post: number; delta_pct: number }
  | { unavailable: string };
export interface DecisionInvalidation {
  text: string;
  still_valid: boolean | null;
  current_weight?: number;
  current?: number;
}
export interface DecisionLifecycleEntry {
  id: number;
  status: string;
  human_action: string | null;
  created_at: string;
  human_note: string | null;
}
export interface DecisionRow {
  id: number;
  rule_id: string;
  action: string;
  status: string;
  sizing: number | null;
  as_of: string;
  report_week: string;
  funding_source: DecisionFundingSource;
  risk_budget_impact: DecisionRiskBudgetImpact;
  invalidation: DecisionInvalidation;
  review_at: string;
  expires_at: string;
  forecast: null;
  outcome: null;
  lifecycle: DecisionLifecycleEntry[];
}
export interface PackDecisionsChapter extends PackChapterCommon {
  rows: DecisionRow[];
  counts: Record<string, number>;
  no_action_rate: number;
}

export interface ReportPackSections {
  exec_summary: PackExecSummaryChapter | PackAbsent;
  portfolio: PackPortfolioChapter | PackAbsent;
  market: PackMarketChapter | PackAbsent;
  holdings: PackHoldingsChapter | PackAbsent;
  quant: PackQuantChapter | PackAbsent;
  shadow: PackShadowChapter | PackAbsent;
  methodology: PackMethodologyChapter | PackAbsent;
  // Additive, Phase 18 — optional: a pack built before this landed, or
  // one whose chapter build failed (calibration/decisions) or whose whole
  // issuance step failed (forecasts, `PackAbsent`-shaped instead), may omit
  // the key entirely. Demo-mode packs (`_demo_pack`) never populate these —
  // no demo seam for the forecast ledger/decision projection (same rule as
  // exec_summary/market/holdings/quant in demo mode).
  forecasts?: PackForecastsChapter | PackAbsent;
  calibration?: PackCalibrationChapter;
  decisions?: PackDecisionsChapter;
}
export interface ReportPack {
  report_week: string;
  status: string;
  sections: ReportPackSections;
  sources: Record<string, PackSource>;
  narrated: boolean;
  pdf: boolean;
}

// Additive to `/reports/weeks` — existing `weeks` key stays untouched.
export interface ReportWeekAvailability {
  pack: boolean;
  pdf: boolean;
}
export interface ReportWeeksResponse {
  weeks: string[];
  availability?: Record<string, ReportWeekAvailability>;
}
