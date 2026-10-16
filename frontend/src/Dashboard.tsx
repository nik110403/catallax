// Grid dashboard shell for one tab. react-grid-layout panels, each rendering a
// registered widget. Panels belong to the active named preset; presets are
// created / renamed / deleted / switched from the bar and persist to localStorage
// (see layouts.ts). Adding a widget = pick from the registry; the grid is fully
// config-driven.
import { Suspense, useEffect, useRef, useState } from "react";
import GridLayout, { WidthProvider } from "react-grid-layout";
import type { Layout } from "react-grid-layout";

import { panelUid, usePresets, type PanelInstance } from "./layouts";
import type { PendingAction } from "./navigation";
import { theme } from "./theme";
import { WidgetCatalog } from "./WidgetCatalog";
import { WIDGET_HELP } from "./widgets/help";
import { Panel } from "./widgets/Panel";
import { getWidget } from "./widgets/registry";
import { WidgetErrorBoundary } from "./widgets/WidgetErrorBoundary";
import "./widgets"; // side-effect: registers all widgets

const Grid = WidthProvider(GridLayout);

// [widgetId, x, y, w, h] with an optional 6th element overriding defaultConfig
// (e.g. two yield-curve panels seeded as US + EU).
type Placement = Array<
  [string, number, number, number, number] | [string, number, number, number, number, unknown]
>;

function buildSeed(place: Placement): PanelInstance[] {
  return place
    .map((row) => {
      const [widgetId, x, y, w, h, config] = row as [
        string,
        number,
        number,
        number,
        number,
        unknown?,
      ];
      const def = getWidget(widgetId);
      if (!def) return null;
      return { i: panelUid(), widgetId, config: config ?? def.defaultConfig, x, y, w, h };
    })
    .filter((p): p is PanelInstance => p !== null);
}

// Home tab default (Phase 14b, re-curated 14e): the welcome banner spans the
// full width up top (short); a one-glance snapshot below without opening a
// tab. Net worth takes the left half at hero height; the right half stacks
// the markets board (seeded to the indices group only — the full 30+-row
// cross-asset board doesn't belong at hero size 14b) over the
// market regime composite, together summing to net worth's height so the
// column has no void. (Was three equal thirds with net-worth/markets-board
// both h:25 against market-regime's h:14 — the mismatch left the market
// regime's own third empty for 11 rows below the fold.)
export function homeSeed(): PanelInstance[] {
  return buildSeed([
    ["home-intro", 0, 0, 12, 4],
    ["net-worth", 0, 4, 6, 20],
    ["markets-board", 6, 4, 6, 9, { group: "indices" }],
    ["market-regime", 6, 13, 6, 11],
  ]);
}

// Personal tab default: the portfolio exporter drives the top (net worth + allocation);
// the ledger drives the bottom (full-width holdings, full-width account tree).
// News lives only on the Markets tab. Every panel is drag/resize/removable.
// Currencies: this ledger has exactly one currency, so the
// widget rendered a hero + one data row in an ~80%-empty box — cut from the
// default seed and let holdings take the freed width instead of leaving a
// gap. The widget stays registered (catalog + ⌘K) for anyone with a
// multi-currency ledger; this is a default-layout call, not a removal.
export function personalSeed(): PanelInstance[] {
  return buildSeed([
    ["net-worth", 0, 0, 7, 8],
    ["allocation", 7, 0, 5, 8],
    ["holdings", 0, 8, 12, 8],
    ["account-tree", 0, 16, 12, 8],
  ]);
}

// Markets tab default: cross-asset board + geopolitical map on top, risk
// composite + markets news below.
export function marketsSeed(): PanelInstance[] {
  return buildSeed([
    ["market-composite", 0, 0, 4, 4],
    ["markets-board", 0, 4, 4, 18],
    ["geo-map", 4, 0, 8, 16],
    ["news", 4, 16, 8, 8],
  ]);
}

