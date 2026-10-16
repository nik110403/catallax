# React Widget System (Catallax)

Contract for dashboard widgets. Written as the Phase 3 spec; implementation
lands in Phase 3. Every widget must follow this — it's what makes widgets
independently mountable and the grid config-driven.

## Concepts

- **Widget**: a self-contained panel (chart, ticker, news, net worth, …).
- **Registry**: module-level map of `widgetId → WidgetDefinition`. The grid
  renders only what the registry knows about.
- **Self-registration**: each widget module calls `registerWidget(...)` at
  import time. The registry barrel (`widgets/index.ts`) imports all widget
  modules; adding a widget = new file + one import line, nothing else changes.

## Interfaces

```ts
// widgets/registry.ts
export interface WidgetProps<C = unknown> {
  config: C;                          // widget instance config (persisted)
  onConfigChange: (next: C) => void;  // persist config changes
}

export interface WidgetDefinition<C = unknown> {
  id: string;                         // unique, kebab-case: "price-chart"
  title: string;                      // default panel title
  component: React.ComponentType<WidgetProps<C>>;
  defaultSize: { w: number; h: number };   // grid units
  minSize?: { w: number; h: number };
  defaultConfig: C;
  configSchema?: unknown;             // optional JSON schema for config UI
  category?: string;                  // catalog grouping (Personal / Markets / Research / General)
  description?: string;               // one-line catalog blurb (falls back to title)
  help?: string;                      // multi-line "what it shows + how to read it" for the panel ⓘ popover
}

const registry = new Map<string, WidgetDefinition>();
export function registerWidget(def: WidgetDefinition): void { registry.set(def.id, def); }
export function getWidget(id: string): WidgetDefinition | undefined { return registry.get(id); }
export function listWidgets(): WidgetDefinition[] { return [...registry.values()]; }

// F4: register a code-split widget — metadata is eager, the component (and its
// heavy deps) load on first mount via React.lazy. Render sites are inside
// <Suspense> (Dashboard wraps every panel).
type WidgetMeta = Omit<WidgetDefinition, "component">;
type WidgetLoader = () => Promise<{ default: React.ComponentType<WidgetProps> }>;
export function registerLazyWidget(meta: WidgetMeta, loader: WidgetLoader): void { /* ... */ }
```

## Adding a New Widget

1. Create `frontend/src/widgets/<WidgetName>.tsx` implementing `WidgetProps<C>`
   for its config type `C`. Set a `category` + `description` so it lands in the
   right group of the widget catalog.
