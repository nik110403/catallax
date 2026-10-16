// Typed client for the FastAPI backend. Same-origin: nginx routes /api to the
// backend, so all paths are relative. Widgets call only these helpers — never
// Fava/Ghostfolio/external APIs directly (widget-system rule).
import type {
  AccountNode,
  AdvisorReport,
  AlgoAccount,
  AlgoFill,
  AlgoOrder,
  AlgoOrderSpec,
  AlgoPosition,
  AlgoReconciliation,
  AlgoStatus,
  AlgoStrategy,
  AlgoStrategyCreate,
  Allocation,
  BacktestConfigInput,
  BacktestJob,
  BacktestList,
  BacktestReport,
  BreadthReport,
  CalendarData,
  CockpitJob,
  CockpitMessage,
  CockpitSession,
  Candle,
  CombinedNetWorth,
  ConditionsReport,
  CrossAssetReport,
  CurrencyTotal,
  Envelope,
  FactorConfigInput,
  FactorReport,
  Forecast,
  ForecastListParams,
  ForecastsListResponse,
  FxDragReport,
  GeoFeatureCollection,
  Holding,
  IndicatorBoard,
  IntelNewsItem,
  IntelRun,
  IntelStatus,
  LabEngines,
  LabJob,
  LabList,
  MacroSeries,
  ModelConfigInput,
  ModelReport,
  MarketRegime,
  MarketsOverview,
  NetWorthScope,
  NewsItem,
  PackDecisionsChapter,
  PortfolioSummary,
  Quote,
  RatesData,
  RecommendationTransition,
  ReportPack,
  ReportWeeksResponse,
  ResearchFundamentals,
  ResearchScores,
  ResearchScreen,
  ResearchTechnicals,
  ResearchValuation,
  RiskXray,
  RRGReport,
  RotationReport,
  ScanReport,
  Scoreboard,
  ScreenFilters,
  SentimentReport,
  Session,
  SignalCatalog,
  SignalEmitResult,
  SignalGrade,
  StressReport,
  SymbolMatch,
  ValuationReport,
  VarReport,
  VolatilityReport,
  WatchlistItem,
  WatchlistItemInput,
  WealthfolioHoldings,
  YieldCurve,
} from "./types";

const API = "/api/v1";

export class ApiError extends Error {
  code: string;
  status: number;
  constructor(code: string, message: string, status: number) {
    super(message);
    this.code = code;
    this.status = status;
  }
}

// Hosted read-only demo: the backend answers every non-GET /api/v1 request with
// 403 {"error":"read_only_demo"}, and the edge proxy may answer writes with a
// bare 405. Both surface as one quiet "read-only demo" ApiError so every caller
// that shows `e.message` gets the same low-emphasis line, with no per-site code.
// Reacts to the response only — no server-fetched demo flag.
export const READ_ONLY_DEMO_CODE = "read_only_demo";
export const READ_ONLY_DEMO_MESSAGE = "read-only demo";

export function isReadOnlyDemoError(e: unknown): boolean {
  return e instanceof ApiError && e.code === READ_ONLY_DEMO_CODE;
}

// Call at the top of a failed (!res.ok) branch. Reads a clone, so the caller's
// own body parsing is unaffected. Returns null for every other failure.
export async function readOnlyDemoError(res: Response, method: string): Promise<ApiError | null> {
  if (method.toUpperCase() === "GET") return null;
  let hit = res.status === 405;
  if (!hit && res.status === 403) {
    try {
      hit = (await res.clone().json())?.error === READ_ONLY_DEMO_CODE;
    } catch {
      /* non-JSON 403 → ordinary forbidden */
    }
  }
  return hit ? new ApiError(READ_ONLY_DEMO_CODE, READ_ONLY_DEMO_MESSAGE, res.status) : null;
}

