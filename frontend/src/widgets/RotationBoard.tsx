// Sector & Factor Rotation board (Phase 10a). Table of return heat cells per
// symbol: 1D/1W/1M/3M/6M/YTD plus RS-vs-benchmark (3m). Grouped by sector/factor.
import { Fragment, useState } from "react";

import { api } from "../api/client";
import type { RotationRow } from "../api/types";
import { NOT_IN_SNAPSHOT, useDemoMode } from "../demo";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import { Provenance } from "./Provenance";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";

const fmtPct = (v: number | null, dp = 1): string =>
  v === null ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(dp)}%`;

/**
 * Heat-color a return cell. Intensity scales with magnitude (cap at ±10% for
 * full saturation). Returns CSS background + foreground.
 */
function heatStyle(v: number | null): React.CSSProperties {
  if (v === null) return { color: theme.muted };
  const mag = Math.min(Math.abs(v) / 10, 1); // 0..1
  const alpha = 0.08 + mag * 0.3;
  if (v >= 0) {
    return {
      background: `rgba(63,185,80,${alpha})`,
      color: theme.up,
    };
  }
  return {
    background: `rgba(248,81,73,${alpha})`,
    color: theme.down,
  };
}

function RetCell({ v }: { v: number | null }) {
  return (
    <td
      className="num"
      style={{
        padding: "3px 5px",
        textAlign: "right",
        fontSize: theme.fontSize.micro,
        whiteSpace: "nowrap",
        ...heatStyle(v),
      }}
    >
      {fmtPct(v)}
    </td>
  );
}

// Group label separator row
function GroupRow({ label }: { label: string }) {
  return (
    <tr>
      <td
        colSpan={9}
        style={{
          padding: "4px 6px 2px",
          fontSize: theme.fontSize.micro,
          fontWeight: 600,
          color: theme.accent,
          letterSpacing: "0.06em",
          borderTop: `1px solid ${theme.grid}`,
        }}
      >
        {label.toUpperCase()}
      </td>
    </tr>
  );
}

function RotationBoard() {
  const demo = useDemoMode();
  // Bumping retryKey re-runs useAsync's fetch — it's already part of the
  // hook's public contract (any deps change re-triggers the effect), so
  // Retry needs no change to useAsync.ts itself.
  const [retryKey, setRetryKey] = useState(0);
  const { data: env, loading, error } = useAsync(() => api.marketRotation(), [retryKey], 3_600_000);
  if (loading && !env) return <PanelState kind="loading" />;
  if (error) return <PanelState kind="error" message={error} onRetry={() => setRetryKey((k) => k + 1)} />;
  const d = env?.data;
  if (!d) return <PanelState kind="empty" message="no data" />;

  // Group rows by the `group` field, preserving order of first occurrence.
  const groups: string[] = [];
  const byGroup = new Map<string, RotationRow[]>();
  for (const r of d.rows) {
    // Demo: only rows the snapshot actually has data for.
    if (demo && [r.ret_1d, r.ret_1w, r.ret_1m, r.ret_3m, r.ret_6m, r.ret_ytd, r.rs_vs_bench_3m].every((v) => v === null)) continue;
    if (!byGroup.has(r.group)) {
      groups.push(r.group);
      byGroup.set(r.group, []);
    }
    byGroup.get(r.group)!.push(r);
  }

  if (demo && groups.length === 0) return <PanelState kind="empty" message={NOT_IN_SNAPSHOT} fill />;

  const th: React.CSSProperties = {
    padding: "3px 5px",
    color: theme.muted,
    fontSize: theme.fontSize.micro,
    fontWeight: 400,
    textAlign: "right",
    whiteSpace: "nowrap",
    position: "sticky",
    top: 0,
    background: theme.panelHeader,
    zIndex: 1,
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        deck={deckLine("market-rotation")}
        agate={
          <>
            <span style={{ letterSpacing: "0.06em" }}>VS {d.benchmark}</span>
            {env && (
              <Provenance
                source={env.source}
                cacheHit={env.cache_hit}
                stale={env.stale}
                warnings={env.warnings}
                fetchedAt={env.fetched_at}
              />
            )}
          </>
        }
      />

      <div tabIndex={0} style={{ flex: 1, minHeight: 0, overflowY: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr>
              <th style={{ ...th, textAlign: "left", position: "sticky", left: 0, zIndex: 2 }}>
                Symbol
              </th>
              <th style={th}>1D</th>
              <th style={th}>1W</th>
              <th style={th}>1M</th>
              <th style={th}>3M</th>
              <th style={th}>6M</th>
              <th style={th}>YTD</th>
              <th style={{ ...th, color: theme.accent }}>RS 3M</th>
            </tr>
          </thead>
          <tbody>
            {groups.map((g) => (
              <Fragment key={g}>
                <GroupRow label={g} />
                {byGroup.get(g)!.map((r) => (
                  <tr key={r.symbol}>
                    <td
                      style={{
                        padding: "3px 6px",
                        fontSize: theme.fontSize.micro,
                        whiteSpace: "nowrap",
                        position: "sticky",
                        left: 0,
                        background: theme.panel,
                      }}
                    >
                      <span style={{ color: theme.text }}>{r.symbol}</span>
                      <span
                        style={{
                          color: theme.muted,
                          fontSize: theme.fontSize.micro,
                          marginLeft: 4,
                          display: "block",
                          lineHeight: 1,
                        }}
                      >
                        {r.label}
                      </span>
                    </td>
                    <RetCell v={r.ret_1d} />
                    <RetCell v={r.ret_1w} />
                    <RetCell v={r.ret_1m} />
                    <RetCell v={r.ret_3m} />
                    <RetCell v={r.ret_6m} />
                    <RetCell v={r.ret_ytd} />
                    <RetCell v={r.rs_vs_bench_3m} />
                  </tr>
                ))}
              </Fragment>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

registerWidget({
  id: "market-rotation",
  title: "Sector & Factor Rotation",
  component: RotationBoard,
  defaultSize: { w: 7, h: 9 },
  minSize: { w: 4, h: 6 },
  defaultConfig: {},
  category: "Market Research",
  description: "Heatmap-coloured return table (1D–YTD) for sectors and factors, with RS vs benchmark",
});
