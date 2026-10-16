// Valuation (Phase 10a). Tiles for each ValuationMetric: label, value,
// percentile bar (0-100). Metrics that need_source render a muted
// "pending v1.1" placeholder with the note text.
import { useState } from "react";

import { api } from "../api/client";
import type { ValuationMetric } from "../api/types";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import { Provenance } from "./Provenance";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";

const fmtNum = (v: number | null, dp = 1): string => (v === null ? "—" : v.toFixed(dp));

/** 0-100 percentile bar. Color: high = red (expensive), low = green (cheap). */
function PctBar({ pct }: { pct: number | null }) {
  if (pct === null) {
    return <div style={{ height: 4, background: theme.grid, borderRadius: 2 }} />;
  }
  const w = Math.max(0, Math.min(100, pct));
  // Above 75th = expensive (red), below 25th = cheap (green), else neutral.
  const color = w >= 75 ? theme.down : w <= 25 ? theme.up : theme.accent;
  return (
    <div style={{ height: 4, background: theme.grid, borderRadius: 2, overflow: "hidden" }}>
      <div style={{ width: `${w}%`, height: "100%", background: color, borderRadius: 2 }} />
    </div>
  );
}

function MetricTile({ m }: { m: ValuationMetric }) {
  if (m.needs_source) {
    return (
      <div
        style={{
          background: theme.panelHeader,
          border: `1px solid ${theme.border}`,
          borderRadius: 4,
          padding: "6px 8px",
          display: "flex",
          flexDirection: "column",
          gap: 3,
          opacity: 0.55,
        }}
      >
        <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.05em" }}>
          {m.label.toUpperCase()}
        </div>
        <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, fontStyle: "italic" }}>
          pending v1.1
        </div>
        {m.note && (
          <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, marginTop: 1, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }} title={m.note}>{m.note}</div>
        )}
      </div>
    );
  }

  return (
    <div
      style={{
        background: theme.panelHeader,
        border: `1px solid ${theme.border}`,
        borderRadius: 4,
        padding: "6px 8px",
        display: "flex",
        flexDirection: "column",
        gap: 3,
      }}
    >
      {/* label */}
      <div
        style={{
          fontSize: theme.fontSize.micro,
          color: theme.muted,
          letterSpacing: "0.05em",
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
        }}
      >
        {m.label.toUpperCase()}
      </div>

      {/* value */}
      <span className="num" style={{ fontSize: theme.fontSize.lg, fontWeight: 700, color: theme.text, lineHeight: 1 }}>
        {fmtNum(m.value)}
      </span>

      {/* percentile bar */}
      <PctBar pct={m.percentile} />

      {/* percentile label + note */}
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          fontSize: theme.fontSize.micro,
          color: theme.muted,
        }}
      >
        <span>
          {m.percentile !== null ? `${m.percentile.toFixed(0)}th pct` : "—"}
        </span>
      </div>
      {m.note && (
        <div style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>{m.note}</div>
      )}
    </div>
  );
}

function ValuationGauge() {
  // Bumping retryKey re-runs useAsync's fetch — it's already part of the
  // hook's public contract (any deps change re-triggers the effect), so
  // Retry needs no change to useAsync.ts itself.
  const [retryKey, setRetryKey] = useState(0);
  const { data: env, loading, error } = useAsync(() => api.marketValuation(), [retryKey], 3_600_000);
  if (loading && !env) return <PanelState kind="loading" />;
  if (error) return <PanelState kind="error" message={error} onRetry={() => setRetryKey((k) => k + 1)} />;
  const d = env?.data;
  if (!d) return <PanelState kind="empty" message="no data" />;

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        deck={deckLine("market-valuation")}
        agate={
          <>
            <span className="num">as of {d.as_of.slice(0, 10)}</span>
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

      <div tabIndex={0} style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "6px 8px" }}>
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(auto-fill, minmax(130px, 1fr))",
            gap: 6,
          }}
        >
          {d.metrics.map((m) => (
            <MetricTile key={m.key} m={m} />
          ))}
        </div>
      </div>
    </div>
  );
}

registerWidget({
  id: "market-valuation",
  title: "Market Valuation",
  component: ValuationGauge,
  defaultSize: { w: 5, h: 8 },
  minSize: { w: 3, h: 6 },
  defaultConfig: {},
  category: "Market Research",
  description: "Equity valuation metrics (CAPE, P/E, Buffett indicator, real 10y yield) with historical percentile bars",
});
