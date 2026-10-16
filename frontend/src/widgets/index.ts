// Widget barrel. Light widgets self-register by side-effect import (one import
// line per widget). Heavy widgets (charts, maps) are registered lazily here —
// metadata is eager, the component + its deps code-split and load on first mount.
// Adding a widget = new file + one line here (widget-system rule).
import { registerLazyWidget } from "./registry";

import "./NetWorth";
import "./Holdings";
import "./Allocation";
import "./AccountTree";
import "./CurrencyBreakdown";
import "./Quotes";
import "./NewsPanel";
import "./MarketsBoard";
import "./MarketComposite";
// Macro & Rates — light widgets self-register; charts are lazy below.
import "./MacroHeatmap";
import "./RecessionSignals";
import "./EconCalendar";
import "./YieldCurve";
// Risk lens (Phase 8.5) — tables/bars, all light.
import "./RiskXray";
import "./RiskVaR";
import "./FxDrag";
import "./StressTest";
// Signal self-scoring core — leaderboard + grade card, all light.
import "./SignalScoreboard";
import "./SignalGradeCard";
// Backtester (Phase 9, engine) — the Lab runner is light (self-registers); the
// report ships its lightweight-charts chunk lazily (registered below).
import "./BacktestLab";
// Market Research (Phase 10a) — recharts-backed, all light (recharts already in
// the eager bundle via YieldCurve).
import "./RegimeComposite";
import "./CrossAssetMatrix";
import "./RotationBoard";
import "./RRG";
import "./VolRegime";
import "./MarketConditions";
import "./ValuationGauge";
// Equity Research (Phase 10b) — light widgets self-register; TechnicalOverlay is lazy below.
import "./Fundamentals";
import "./Valuation";
import "./ScoreCard";
import "./Screener";
// Quant / ML Lab — recharts-backed runners + scanner table, all light.
import "./FactorResearch";
import "./ModelZoo";
import "./Scanner";
// AI Intel — the runner + status card are light (self-register);
// the enriched news feed is list-heavy so it ships lazily (registered below).
import "./IntelRunner";
import "./AIStatus";
// Analyst Cockpit — light and catalog-only under the existing
// AI namespace; no default seed changes the Live surface.
import "./Cockpit";
// Paper-Trading Algo Hub — the ticket, positions book, and strategy
// panel are light (self-register); the orders blotter is list-heavy and SSE-fed
// so it ships lazily (registered below).
import "./OrderTicket";
import "./PositionsBook";
import "./StrategyPanel";
// Home tab (Phase 14b) — the welcome banner is light (self-registers).
import "./HomeIntro";
// Weekly Advisor reports — three registered widgets, all
// light (self-register). `RecommendationRow.tsx` is deliberately NOT
// imported here — it's a row COMPONENT ReportView imports directly
// (`RecommendationList`), not a widget ; it
// reaches the bundle through ReportView's own import graph instead.
import "./AdvisorScorecard";
import "./ReportArchive";
import "./ReportView";
// Analyst Journal + Notes — all four light (self-register).
// `NoteEditor.tsx`/`NoteView.tsx` share a local notes API layer the same way
// `JournalQuickEntry.tsx`/`JournalTimeline.tsx` share the journal one.
import "./JournalQuickEntry";
import "./JournalTimeline";
import "./NoteEditor";
import "./NoteView";

// ── Lazy (code-split) widgets ───────────────────────────────
registerLazyWidget(
  {
    id: "price-chart",
    title: "Price Chart",
    defaultSize: { w: 7, h: 8 },
    minSize: { w: 3, h: 5 },
    defaultConfig: { symbol: "AAPL", interval: "1d" },
    category: "Equity Research",
    description: "Candlestick chart with symbol / source / interval controls",
  },
  () => import("./PriceChart").then((m) => ({ default: m.PriceChart })),
);