// Macro tab default (Phase 8, re-curated 14e): indicator heatmap + a series
// chart on top; recession composite + econ calendar below (each half-width).
// The recession tile lives here (its macro home), not on Rates, so the two
// tabs don't duplicate it. News lives only on the Markets tab.
// Row heights sized to each widget's own defaultSize (its author's own
// content-fit number), not an arbitrary target — the two content-fixed
// tiles (heatmap, recession-signals) get exactly that; the two elastic ones
// (series chart flex-fills, calendar is a scrolling list) match them rather
// than the reverse. The old seed (5+8=13 left column rows vs 9+13=22 right)
// left a genuinely empty ~9-row band across the whole bottom-left quadrant
// regardless of the macro service being up — a seed math bug, not a
// disabled-state artifact (that's MacroSeries.tsx's `fill` fix instead).
export function macroSeed(): PanelInstance[] {
  return buildSeed([
    ["macro-heatmap", 0, 0, 7, 8],
    ["macro-series", 7, 0, 5, 8],
    ["recession-signals", 0, 8, 6, 7],
    ["econ-calendar", 6, 8, 6, 7],
  ]);
}

// Rates tab default: US + EU yield curves on top, full-width spread
// history below (fills the viewport; recession composite lives on the Macro tab).
export function ratesSeed(): PanelInstance[] {
  return buildSeed([
    ["yield-curve", 0, 0, 6, 9, { region: "US" }],
    ["yield-curve", 6, 0, 6, 9, { region: "EU" }],
    ["rate-spreads", 0, 9, 12, 13],
  ]);
}

// Risk tab default (Phase 8.5): balanced two-column grid (both columns sum to 22
// rows so neither leaves a void). All computed over the imported portfolio.
export function riskSeed(): PanelInstance[] {
  return buildSeed([
    ["risk-xray", 0, 0, 7, 8],
    ["stress-test", 0, 8, 7, 9],
    ["risk-var", 7, 0, 5, 9],
    ["fx-drag", 7, 9, 5, 13],
  ]);
}

// Signals tab default: the scoreboard leaderboard on the left, the
// grade-card drill-down on the right (both columns ≈22 rows so neither leaves a
// void). All graded from F0 forward returns; empty until the reference seed runs.
export function signalsSeed(): PanelInstance[] {
  // Balanced two-column grid (both columns ≈18 rows) so neither leaves a void:
  // the scoreboard is short (one row per signal) so it sits tight above the chart;
  // the grade card spans the right column at a content-fit height.
  return buildSeed([
    ["signal-scoreboard", 0, 0, 7, 7],
    ["signal-grade-card", 7, 0, 5, 18, { signalId: "xs_mom_252_21" }],
    ["price-chart", 0, 7, 7, 11],
  ]);
}

// Market Research tab default (Phase 10a): regime composite + conditions tiles
// on top (the two flagship panels); rotation board + cross-asset matrix below.
// Sentiment & Positioning + Market Breadth span full width at the bottom (v1.2).
// The other three widgets (RRG, volatility, valuation) are catalog-only.
export function marketResearchSeed(): PanelInstance[] {
  return buildSeed([
    ["market-regime", 0, 0, 5, 12],
    ["market-conditions", 5, 0, 7, 7],
    ["market-rotation", 0, 12, 7, 18],
    ["market-cross-asset", 7, 7, 5, 18],
    ["market-sentiment", 0, 30, 12, 18],
    ["market-breadth", 0, 48, 6, 16],
  ]);
}

// Research tab default (Phase 10b): forensic scores + valuation on top (left/right
// split); technicals fills the right below valuation; fundamentals + screener below.
export function researchSeed(): PanelInstance[] {
  return buildSeed([
    ["research-scorecard", 0, 0, 5, 16, { symbol: "AAPL" }],
    ["research-valuation", 5, 0, 7, 8, { symbol: "AAPL" }],
    ["research-technicals", 5, 8, 7, 8, { symbol: "AAPL" }],
    ["research-fundamentals", 0, 16, 7, 16, { symbol: "AAPL" }],
    ["research-screen", 7, 16, 5, 16],
  ]);
}

// Backtest tab default (Phase 9, engine): the Lab (config form → off-API run →
// live SSE progress → inline equity curve + metric bundle) on the left. The right
// column shows which signals you can backtest — the scoreboard up top, then a
// grade card beneath it so the column fills instead of leaving a void.
export function backtestSeed(): PanelInstance[] {
  return buildSeed([
    ["backtest-lab", 0, 0, 8, 22],
    ["signal-scoreboard", 8, 0, 4, 7],
    ["signal-grade-card", 8, 7, 4, 15, { signalId: "xs_mom_252_21" }],
  ]);
}