// Fires when any request below gets a 401 — lets the app flip to the locked
// state mid-session (the share-gate cookie expired) without every caller
// checking status codes itself (Phase 14a). Not used by login()/logout()
// below: a wrong-passphrase 401 from the login route is an expected, inline-
// handled outcome there, not a "you got logged out" event.
let unauthorizedHandler: (() => void) | null = null;
export function onUnauthorized(cb: () => void): void {
  unauthorizedHandler = cb;
}
export function notifyUnauthorized(status: number): void {
  if (status === 401) unauthorizedHandler?.();
}

async function get<T>(path: string, params?: Record<string, string | number | undefined>): Promise<T> {
  // Defensive: every caller passes a literal path; reject anything that could
  // traverse outside /api/v1 if a user-controlled value is ever passed.
  if (!path.startsWith("/") || path.includes("..")) {
    throw new ApiError("BAD_PATH", `Invalid API path: ${path}`, 0);
  }
  const qs = params
    ? "?" +
      new URLSearchParams(
        Object.entries(params)
          .filter(([, v]) => v !== undefined && v !== "")
          .map(([k, v]) => [k, String(v)]),
      ).toString()
    : "";
  const res = await fetch(API + path + qs);
  if (!res.ok) {
    let code = "ERROR";
    let message = res.statusText;
    try {
      const body = await res.json();
      if (body?.error) {
        code = body.error.code ?? code;
        message = body.error.message ?? message;
      }
    } catch {
      /* non-JSON error body */
    }
    notifyUnauthorized(res.status);
    throw new ApiError(code, message, res.status);
  }
  return res.json() as Promise<T>;
}

async function post<T>(path: string): Promise<T> {
  if (!path.startsWith("/") || path.includes("..")) {
    throw new ApiError("BAD_PATH", `Invalid API path: ${path}`, 0);
  }
  const res = await fetch(API + path, { method: "POST" });
  if (!res.ok) {
    const ro = await readOnlyDemoError(res, "POST");
    if (ro) throw ro;
    let code = "ERROR";
    let message = res.statusText;
    try {
      const body = await res.json();
      if (body?.error) {
        code = body.error.code ?? code;
        message = body.error.message ?? message;
      }
    } catch {
      /* non-JSON error body */
    }
    notifyUnauthorized(res.status);
    throw new ApiError(code, message, res.status);
  }
  return res.json() as Promise<T>;
}

async function del<T>(path: string): Promise<T> {
  if (!path.startsWith("/") || path.includes("..")) {
    throw new ApiError("BAD_PATH", `Invalid API path: ${path}`, 0);
  }
  const res = await fetch(API + path, { method: "DELETE" });
  if (!res.ok) {
    const ro = await readOnlyDemoError(res, "DELETE");
    if (ro) throw ro;
    let code = "ERROR";
    let message = res.statusText;
    try {
      const body = await res.json();
      if (body?.error) {
        code = body.error.code ?? code;
        message = body.error.message ?? message;
      }
    } catch {
      /* non-JSON error body */
    }
    notifyUnauthorized(res.status);
    throw new ApiError(code, message, res.status);
  }
  return res.json() as Promise<T>;
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  if (!path.startsWith("/") || path.includes("..")) {
    throw new ApiError("BAD_PATH", `Invalid API path: ${path}`, 0);
  }
  const res = await fetch(API + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const ro = await readOnlyDemoError(res, "POST");
    if (ro) throw ro;
    let code = "ERROR";
    let message = res.statusText;
    try {
      const b = await res.json();
      if (b?.error) {
        code = b.error.code ?? code;
        message = b.error.message ?? message;
        // Pre-flight validation surfaces field-keyed details — fold them into the
        // message so the launcher shows exactly what to fix (Vibe-Trading pattern).
        if (Array.isArray(b.error.details) && b.error.details.length) {
          // Pre-flight details use `.message`; Pydantic request-validation uses `.msg` —
          // read either so the specific fix is shown, not the generic fallback.
          message = b.error.details
            .map((d: { message?: string; msg?: string }) => d.message ?? d.msg)
            .filter(Boolean)
            .join(" · ");
        }
      }
    } catch {
      /* non-JSON error body */
    }
    notifyUnauthorized(res.status);
    throw new ApiError(code, message, res.status);
  }
  return res.json() as Promise<T>;
}

