// Dark, Bloomberg-density theme tokens — the SINGLE canonical source of truth.
// The CSS custom properties in index.css (:root) mirror these values 1:1 for the
// CSS chrome; the drift guard (scripts/check-tokens.mjs) fails if they diverge.
// These JS constants exist because canvas/chart libraries (lightweight-charts,
// MapLibre, recharts) cannot read CSS variables. For beta this is the one dark
// theme; Phase 15's runtime switcher injects an alternate set.
export const theme = {
  // ── Surfaces + core semantic colors ──────────────────────────────
  bg: "#0d1117",
  panel: "#11161d",
  panelHeader: "#161b22",
  border: "#21262d",
  text: "#e6edf3",
  muted: "#838b96", // F4 2026-08-15: raised from #7d8590 for 4.5:1 contrast; kept in sync with index.css --muted per scripts/check-tokens.mjs
  accent: "#f0883e",
  up: "#3fb950",
  down: "#f85149",
  warn: "#d29922", // amber — caution / mid-range verdicts (was 9 raw-hex sites)
  grid: "#1c2128",

  // ── Categorical chart palette ─────────────────────────────────────
  // The one ordered set every multi-series chart draws from (Allocation, RRG,
  // RiskXray breakdowns, GeoMap layers). Fold widget hex arrays onto these by
  // index; map strays to the nearest hue. Hue-stable so a series keeps its color.
  chart: [
    "#58a6ff", // 1 blue
    "#f0883e", // 2 orange (accent)
    "#3fb950", // 3 green
    "#db61a2", // 4 pink
    "#a371f7", // 5 violet
    "#e3b341", // 6 gold
    "#56d4dd", // 7 cyan
    "#f85149", // 8 red
  ],

  // ── Type scale (px) ───────────────────────────────────────────────
  // 11 is the legibility floor — nothing renders below it.
  fontSize: {
    micro: 11, // agate / dense grid labels (absorbs the old 8–10px sites)
    sm: 12,
    base: 13, // body default (matches index.css body)
    lg: 15, // section / panel hero
    xl: 20, // wordmark / home standfirst figure
  },

  // ── Type families ─────────────────────────────────────────────────
  fontMono: '"SF Mono", "Fira Code", "JetBrains Mono", Consolas, monospace',
  // Prose/reading layer ONLY (deck lines, standfirst, section titles); every
  // numeral stays mono tabular.
  fontSerif: 'Georgia, "Times New Roman", serif',

  // ── Reading layer (Phase 15 fold-in, Q5) ─────────────
  // NOT a second theme (the full alternate palette stays parked) — just the
  // few extra metrics long-form prose needs that the dense agate/mono grid
  // elsewhere never does: a measured line length, open leading, one step up
  // in size. Used ONLY by NoteView's reading surface and a journal entry's
  // full view/thesis prose (JournalTimeline's per-row disclosure) — every
  // dense list/table row keeps the base scale. Headings on this layer still
  // use fontSerif above; this block is the body-copy metrics fontSerif
  // alone doesn't cover.
  reading: {
    measure: "66ch", // ideal long-form line length
    lineHeight: 1.6, // vs. the app's dense ~1.3 default
    fontSize: 15, // one step up from base (13) — a reading surface, not a grid row
  },
} as const;

export type Theme = typeof theme;
