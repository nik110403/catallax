// Macro indicator heatmap. Multi-country grid of headline indicators
// (CPI / Core / PPI YoY, Unemployment level, Real-GDP YoY) from econdb (keyless),
// latest value per country, colour-coded with econ-meaningful thresholds. Rows =
// indicators, columns = countries.
import { api } from "../api/client";
import type { IndicatorCell } from "../api/types";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import { Provenance } from "./Provenance";
import type { WidgetProps } from "./registry";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";

// Indicator row order (by econdb root).
const ROOT_ORDER = ["CPI", "CORE", "PPI", "URATE", "RGDP", "GDP"];

// Colour by indicator semantics. Values arrive as fractions → pct here.
function cellStyle(root: string, pct: number): React.CSSProperties {
  let tone = 0; // -1 bad/red, +1 good/green, 0 neutral
  if (root === "RGDP" || root === "GDP") tone = pct > 0 ? 1 : -1;
  else if (root === "URATE") tone = pct > 6 ? -1 : pct < 4.5 ? 1 : 0;
  else {
    // inflation (CPI/CORE/PPI): near a ~2% target is healthy; hot = red, deflation = blue
    if (pct > 3) tone = -1;
    else if (pct >= 1 && pct <= 3) tone = 1;
    else tone = 0;
  }
  const bg =
    tone > 0 ? "rgba(63,185,80,0.16)" : tone < 0 ? "rgba(248,81,73,0.16)" : "transparent";
  const color = tone > 0 ? theme.up : tone < 0 ? theme.down : theme.text;
  return { background: bg, color };
}

function MacroHeatmap() {
  const { data: env, loading, error, errorCode } = useAsync(() => api.macroIndicators(), [], 3_600_000);
  if (loading && !env) return <PanelState kind="loading" />;

  const board = env?.data;
  const countries = board?.countries ?? [];
  const cells = board?.cells ?? [];
  const hasData = countries.length > 0;

  // index: root -> country -> cell
  const byRoot = new Map<string, { label: string; transform: string; cells: Map<string, IndicatorCell> }>();
  for (const c of cells) {
    let row = byRoot.get(c.root);
    if (!row) {
      row = { label: c.label, transform: c.transform, cells: new Map() };
      byRoot.set(c.root, row);
    }
    row.cells.set(c.country, c);
  }
  const roots = [...byRoot.keys()].sort(
    (a, b) => (ROOT_ORDER.indexOf(a) + 1 || 99) - (ROOT_ORDER.indexOf(b) + 1 || 99),
  );

  const th: React.CSSProperties = {
    padding: "3px 6px",
    color: theme.muted,
    fontSize: theme.fontSize.micro,
    fontWeight: 400,
    textAlign: "right",
    whiteSpace: "nowrap",
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        deck={deckLine("macro-heatmap")}
        agate={
          <>
            <span style={{ letterSpacing: "0.06em" }}>INDICATORS · YoY</span>
            {env && <Provenance source={env.source} cacheHit={env.cache_hit} stale={env.stale} warnings={env.warnings} fetchedAt={env.fetched_at} />}
          </>
        }
      />
      {/* keep the table mounted (roots/byRoot are computed unconditionally above)
          but hidden whenever a state should own the body instead — including the
          disabled-service case, where `env` never resolves a body and a naive
          `!countries.length` check alone would still let this empty grid render
          above the disabled message. */}
      <div tabIndex={0} style={{ flex: 1, minHeight: 0, overflow: "auto", display: !error && hasData ? "block" : "none" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: theme.fontSize.micro }}>
          <thead>
            <tr>
              <th style={{ ...th, textAlign: "left", position: "sticky", left: 0, background: theme.panel }} />
              {countries.map((c) => (
                <th key={c} style={th} title={c}>
                  {c.replace("Euro area", "Euro").replace("United States", "US").replace("United Kingdom", "UK").slice(0, 8)}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {roots.map((root) => {
              const row = byRoot.get(root)!;
              const yoy = row.transform === "toya";
              return (
                <tr key={root}>
                  <td
                    style={{
                      padding: "3px 6px",
                      fontWeight: 600,
                      whiteSpace: "nowrap",
                      position: "sticky",
                      left: 0,
                      background: theme.panel,
                    }}
                  >
                    {row.label}
                    <span style={{ color: theme.muted, fontSize: theme.fontSize.micro }}>{yoy ? " %y" : " %"}</span>
                  </td>
                  {countries.map((c) => {
                    const cell = row.cells.get(c);
                    if (!cell || cell.value === null) {
                      return (
                        <td key={c} style={{ padding: "3px 6px", textAlign: "right", color: theme.muted }}>
                          —
                        </td>
                      );
                    }
                    const pct = cell.value * 100;
                    // Guard a known econdb units quirk: some countries report the
                    // unemployment rate already in percent (so ×100 yields a
                    // non-physical 400%). Suppress impossible rates rather than
                    // display a wrong figure on the board.
                    if (!Number.isFinite(pct) || (root === "URATE" && (pct < 0 || pct > 40))) {
                      return (
                        <td
                          key={c}
                          title={`source value out of range (${pct.toFixed(1)}%)`}
                          style={{ padding: "3px 6px", textAlign: "right", color: theme.muted }}
                        >
                          —
                        </td>
                      );
                    }
                    return (
                      <td
                        key={c}
                        className="num"
                        title={cell.date ?? ""}
                        style={{ padding: "3px 6px", textAlign: "right", ...cellStyle(root, pct) }}
                      >
                        {pct.toFixed(1)}
                      </td>
                    );
                  })}
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      {errorCode === "SIDECAR_NOT_ENABLED" && <PanelState kind="disabled" profile="macro" fill />}
      {error && errorCode !== "SIDECAR_NOT_ENABLED" && <PanelState kind="error" message={error} fill />}
      {!error && !hasData && (
        <PanelState kind="empty" message="no indicator data" hint={env?.warnings?.[0]} fill />
      )}
    </div>
  );
}

registerWidget({
  id: "macro-heatmap",
  title: "Indicator Heatmap",
  component: MacroHeatmap,
  defaultSize: { w: 6, h: 8 },
  minSize: { w: 4, h: 5 },
  defaultConfig: {},
  category: "Macro",
  description: "Multi-country CPI / PPI / unemployment / GDP grid (YoY)",
});