// Login/logout return 204 (no body) on success — unlike every other POST here,
// which always `.json()`s a response body — so they get their own thin
// wrapper instead of forcing post()/postJson() to special-case an empty
// response. Deliberately does NOT call notifyUnauthorized: a 401 here is the
// login route's own "wrong passphrase" outcome (handled inline by the caller),
// not a mid-session cookie expiry.
async function putJson<T>(path: string, body: unknown): Promise<T> {
  if (!path.startsWith("/") || path.includes("..")) {
    throw new ApiError("BAD_PATH", `Invalid API path: ${path}`, 0);
  }
  const res = await fetch(API + path, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const ro = await readOnlyDemoError(res, "PUT");
    if (ro) throw ro;
    let code = "ERROR";
    let message = res.statusText;
    try {
      const b = await res.json();
      if (b?.error) {
        code = b.error.code ?? code;
        message = b.error.message ?? message;
      }
    } catch {
      /* non-JSON error body */
    }
    notifyUnauthorized(res.status);
    throw new ApiError(code, message, res.status);
  }
  return res.json() as Promise<T>;
}

async function postNoBody(path: string, body?: unknown): Promise<void> {
  if (!path.startsWith("/") || path.includes("..")) {
    throw new ApiError("BAD_PATH", `Invalid API path: ${path}`, 0);
  }
  const res = await fetch(API + path, {
    method: "POST",
    headers: body ? { "Content-Type": "application/json" } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!res.ok) {
    const ro = await readOnlyDemoError(res, "POST");
    if (ro) throw ro;
    let code = "ERROR";
    let message = res.statusText;
    try {
      const b = await res.json();
      if (b?.error) {
        code = b.error.code ?? code;
        message = b.error.message ?? message;
      }
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(code, message, res.status);
  }
}

// SSE stream URL for live backtest progress (consumed by EventSource, not fetch).
export const backtestStreamUrl = (jobId: string) =>
  `${API}/backtests/${encodeURIComponent(jobId)}/stream`;

// SSE stream URL for live Quant/ML Lab run progress — same shape.
export const labStreamUrl = (jobId: string) =>
  `${API}/lab/runs/${encodeURIComponent(jobId)}/stream`;

// SSE stream URL for live AI intel-enrichment run progress — same shape.
export const intelStreamUrl = (jobId: string) =>
  `${API}/intel/runs/${encodeURIComponent(jobId)}/stream`;

// SSE stream URL for live paper-trading algo hub events — a single
// pub/sub feed (order fills, kill-switch toggles), not per-job like the others.
export const algoStreamUrl = () => `${API}/algo/stream`;

// PDF download URL for a report pack — a plain same-origin
// anchor href, not a fetch+blob dance (same convention as the stream URLs
// above: the browser handles the actual transfer). Caller must only render
// this when the pack's own `pdf:true` (or `/weeks`' `availability[week].pdf`)
// says a PDF exists — the route itself 404s otherwise (and always 404s under
// DEMO_MODE, never even reaching a real `report_packs` row).
export const reportPdfUrl = (reportWeek: string) => `${API}/reports/${encodeURIComponent(reportWeek)}/pdf`;

// Session + share-gate auth (Phase 14a). Kept outside the `api` object since
// they're bootstrap/auth concerns, not domain data — same treatment as the
// stream-URL helpers above. The server sets/clears the session cookie itself
// (HttpOnly); nothing here touches storage directly.
// Deliberately does NOT swallow errors into a default session. An earlier
// revision resolved failures to `{auth_required:false, authenticated:true}` so
// the app could boot before the route existed — that fails OPEN: any backend
// blip (cold start, 5xx, a middleware exemption drifting so /session itself
// 401s) would mount the whole shell, and its data fetches, in place of the
// login screen. The caller must treat a rejection as "not verified" and render
// a retry state instead of the app (see App.tsx). Failing closed here cannot
// brick the no-passphrase setup, because the caller's error state is a retry
// card, not a login form nobody could satisfy.
export const getSession = (): Promise<Session> => get<Session>("/session");
export const login = (passphrase: string, remember: boolean): Promise<void> =>
  postNoBody("/auth/login", { passphrase, remember });
export const logout = (): Promise<void> => postNoBody("/auth/logout");

export const api = {
  quote: (symbol: string, source?: string) =>
    get<Quote>(`/prices/${encodeURIComponent(symbol)}`, { source }),
  history: (
    symbol: string,
    opts: { interval?: string; source?: string; start?: string; end?: string } = {},
  ) => get<Candle[]>(`/prices/${encodeURIComponent(symbol)}/history`, opts),
  search: (q: string, source?: string) => get<SymbolMatch[]>(`/search`, { q, source }),
  accounts: () => get<AccountNode[]>(`/ledger/accounts`),
  // Net worth = imported portfolio + the ledger's bank/wallet cash (combined).
  // scope: "all" (default) or "investments" (market-priced holdings only).
  networth: (months?: number, scope?: NetWorthScope) =>
    get<CombinedNetWorth>(`/networth`, { months, scope }),
  // Holdings come from the imported portfolio (per-asset + per-account).
  wealthfolioHoldings: () => get<WealthfolioHoldings>(`/wealthfolio/holdings`),
  currencies: (currency?: string) => get<CurrencyTotal[]>(`/ledger/currencies`, { currency }),
  news: () => get<NewsItem[]>(`/news`),
  // Markets: cross-asset overview + geo-map data (USGS quakes proxy).
  marketsOverview: () => get<MarketsOverview>(`/markets/overview`),
  marketsQuakes: () => get<GeoFeatureCollection>(`/markets/quakes`),
  // Macro & Economic: all via MacroAdapter → openbb-data. Envelope-wrapped
  // (data + provenance). Free sources; FRED series/calendar need OPENBB_FRED_API_KEY.
  macroSeries: (seriesId: string, transform?: string) =>
    get<Envelope<MacroSeries>>(`/macro/series/${encodeURIComponent(seriesId)}`, { transform }),
  macroIndicators: () => get<Envelope<IndicatorBoard>>(`/macro/indicators`),
  macroCalendar: () => get<Envelope<CalendarData>>(`/macro/calendar`),
  macroYieldCurve: (region: string) => get<Envelope<YieldCurve>>(`/macro/yield-curve`, { region }),
  macroRates: () => get<Envelope<RatesData>>(`/macro/rates`),
  // Portfolio Risk lens (Phase 8.5): computed over the imported portfolio via F0 history.
  riskXray: () => get<Envelope<RiskXray>>(`/risk/xray`),
  riskVar: () => get<Envelope<VarReport>>(`/risk/var`),
  riskFxDrag: () => get<Envelope<FxDragReport>>(`/risk/fx-drag`),
  riskStress: () => get<Envelope<StressReport>>(`/risk/stress`),
  // Signal self-scoring core: logged signals graded vs realised forward
  // returns COMPUTED from F0 history (Rank-IC / ICIR / quintile L-S spread).
  signalCatalog: () => get<Envelope<SignalCatalog>>(`/signals`),
  signalScoreboard: (horizon?: number, quantiles?: number) =>
    get<Envelope<Scoreboard>>(`/signals/scoreboard`, { horizon, quantiles }),
  signalGrade: (signalId: string, horizon?: number, quantiles?: number) =>
    get<Envelope<SignalGrade>>(`/signals/${encodeURIComponent(signalId)}/grade`, { horizon, quantiles }),
  // Runs the reference factor generators (xs_mom_252_21 + xs_lowvol_63) over the portfolio.
  signalSeed: () => post<Envelope<SignalEmitResult>>(`/signals/seed`),
  // Backtester: configure a signal-driven strategy, run it OFF the main
  // API (worker), poll the job, fetch the equity curve + metric bundle.
  createBacktest: (config: BacktestConfigInput) => postJson<BacktestJob>(`/backtests`, config),
  listBacktests: (limit?: number) => get<BacktestList>(`/backtests`, { limit }),
  getBacktest: (jobId: string) => get<BacktestJob>(`/backtests/${encodeURIComponent(jobId)}`),
  backtestReport: (jobId: string) =>
    get<Envelope<BacktestReport>>(`/backtests/${encodeURIComponent(jobId)}/report`),
  // Quant / ML Lab: factor-IC studies + ML model train/eval run OFF the
  // main API in the heavy `qlib-lab` worker (mirror the backtester job flow); the
  // technical scanner is synchronous over the F0 backbone.
  labEngines: () => get<LabEngines>(`/lab/engines`),
  createFactorStudy: (config: FactorConfigInput) => postJson<LabJob>(`/lab/factors`, config),
  createModelRun: (config: ModelConfigInput) => postJson<LabJob>(`/lab/models`, config),
  listLabRuns: (limit?: number) => get<LabList>(`/lab/runs`, { limit }),
  getLabRun: (jobId: string) => get<LabJob>(`/lab/runs/${encodeURIComponent(jobId)}`),
  labFactorReport: (jobId: string) =>
    get<Envelope<FactorReport>>(`/lab/runs/${encodeURIComponent(jobId)}/report`),
  labModelReport: (jobId: string) =>
    get<Envelope<ModelReport>>(`/lab/runs/${encodeURIComponent(jobId)}/report`),
  labScan: (patterns?: string[], universe?: string[], emit?: boolean) =>
    get<Envelope<ScanReport>>(`/lab/scan`, {
      patterns: patterns?.join(",") || undefined,
      universe: universe?.join(",") || undefined,
      emit: emit ? "true" : undefined,
    }),
  // Market Research (Phase 10a): regime, cross-asset, rotation, RRG, vol, conditions, valuation.
  marketRegime: () => get<Envelope<MarketRegime>>(`/market/regime`),
  marketCrossAsset: () => get<Envelope<CrossAssetReport>>(`/market/cross-asset`),
  marketRotation: () => get<Envelope<RotationReport>>(`/market/rotation`),
  marketRrg: () => get<Envelope<RRGReport>>(`/market/rrg`),
  marketVolatility: () => get<Envelope<VolatilityReport>>(`/market/volatility`),
  marketConditions: () => get<Envelope<ConditionsReport>>(`/market/conditions`),
  marketValuation: () => get<Envelope<ValuationReport>>(`/market/valuation`),
  marketSentiment: () => get<Envelope<SentimentReport>>(`/market/sentiment`),
  marketBreadth: () => get<Envelope<BreadthReport>>(`/market/breadth`),
  // Equity Research (Phase 10b): single-name fundamentals + forensic scores
  // (Piotroski/Altman/Beneish) + technicals + a universe screen. All via the
  // FundamentalsAdapter (yfinance) behind the FastAPI boundary; Envelope-wrapped.
  researchFundamentals: (symbol: string) =>
    get<Envelope<ResearchFundamentals>>(`/research/${encodeURIComponent(symbol)}/fundamentals`),
  researchValuation: (symbol: string) =>
    get<Envelope<ResearchValuation>>(`/research/${encodeURIComponent(symbol)}/valuation`),
  researchScores: (symbol: string) =>
    get<Envelope<ResearchScores>>(`/research/${encodeURIComponent(symbol)}/scores`),
  researchTechnicals: (symbol: string) =>
    get<Envelope<ResearchTechnicals>>(`/research/${encodeURIComponent(symbol)}/technicals`),
  researchScreen: (filters: ScreenFilters = {}) =>
    get<Envelope<ResearchScreen>>(`/research/screen`, filters as Record<string, number | undefined>),
  // Ledger-based portfolio engine — retained; the dashboard widgets now
  // source holdings/allocation from the portfolio exporter above.
  holdings: (enrich = true) =>
    get<Holding[]>(`/portfolio/holdings`, { enrich: enrich ? "true" : "false" }),
  allocation: () => get<Allocation>(`/portfolio/allocation`),
  portfolioSummary: () => get<PortfolioSummary>(`/portfolio/summary`),
  // AI Intel: enriched news + off-API enrichment runs + provider health.
  // The API only enqueues/reads — the ai-runner worker does the actual enrichment.
  intelNews: (limit = 60) => get<{ items: IntelNewsItem[] }>(`/intel/news`, { limit }),
  intelEvents: (limit = 200) => get<{ events: IntelNewsItem[] }>(`/intel/events?limit=${limit}`),
  intelEnrich: () => post<IntelRun>("/intel/enrich"),
  intelRun: (jobId: string) => get<IntelRun>(`/intel/runs/${encodeURIComponent(jobId)}`),
  intelStatus: () => get<IntelStatus>("/intel/status"),
  // Analyst Cockpit: persisted, read-only assistant sessions. The
  // worker job has no SSE endpoint; Cockpit.tsx polls `cockpitJob()`.
  cockpitSessions: () => get<{ sessions: CockpitSession[]; count: number }>("/cockpit/sessions"),
  cockpitSession: (key: string) =>
    get<{ session: CockpitSession; messages: CockpitMessage[] }>(`/cockpit/sessions/${encodeURIComponent(key)}`),
  createCockpitSession: (title?: string) => postJson<CockpitSession>("/cockpit/sessions", { title }),
  askCockpit: (key: string, text: string) =>
    postJson<{ job_id: string; message_id: number }>(`/cockpit/sessions/${encodeURIComponent(key)}/ask`, { text }),
  cockpitJob: (jobId: string) => get<CockpitJob>(`/cockpit/jobs/${encodeURIComponent(jobId)}`),
  // Paper-Trading Algo Hub: orders/fills/positions/account against the
  // paper OMS (`AlgoService`), plus signal-driven strategy CRUD and the kill switch.
  // The API only validates + enqueues orders — `algo_worker` (trading-engine)
  // resolves fills off-API.
  algoSubmitOrder: (spec: AlgoOrderSpec) => postJson<AlgoOrder>("/algo/orders", spec),
  algoCancelOrder: (orderId: number) => del<AlgoOrder>(`/algo/orders/${encodeURIComponent(orderId)}`),
  algoOrders: (status?: string) => get<{ orders: AlgoOrder[] }>(`/algo/orders`, { status }),
  algoFills: () => get<{ fills: AlgoFill[] }>(`/algo/fills`),
  algoPositions: () => get<{ positions: AlgoPosition[] }>(`/algo/positions`),
  algoAccount: () => get<AlgoAccount>(`/algo/account`),
  algoStrategies: () => get<{ strategies: AlgoStrategy[] }>(`/algo/strategies`),
  algoCreateStrategy: (body: AlgoStrategyCreate) => postJson<AlgoStrategy>("/algo/strategies", body),
  algoStrategyAction: (strategyId: number, action: "start" | "pause") =>
    post<AlgoStrategy>(`/algo/strategies/${encodeURIComponent(strategyId)}/${action}`),
  algoDeleteStrategy: (strategyId: number) =>
    del<{ deleted: number }>(`/algo/strategies/${encodeURIComponent(strategyId)}`),
  algoKill: () => post<{ kill: boolean }>("/algo/kill"),
  algoResume: () => post<{ kill: boolean }>("/algo/resume"),
  algoStatus: () => get<AlgoStatus>("/algo/status"),
  algoReconciliation: () => get<AlgoReconciliation>("/algo/reconciliation"),
  // Weekly Advisor reports: read routes reassemble a
  // report on every call (no persisted `reports` row) — `weeks` for the
  // archive list, `latest`/`report(week)` for one full report. Accept/
  // override never update a row: each is an append-only INSERT returning
  // both the new row and the still-unchanged prior it supersedes; only a
  // `status:"proposed"` row is actionable (400 otherwise), and both are
  // read-only-rejected (403) in DEMO_MODE.
  reportWeeks: () => get<ReportWeeksResponse>("/reports/weeks"),
  reportLatest: () => get<AdvisorReport>("/reports/latest"),
  report: (reportWeek: string) => get<AdvisorReport>(`/reports/${encodeURIComponent(reportWeek)}`),
  // Report pack: the seven-chapter deep-dive alongside the
  // plain weekly report above. A 404 here (pre-pack week, or a demo week
  // outside the deterministic trailing window) is an expected, not-yet-built
  // state — callers render the plain report unchanged rather than an error.
  reportPack: (reportWeek: string) => get<ReportPack>(`/reports/${encodeURIComponent(reportWeek)}/pack`),
  acceptRecommendation: (recommendationId: number, note: string) =>
    postJson<RecommendationTransition>(`/reports/recommendations/${encodeURIComponent(recommendationId)}/accept`, { note }),
  overrideRecommendation: (recommendationId: number, note: string) =>
    postJson<RecommendationTransition>(`/reports/recommendations/${encodeURIComponent(recommendationId)}/override`, { note }),
  // Modify/reject/execute (Phase 18 — additive to the same
  // append-only transition table accept/override already use). `reject`
  // persists as `overridden` server-side (F1 — the frozen cooldown loop
  // reads exactly that status) with `human_action:"reject"` distinguishing
  // it; `execute` is only valid from an `accepted`/`modified` leaf.
  modifyRecommendation: (recommendationId: number, note: string, sizingOverride: number) =>
    postJson<RecommendationTransition>(`/reports/recommendations/${encodeURIComponent(recommendationId)}/modify`, {
      note, sizing_override: sizingOverride,
    }),
  rejectRecommendation: (recommendationId: number, note: string) =>
    postJson<RecommendationTransition>(`/reports/recommendations/${encodeURIComponent(recommendationId)}/reject`, { note }),
  executeRecommendation: (
    recommendationId: number,
    body: { note: string; executed_at: string; fill_price: number; quantity: number; venue: string },
  ) => postJson<RecommendationTransition>(`/reports/recommendations/${encodeURIComponent(recommendationId)}/execute`, body),
  // Decisions projection — read-only, live-computed chapter
  // shape, same as the pack's own `sections.decisions` (fetched separately
  // here so a caller can pin to a specific week without pulling the whole
  // pack). `week` accepts "latest" (resolved per-handler server-side).
  reportDecisions: (week: string) => get<PackDecisionsChapter>(`/reports/${encodeURIComponent(week)}/decisions`),
  // Forecast ledger reads. No issue/resolve client calls this
  // phase (F11 — human issuance/manual resolve are curl/Phase-19 surfaces,
  // no UI form yet); the endpoints exist server-side regardless.
  forecasts: (params: ForecastListParams = {}) =>
    get<ForecastsListResponse>(`/forecasts`, params as Record<string, string | number | undefined>),
  forecast: (id: number) => get<Forecast>(`/forecasts/${encodeURIComponent(id)}`),
  // Marquee ticker watchlist (Phase 14e): persisted, editable symbol list.
  // Whole-list replace — saveWatchlist sends the full ordered array, gets
  // the saved (normalized) list back.
  getWatchlist: () => get<WatchlistItem[]>("/watchlist"),
  saveWatchlist: (items: WatchlistItemInput[]) => putJson<WatchlistItem[]>("/watchlist", items),
};