2. Register it:
   - **Light widget** (no heavy deps): call `registerWidget({...})` in the module
     and add a side-effect `import "./<WidgetName>";` line to `widgets/index.ts`.
   - **Heavy widget** (pulls a big lib like `lightweight-charts` / `maplibre-gl`):
     `export` the component (don't self-register) and add a `registerLazyWidget({meta},
     () => import("./<WidgetName>").then(m => ({ default: m.<WidgetName> })))` entry to
     `widgets/index.ts` so the lib code-splits and loads on mount. Add the lib to
     `vite.config.ts` `manualChunks` if it should be its own chunk.
3. Update this file's widget catalogue below.

No existing widget, grid, or registry code is touched.

## Rules

- Widgets fetch data only from the FastAPI backend (`/api/v1/...`) or its
  WebSocket endpoints — never from Fava, Wealthfolio, or external APIs.
- Widgets own their loading/error states; the grid never knows about them.
- **Panel help (ⓘ):** every panel header shows an ⓘ button that opens a hover/click
  popover explaining what the widget shows and how to read it. The text is resolved
  as `def.help ?? WIDGET_HELP[id]` — set `help` inline on the definition, or add an
  entry to the central map `widgets/help.ts` (keyed by id; blank line = paragraph
  break). Every new widget MUST have help text (one of the two). The popover is
  portalled to `<body>` at a fixed position (Panel.tsx) so the panel's
  `overflow:hidden` can't clip it.
- All instance state that should survive reload goes through `config` /
  `onConfigChange` — no ad-hoc localStorage inside widgets.
- Styling: dark theme tokens, Bloomberg-density (compact paddings, monospace
  numerics). Theme tokens defined in Phase 3.

## Widget Catalogue

| id | Title | Purpose | Data source | Phase |
|---|---|---|---|---|
| `net-worth` | Net Worth | EUR net-worth area chart + latest figure; **All / Investments** scope toggle (`?scope=all\|investments`) | `GET /networth` (Wealthfolio) | 3 |
| `price-chart` | Price Chart | Candlestick chart; symbol/source/interval controls | `GET /prices/{sym}/history` | 3 |
| `account-tree` | Accounts | Indented multi-currency ledger balance tree | `GET /ledger/accounts` | 3 |
| `news` | Market News | Aggregated RSS headlines (polled). **Seeded only on the Markets tab** (not Personal/Macro); still addable anywhere via the catalog | `GET /news` | 3 |
| `quotes` | Watchlist | Live quotes table for a symbol list | WS `/ws/prices` | 3 |
| `holdings` | Holdings | Positions table — qty / live price / value / P&L, with summary header | `GET /portfolio/holdings` | 4 |
| `allocation` | Allocation | SVG donut + legend, by asset class | `GET /portfolio/allocation` | 4 |
| `currency-breakdown` | Currencies | Per-currency units / value / share bar | `GET /ledger/currencies` | 4 |
| `signal-scoreboard` | Signal Scoreboard | Leaderboard of logged signals by ICIR / Rank-IC + a seed button | `GET /signals/scoreboard` (Envelope) | 9 |
| `signal-grade-card` | Signal Grade | One signal: Rank-IC over time, quintile spread, ICIR/t (lookahead-clean) | `GET /signals/{id}/grade` (Envelope) | 9 |
| `backtest-lab` | Backtest Lab | Configure + run a signal-driven backtest off-API; live SSE progress + inline equity/metrics | `POST /backtests`, `GET /backtests`, SSE `/backtests/{id}/stream` | 9 |
| `backtest-report` | Backtest Report | Equity + drawdown (lightweight-charts, lazy) + the qlib metric bundle for one run | `GET /backtests/{id}/report` (Envelope) | 9 |

(Later phases — 7 Markets, 8 Macro/Rates, 8.5 Risk — add their own widgets in the same
self-registering pattern; the table above is the core. Phase 9's signal widgets are
`category:"Signals"`; the backtester widgets are `category:"Backtest"`. `backtest-lab` is
light + self-registering and embeds the SSE `JobStatus` progress component; `backtest-report`
is `registerLazyWidget` (lightweight-charts chunk). See `skills/backtest-engine.md`.)

**Market Research (Phase 10a) — namespace `market-research`, category `"Market Research"`:**

| id | Title | Purpose | Data source |
|---|---|---|---|
| `market-regime` | Market Regime | Composite risk-on/off score + per-component diverging bars + score history AreaChart + recession/Sahm flag | `GET /market/regime` (Envelope) |
| `market-cross-asset` | Cross-Asset | Correlation heatmap (symbols × symbols, blue→red) + snapshot strip (1D/1W/1M/YTD) + ratios + avg-corr sparkline | `GET /market/cross-asset` (Envelope) |
| `market-rotation` | Sector & Factor Rotation | Heatmap-coloured return table (1D/1W/1M/3M/6M/YTD + RS vs benchmark), grouped by sector/factor | `GET /market/rotation` (Envelope) |
| `market-rrg` | Relative Rotation (RRG) | RRG ScatterChart: RS-ratio vs RS-momentum, quadrant labels, tail polylines per series, group legend | `GET /market/rrg` (Envelope) |
| `market-volatility` | Volatility Regime | Big VIX + percentile, term structure badge (contango/backwardation), realized vol, implied-minus-realized, MOVE, VIX history LineChart | `GET /market/volatility` (Envelope) |
| `market-conditions` | Financial Conditions | Tile grid: value + unit, centred z-score mini-bar, percentile, trend arrow, risk-direction coloring | `GET /market/conditions` (Envelope) |
| `market-valuation` | Valuation | Tile grid: CAPE, P/E, Buffett indicator, real 10y yield — value + percentile bar (red = expensive, green = cheap); needs_source tiles render "pending v1.1" | `GET /market/valuation` (Envelope) |

Default seed (focused 4 panels): `market-regime` (0,0,5,11), `market-conditions` (5,0,7,11), `market-rotation` (0,11,7,10), `market-cross-asset` (7,11,5,10). RRG, volatility, valuation are catalog-only. All 7 are light (self-registering), importing recharts which is already in the eager bundle via YieldCurve.

Sentiment & Positioning (`market-sentiment`) + Market Breadth (`market-breadth`) are lazy (v1.1/v1.2), seeded full-width below.

**Equity Research (Phase 10b) — namespace `research`, category `"Research"`:**

| id | Title | Purpose | Data source |
|---|---|---|---|
| `research-fundamentals` | Fundamentals | Symbol input → income/balance/cashflow tables (cols = fiscal years) + derived margins/ROE/ROA | `GET /research/{symbol}/fundamentals` |
| `research-valuation` | Valuation | Price + market cap header + ratio tile grid (P/E, P/B, P/S, EV/EBITDA, FCF/Div yield, ROE, ROA) | `GET /research/{symbol}/valuation` |
| `research-scorecard` | Forensic Scores | Three cards (Piotroski F / Altman Z / Beneish M): score + verdict badge + per-component ✓/✗/n-a breakdown | `GET /research/{symbol}/scores` |
| `research-technicals` | Technical Overlay | recharts close + SMA50/200 lines + stat strip (last, vs-200dma, 52w range, RSI-14, trend); **lazy** | `GET /research/{symbol}/technicals` |
| `research-screen` | Stock Screener | Filter bar (min F-score / max P/E / min div / min Z) + scored results table over the config universe | `GET /research/screen` (typed filter params) |

Each symbol widget carries its own `config.symbol` (default `AAPL`) with an in-header input (PriceChart pattern). Default seed: `research-scorecard` (0,0,5,16), `research-valuation` (5,0,7,8), `research-technicals` (5,8,7,8), `research-fundamentals` (0,16,7,16), `research-screen` (7,16,5,16). Four are light; `research-technicals` is lazy (recharts). Workspaces **v9** adds the Research tab after Market Research.

**Quant / ML Lab — namespace `quant-lab`, category `"Quant Lab"`:**

| id | Title | Purpose | Data source |
|---|---|---|---|
| `lab-factor` | Factor Research | Config form → off-API factor-IC study; live SSE progress + inline IC series / quantile returns / IR / t-stat | `POST /lab/factors`, SSE `/lab/runs/{id}/stream`, `GET /lab/runs/{id}/report` |
| `lab-models` | Model Zoo | Config form → off-API ML train/eval (sklearn-ensemble / deep-zoo); live progress + metrics (Rank-IC/R²/hit-rate) + feature importances | `POST /lab/models`, SSE stream, `GET /lab/runs/{id}/report` |
| `lab-scanner` | Technical Scanner | Synchronous chart-pattern scan over the F0 OHLCV backbone; matched patterns + net bias score per name; optional emit-to-signals | `GET /lab/scan` (Envelope) |

`lab-factor`/`lab-models` mirror `BacktestLab` (config → POST → `<JobStatus streamUrl={labStreamUrl}>` SSE → inline report); `JobStatus` gained an optional `streamUrl` prop so it is shared between the backtester and the lab. `lab-scanner` mirrors `Screener`. Default seed: `lab-factor` (0,0,6,18), `lab-models` (6,0,6,18), `lab-scanner` (0,18,7,15), `signal-scoreboard` (7,18,5,15 — the reused SignalScores slot). All three are light (self-registering, recharts already eager). Workspaces **v10** adds the Quant Lab tab after Backtest. See `skills/quant-lab.md`.

**AI Intelligence (Phase 12, opt-in `--profile ai`) — namespace `ai`, category `"AI"`:**

| id | Title | Purpose | Data source |
|---|---|---|---|
| `ai-news` | AI News | RSS enriched via the off-API `ai-runner`/Claude Code worker — sentiment chip, country tag, topic labels, grounded/ungrounded dot, model summary; untrusted `link` guarded by `safeHref` (http(s)-only), rendered via React text nodes (no `dangerouslySetInnerHTML`) | `GET /intel/news` |
| `ai-intel-runner` | Intel Runner | "Enrich now" button → enqueues an off-API enrich job → the shared `<JobStatus>` SSE component streams progress to completion | `POST /intel/enrich`, SSE `/intel/runs/{id}/stream` |
| `ai-status` | AI Status | Provider health card for the configured LLM provider (`claude-code` when `ai-runner` is up) | `GET /intel/status` |
| `cockpit` | Analyst Cockpit | Persisted, read-only analyst conversation: cited answers and job-polled turns; disabled until `COCKPIT_ENABLED=true` | `GET/POST /cockpit/sessions`, `POST /cockpit/sessions/{key}/ask`, `GET /cockpit/jobs/{id}` |

`ai-news` is `registerLazyWidget` (own chunk, matches the heavy-widget pattern); `ai-intel-runner`/`ai-status` are light self-registering. Workspaces **v11** adds the AI tab; palette code `AI`. `GeoMap.tsx` also gained a fourth toggleable layer, **AI Events** (`ai_events_layer`), plotting `GET /intel/events` (sentiment-tagged geo-located articles) as colored markers with a click popup; because that popup renders untrusted RSS-derived text through MapLibre's raw `setHTML`, it goes through an explicit `escapeHtml()` helper first (the map's other three popup branches — quakes/hubs/sanctions — use curated/USGS data and were left on raw `setHTML`, logged as a lower-risk follow-up). See `skills/ai-intel.md`.

**Paper-Trading Algo Hub — namespace `algo`, category `"Algo"`:**

| id | Title | Purpose | Data source |
|---|---|---|---|
| `algo-ticket` | Order Ticket | Manual order entry (symbol/side/type/qty/limit) against the paper OMS; debounced live quote preview via `api.quote`; structured 400 detail shown inline; red banner when the kill switch is engaged | `POST /algo/orders`, `GET /algo/status` |
| `algo-blotter` | Orders Blotter | Order + fill history, status-colored (filled green / rejected red / accepted amber / cancelled gray) with reason; Cancel on new/accepted; live via SSE + 15s poll fallback | `GET /algo/orders`, `GET /algo/fills`, SSE `/algo/stream` |
| `algo-positions` | Positions Book | Positions table (qty/avg cost/mark/unrealized P&L) + account strip (cash/equity/day P&L) + equity sparkline from account snapshots | `GET /algo/positions`, `GET /algo/account` |
| `algo-strategies` | Strategy Panel | Signal-driven strategy CRUD (create/start/pause/delete) over a comma-separated universe; engine heartbeat dot; global KILL SWITCH / RESUME (window.confirm-gated) | `GET/POST /algo/strategies`, `POST /algo/kill`, `POST /algo/resume`, `GET /algo/status` |

`algo-ticket`/`algo-positions`/`algo-strategies` are light (self-registering); `algo-blotter` is `registerLazyWidget` (list-heavy, SSE-fed). Default seed: `algo-ticket` (0,0,4,8), `algo-positions` (4,0,8,8), `algo-blotter` (0,8,7,9), `algo-strategies` (7,8,5,9). Workspaces **v12** adds the Algo tab; palette code `ALGO`. OMS shapes: `backend/app/services/algo.py` / `backend/app/routers/algo.py`.

**Advisor Reports — namespace `reports`, category `"Reports"` (Portfolio section, `/portfolio/reports`):**

| id | Title | Purpose | Data source |
|---|---|---|---|
| `report-view` | Weekly Report | The report as a DOCUMENT — all four sections in reading order (What happened / Where we stand / What to do next / Scorecard counts); narration or its absence-reason as a one-line preamble; recommendation rows (accept/override with required reason, near-miss rows muted with would-fire-at arithmetic, `refuse` rows as an alarm banner) rendered by the unregistered row components in `RecommendationRow.tsx` | `GET /reports/latest`, `GET /reports/{week}`, `POST` accept/override |
| `report-archive` | Report Archive | Week list → selects the week shown (secondary; ReportView also has its own week selector in `PanelHead`) | `GET /reports/weeks` |
| `advisor-scorecard` | Advisor Scorecard | The honest-statistics panel: actual vs do-nothing TWR (modified-Dietz, CF=0 consequence stated), `n_effective` (ties excluded), `depth_weeks`, p-FLOOR (labeled as a floor, `None`-reason at n=0), arms-coincide note | `GET /reports/latest` scorecard.performance |

All three light self-registering; `RecommendationRow.tsx` exports row components (`RecommendationList`/`RecommendationCard`/`RefusalBanner`/`NearMissRow`) imported by ReportView — deliberately NOT a registered widget. Shared helpers in `widgets/reportsShared.tsx`. Workspaces **v14** appends the Reports tab (migrates v13 without loss). Distinct from `research-scorecard` (Forensic Scores, Phase 10b). Payload contract: `skills/portfolio-advisor.md`.

`report-view` renders the full weekly **pack** (Phase 17's institutional-style chapters —
exec summary, portfolio, market, holdings, quant, shadow, methodology) via
`widgets/ReportPackChapters.tsx`'s `ReportPackChapters` — same "document, not loose
panels" ruling as `RecommendationRow.tsx`: every chapter body (`ExecSummaryBody`,
`PortfolioBody`, `MarketBody`, `HoldingsBody`, `QuantBody`, `ShadowBody`,
`MethodologyBody`, …) is an unregistered function component switched on by
`sections` keys, not a widget. **Phase 18 adds three chapter bodies**, additive keys
only (older packs without them render unchanged):

| Chapter body | `sections` key | Renders |
|---|---|---|
| `ForecastsBody` | `forecasts` | Issued/duplicates/skipped counts from the week's forecast-issuance hook, pending count, next due date |
| `CalibrationBody` | `calibration` | Per issuer×horizon `CalibrationGroupCard`: summary stat row (Brier / ref / skill / hit-rate vs benchmark / effective n / abstention rate); a null `bins` group shows `bins_note` in the caption register instead of a reliability table; present bins render as a table with `n=0` rows dimmed, never hidden; Brier skill coloured only when `!low_confidence` |
| `DecisionsBody` | `decisions` | One `DecisionCard` per current recommendation leaf: action, status chip, `fundingSourceLine`, `riskDeltaLine`, invalidation text + `still_valid`, review/expiry dates, collapsed lifecycle chain |

Registered in the chapter switch after `shadow`, before `methodology`. Payload
contract for all three: `skills/forecast-ledger.md`. `RecommendationRow.tsx`'s
`RecommendationCard` also gained **Modify**/**Reject**/**Execute** actions in Phase
18 (status vocabulary: `skills/portfolio-advisor.md`); `DecidedRow` renders
`expired`/`superseded` as muted chips. No Issue-forecast UI form this phase
(backend-only path, see `skills/forecast-ledger.md`).

Plus a non-grid top **TickerBar** strip (`widgets/TickerBar.tsx`, WS `/ws/prices`).
Chart widgets use lightweight-charts (v4); see `widgets/chartTheme.ts`. Theme
tokens: `src/theme.ts` (JS, for canvas) + `src/index.css` (CSS custom props).

## Dynamic-UI platform

The shell around the grid. All four pieces are pure frontend except persistence.

- **Workspaces + dynamic tabs** (`workspaces.ts`, `App.tsx`): a workspace is a
  named, ordered set of tabs; each tab points at a layout *namespace* whose panel
  presets live in `layouts.ts`. Switching a workspace swaps which namespaces are on
  screen — it never moves a panel. Tabs are add/rename/reorder/delete from the
  header; the built-in `Daily` workspace seeds `Personal` + `Markets`.
  **Keep-alive (2026-06-24):** every tab opened in a session stays mounted; only
  the active pane is shown (`display:contents`), the rest are `display:none`.
  Returning to a tab is instant — no remount, no `useAsync` refetch/loading flash.
  A tab's `Dashboard` first mounts only when it becomes active (so it's visible →
  react-grid-layout / MapLibre measure a real width, never 0 from a hidden pane).
  Each pane is keyed by tab id so namespaces never bleed across tabs (the Phase 6
  remount existed for that; per-instance panes give the same isolation without the
  reload). Chart widgets that resize (`MacroSeries`, `GeoMap`) use a `ResizeObserver`
  so they self-correct on show.
- **News is Markets-only:** `layouts.ts::sanitize()` strips any `news` panel from
  every non-`markets` namespace on load (localStorage *and* the server blob, via
  `validateStored`), then the cleaned state is written back through. Stale layouts
  persisted before news was pulled from the other seeds self-heal on next load.
- **Help button (ⓘ):** `Panel.tsx` renders one portalled `.help-pop` popover; it
  carries no native `title` (that would stack a second browser tooltip over it).
- **Command palette** (`CommandPalette.tsx`, ⌘K / Ctrl+K): fuzzy search over
  navigation + widgets + live symbols. Function codes: `NW` (net worth), per-tab
  `Go to …`, `GO <SYM>` / a bare ticker → add a price-chart on that symbol. Symbol
  lookup reuses `GET /api/v1/search` (`api.search()`). It dispatches a
  `PaletteCommand` (`navigation.ts`) up to `App`, which switches the tab and hands
  the active `Dashboard` a `PendingAction` to apply on mount (add/focus a widget).
- **Widget catalog** (`WidgetCatalog.tsx`): the "+ widget" gallery, grouped by
  `category`, over `listWidgets()` (metadata only — listing never mounts a widget,
  so no data calls). Legacy widgets without a `category` fall back to a map in the
  catalog; new widgets should set `category` on their def.
- **Server-side persistence** (`remoteStore.ts` → `GET/PUT /api/v1/layouts/{key}`):
  workspaces + per-namespace presets persist to Postgres (table `layouts`, key→JSONB)
  so state follows the user across devices. localStorage stays the synchronous
  first-paint source + offline fallback; the backend is hydrated once on load
  (server wins) and written through debounced. Keys: `workspaces`,
  `presets:<namespace>`. The store is opaque to the backend (allowlisted key,
  size-capped blob); validators in `layouts.ts`/`workspaces.ts` gate any parsed
  blob before it reaches React state.