registerLazyWidget(
  {
    id: "geo-map",
    title: "Geopolitical Map",
    defaultSize: { w: 8, h: 14 },
    minSize: { w: 4, h: 8 },
    defaultConfig: {
      layers: { economic: true, sanctions: true, trade: true, natural: true, ai_events: false },
    },
    category: "Markets",
    description: "World map: quakes + economic / sanctions / trade layers",
  },
  () => import("./GeoMap").then((m) => ({ default: m.GeoMap })),
);

registerLazyWidget(
  {
    id: "macro-series",
    title: "Macro Series",
    defaultSize: { w: 6, h: 8 },
    minSize: { w: 3, h: 5 },
    defaultConfig: { series: "CPI.united_states", transform: "toya" },
    category: "Macro",
    description: "FRED / econdb time series (CPI, GDP, rates, …)",
  },
  () => import("./MacroSeries").then((m) => ({ default: m.MacroSeriesWidget })),
);

registerLazyWidget(
  {
    id: "rate-spreads",
    title: "Treasury Spreads",
    defaultSize: { w: 6, h: 7 },
    minSize: { w: 3, h: 5 },
    defaultConfig: {},
    category: "Rates",
    description: "10y-2y / 10y-3m spread history with a zero baseline",
  },
  () => import("./RateSpreads").then((m) => ({ default: m.RateSpreads })),
);

registerLazyWidget(
  {
    id: "market-sentiment",
    title: "Sentiment & Positioning",
    defaultSize: { w: 6, h: 16 },
    minSize: { w: 4, h: 10 },
    defaultConfig: {},
    category: "Market Research",
    description: "CNN Fear & Greed gauge, component breakdown, and COT futures positioning by asset class",
  },
  () => import("./SentimentPositioning").then((m) => ({ default: m.SentimentPositioning })),
);

registerLazyWidget(
  {
    id: "market-breadth",
    title: "Market Breadth",
    defaultSize: { w: 6, h: 16 },
    minSize: { w: 4, h: 10 },
    defaultConfig: {},
    category: "Market Research",
    description: "NYSE/Nasdaq advance-decline breadth snapshot, A/D line, and McClellan oscillator (whole-market, source WSJ)",
  },
  () => import("./BreadthBoard").then((m) => ({ default: m.BreadthBoard })),
);

registerLazyWidget(
  {
    id: "research-technicals",
    title: "Technical Overlay",
    defaultSize: { w: 7, h: 9 },
    minSize: { w: 3, h: 5 },
    defaultConfig: { symbol: "AAPL" },
    category: "Equity Research",
    description:
      "Price + SMA50 + SMA200 line chart with RSI-14, 52-week range position, and trend badge",
  },
  () => import("./TechnicalOverlay").then((m) => ({ default: m.TechnicalOverlay })),
);

registerLazyWidget(
  {
    id: "backtest-report",
    title: "Backtest Report",
    defaultSize: { w: 6, h: 16 },
    minSize: { w: 4, h: 10 },
    defaultConfig: {},
    category: "Backtest",
    description: "Equity curve + drawdown + the qlib metric bundle for one finished backtest",
  },
  () => import("./BacktestReport").then((m) => ({ default: m.BacktestReport })),
);

registerLazyWidget(
  {
    id: "ai-news",
    title: "AI News",
    defaultSize: { w: 6, h: 16 },
    minSize: { w: 4, h: 8 },
    defaultConfig: {},
    category: "AI",
    description: "RSS enriched with grounded sentiment, topics, and summaries via Claude Code",
  },
  () => import("./AINews").then((m) => ({ default: m.AINews })),
);

registerLazyWidget(
  {
    id: "algo-blotter",
    title: "Orders Blotter",
    defaultSize: { w: 7, h: 9 },
    minSize: { w: 5, h: 6 },
    defaultConfig: {},
    category: "Algo",
    description: "Live order + fill history against the paper OMS, streamed over SSE",
  },
  () => import("./OrdersBlotter").then((m) => ({ default: m.OrdersBlotter })),
);