// Quant / ML Lab tab default (Phase 11, --profile lab): factor research (IC study)
// top-left, the ML model zoo (train/eval) top-right — both off-API runners with
// live SSE progress + inline results. The technical scanner spans the lower-left;
// the signal scoreboard (which the scanner can feed) sits lower-right.
export function quantLabSeed(): PanelInstance[] {
  return buildSeed([
    ["lab-factor", 0, 0, 6, 18],
    ["lab-models", 6, 0, 6, 18],
    ["lab-scanner", 0, 18, 7, 15],
    ["signal-scoreboard", 7, 18, 5, 15],
  ]);
}

// AI tab default: the runner + status card sit top-left (small,
// content-fit), the enriched news feed fills the right column.
export function aiSeed(): PanelInstance[] {
  return buildSeed([
    ["ai-intel-runner", 0, 0, 5, 6],
    ["ai-status", 0, 6, 5, 5],
    ["ai-news", 5, 0, 7, 22],
  ]);
}

// Algo tab default (Phase 13, paper-trading hub): the order ticket sits
// top-left (small, content-fit), positions book fills the top-right, the
// orders blotter spans the lower-left, and the strategy panel (incl. the
// kill switch) sits lower-right.
export function algoSeed(): PanelInstance[] {
  return buildSeed([
    ["algo-ticket", 0, 0, 4, 8],
    ["algo-positions", 4, 0, 8, 8],
    ["algo-blotter", 0, 8, 7, 9],
    ["algo-strategies", 7, 8, 5, 9],
  ]);
}

// Reports tab default: the Weekly Report document takes the
// left, wide column (it's the document — one scroll, all four sections);
// the archive sits alongside as the secondary, optional selector
// (ReportArchive is optional/
// secondary, never a fourth loose panel duplicating the document).
// `advisor-scorecard` stays catalog-only, same treatment as RRG/volatility/
// valuation on Market Research — ReportView already renders its own
// Scorecard section from the same component.
export function reportsSeed(): PanelInstance[] {
  return buildSeed([
    ["report-view", 0, 0, 8, 30],
    ["report-archive", 8, 0, 4, 30],
  ]);
}

// Phase 19 — the Journal + Notes tabs were added elsewhere, but
// `SEEDS`/`*Seed()` live here, so without seeds both tabs would have
// opened on an empty grid. Quick entry leads (writing is the point), timeline
// alongside as the reading surface; Notes mirrors that with the editor
// leading and the rendered view beside it.
export function journalSeed(): PanelInstance[] {
  return buildSeed([
    ["journal-quick-entry", 0, 0, 5, 30],
    ["journal-timeline", 5, 0, 7, 30],
  ]);
}

export function notesSeed(): PanelInstance[] {
  return buildSeed([
    ["note-editor", 0, 0, 6, 30],
    ["note-view", 6, 0, 6, 30],
  ]);
}

export function Dashboard({
  namespace,
  seed,
  pendingAction,
  onPendingHandled,
}: {
  namespace: string;
  seed: () => PanelInstance[];
  // A command-palette action queued by App, applied once this Dashboard mounts
  // (a nav switch may have remounted it). See navigation.ts.
  pendingAction?: PendingAction;
  onPendingHandled?: () => void;
}) {
  const {
    presets,
    activeId,
    active,
    panels,
    setPanels,
    switchPreset,
    createPreset,
    renamePreset,
    deletePreset,
    resetActive,
  } = usePresets(namespace, seed);

  const [catalogOpen, setCatalogOpen] = useState(false);
  // Phase 14e: gates whether grid panels can be dragged/resized. Defaults to
  // false so a first-time visitor can't wreck the layout by dragging a panel
  // header — distinct from `editMode` below, which is the preset-name
  // text-entry UI state (new/rename). Two separate concepts, kept separately
  // named on purpose.
  const [layoutEditing, setLayoutEditing] = useState(false);

  const layout: Layout[] = panels.map((p) => ({
    i: p.i,
    x: p.x,
    y: p.y,
    w: p.w,
    h: p.h,
    minW: 2,
    minH: 3,
  }));

  const onLayoutChange = (next: Layout[]) =>
    setPanels((prev) =>
      prev.map((p) => {
        const l = next.find((n) => n.i === p.i);
        return l ? { ...p, x: l.x, y: l.y, w: l.w, h: l.h } : p;
      }),
    );

  const addWidget = (id: string, config?: unknown) => {
    const def = getWidget(id);
    if (!def) return;
    setPanels((prev) => [
      ...prev,
      {
        i: panelUid(),
        widgetId: id,
        config: config ?? def.defaultConfig,
        x: 0,
        y: Infinity, // drop at the bottom
        w: def.defaultSize.w,
        h: def.defaultSize.h,
      },
    ]);
  };

  const removePanel = (i: string) => setPanels((prev) => prev.filter((p) => p.i !== i));
  const setConfig = (i: string, config: unknown) =>
    setPanels((prev) => prev.map((p) => (p.i === i ? { ...p, config } : p)));

  // Apply a queued command-palette action (add a widget, and/or scroll a widget
  // into view). Keyed on nonce so a repeated identical action still fires.
  useEffect(() => {
    if (!pendingAction) return;
    if (pendingAction.add) addWidget(pendingAction.add.widgetId, pendingAction.add.config);
    const focusId = pendingAction.focusWidgetId;
    if (focusId) {
      const target = panels.find((p) => p.widgetId === focusId);
      if (target) {
        requestAnimationFrame(() => {
          const el = document.getElementById(`panel-${target.i}`);
          el?.scrollIntoView({ behavior: "smooth", block: "center" });
          el?.classList.add("panel-flash");
          window.setTimeout(() => el?.classList.remove("panel-flash"), 1200);
        });
      }
    }
    onPendingHandled?.();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingAction?.nonce]);

  // Inline layout-name entry (replaces the old browser-native prompt dialog —
  // ruled none of those anywhere). "new" and "rename" share one commit
  // path; they differ only in which action the trimmed name feeds.
  const [editMode, setEditMode] = useState<{ kind: "new" | "rename"; value: string } | null>(null);
  // Guards the blur-that-follows-unmount edge case: Enter/Escape resolve the
  // edit via setEditMode(null), which unmounts the focused <input>; some
  // browsers then fire a trailing blur on the way out. Reset false whenever a
  // session opens, so a later *genuine* blur-to-commit still fires correctly.
  const editResolvedRef = useRef(false);

  const startNew = () => {
    editResolvedRef.current = false;
    setEditMode({ kind: "new", value: `Layout ${presets.length + 1}` });
  };
  const startRename = () => {
    editResolvedRef.current = false;
    setEditMode({ kind: "rename", value: active.name });
  };
  const commitEdit = () => {
    editResolvedRef.current = true;
    const name = editMode?.value.trim();
    if (name) {
      if (editMode?.kind === "new") createPreset(name); // clones the current layout under the new name
      else renamePreset(activeId, name);
    }
    setEditMode(null);
  };
  const cancelEdit = () => {
    editResolvedRef.current = true;
    setEditMode(null);
  };
  // Blur commits (same as Enter) rather than discarding the typed name —
  // clicking away from an inline field reads as "done", not "abandon".
  const onEditBlur = () => {
    if (editResolvedRef.current) {
      editResolvedRef.current = false;
      return;
    }
    commitEdit();
  };
  const onEditKeyDown = (e: React.KeyboardEvent<HTMLInputElement>) => {
    if (e.key === "Enter") commitEdit();
    else if (e.key === "Escape") cancelEdit();
  };

  // Two-step inline confirm for the destructive actions (delete / reset): first
  // click arms it (label flips to a question), a second click while armed runs
  // it; losing focus or Escape disarms without acting.
  const [deleteConfirm, setDeleteConfirm] = useState(false);
  const [resetConfirm, setResetConfirm] = useState(false);
  const onDeleteClick = () => {
    if (presets.length <= 1) return;
    if (deleteConfirm) {
      deletePreset(activeId);
      setDeleteConfirm(false);
    } else {
      setDeleteConfirm(true);
    }
  };
  const onResetClick = () => {
    if (resetConfirm) {
      resetActive(seed());
      setResetConfirm(false);
    } else {
      setResetConfirm(true);
    }
  };

  return (
    <div className="grid-wrap">
      <div className="layout-bar">
        <span className="layout-bar-label">LAYOUT</span>
        <select value={activeId} onChange={(e) => switchPreset(e.target.value)} title="Switch layout preset" aria-label="layout preset">
          {presets.map((p) => (
            <option key={p.id} value={p.id}>
              {p.name}
              {p.builtin ? " ·" : ""}
            </option>
          ))}
        </select>
        {editMode?.kind === "new" ? (
          <input
            autoFocus
            aria-label="New layout name"
            value={editMode.value}
            onChange={(e) => setEditMode({ kind: "new", value: e.target.value })}
            onKeyDown={onEditKeyDown}
            onBlur={onEditBlur}
          />
        ) : (
          <button onClick={startNew} title="Create a new layout from the current one">
            + new
          </button>
        )}
        {editMode?.kind === "rename" ? (
          <input
            autoFocus
            aria-label="Rename layout"
            value={editMode.value}
            onChange={(e) => setEditMode({ kind: "rename", value: e.target.value })}
            onKeyDown={onEditKeyDown}
            onBlur={onEditBlur}
          />
        ) : (
          <button onClick={startRename}>rename</button>
        )}
        <button
          onClick={onDeleteClick}
          onBlur={() => setDeleteConfirm(false)}
          onKeyDown={(e) => e.key === "Escape" && setDeleteConfirm(false)}
          disabled={presets.length <= 1}
          title={
            presets.length <= 1
              ? "No other layouts to delete"
              : deleteConfirm
                ? "Click again to delete"
                : "Delete this layout"
          }
        >
          {deleteConfirm ? "delete?" : "delete"}
        </button>

        <span style={{ flex: 1 }} />
        <span className="layout-bar-sep" />

        <button
          onClick={() => setLayoutEditing((v) => !v)}
          aria-label={layoutEditing ? "Finish editing layout" : "Edit layout"}
          aria-pressed={layoutEditing}
          title={
            layoutEditing
              ? "Layout unlocked — drag panel headers or resize; click to lock"
              : "Unlock dragging and resizing panels"
          }
          style={layoutEditing ? { borderColor: theme.accent, color: theme.text } : undefined}
        >
          {layoutEditing ? "done" : "edit layout"}
        </button>
        <button onClick={() => setCatalogOpen(true)} title="Browse the widget catalog">
          + widget
        </button>
        <button
          onClick={onResetClick}
          onBlur={() => setResetConfirm(false)}
          onKeyDown={(e) => e.key === "Escape" && setResetConfirm(false)}
          title={resetConfirm ? "Click again to reset" : undefined}
        >
          {resetConfirm ? "sure?" : "reset"}
        </button>
      </div>
      <WidgetCatalog open={catalogOpen} onClose={() => setCatalogOpen(false)} onAdd={addWidget} />
      <Grid
        className="layout"
        layout={layout}
        cols={12}
        rowHeight={28}
        margin={[6, 6]}
        // .panel-header's `cursor: move` lives in index.css and is rendered by
        // Panel.tsx, not here — Phase 14e follow-up for whoever owns index.css:
        // gate that cursor on layoutEditing too (default to `default`, `move`
        // only while unlocked) once this file's isDraggable gate is in place.
        draggableHandle=".panel-header"
        compactType="vertical"
        isDraggable={layoutEditing}
        isResizable={layoutEditing}
        onLayoutChange={onLayoutChange}
      >
        {panels.map((p) => {
          const def = getWidget(p.widgetId);
          const Comp = def?.component;
          return (
            <div key={p.i} id={`panel-${p.i}`}>
              <Panel
                title={def?.title ?? p.widgetId}
                help={def?.help ?? WIDGET_HELP[p.widgetId]}
                onRemove={() => removePanel(p.i)}
              >
                {Comp ? (
                  <WidgetErrorBoundary widgetTitle={def?.title ?? p.widgetId}>
                    <Suspense fallback={<div className="panel-state">loading…</div>}>
                      <Comp config={p.config} onConfigChange={(c) => setConfig(p.i, c)} />
                    </Suspense>
                  </WidgetErrorBoundary>
                ) : (
                  <div className="panel-state error">Unknown widget: {p.widgetId}</div>
                )}
              </Panel>
            </div>
          );
        })}
      </Grid>
    </div>
  );
}
