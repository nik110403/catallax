// Financial Conditions (Phase 10a). Responsive tile grid — one tile per
// ConditionsTile item. Each tile shows label, value+unit, a centred z-score
// mini-bar, percentile, and a trend arrow. Color follows risk direction.
import { useState } from "react";

import { api } from "../api/client";
import type { ConditionsTile } from "../api/types";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import { Provenance } from "./Provenance";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";

const fmtNum = (v: number | null, dp = 2): string => (v === null ? "—" : v.toFixed(dp));

/** Tile accent color: risk-off direction means high z → red, else green. */
function tileAccent(tile: ConditionsTile): string {
  if (tile.zscore === null) return theme.muted;
  const riskSignal = tile.higher_is_risk_off ? tile.zscore > 0 : tile.zscore < 0;
  return riskSignal ? theme.down : tile.zscore === 0 ? theme.muted : theme.up;
}

function trendArrow(trend: string): string {
  const t = trend.toLowerCase();
  if (t === "up" || t === "rising") return "↑";
  if (t === "down" || t === "falling") return "↓";
  return "→";
}

function trendColor(trend: string, higher_is_risk_off: boolean): string {
  const t = trend.toLowerCase();
  const isUp = t === "up" || t === "rising";
  const isDown = t === "down" || t === "falling";
  if (isUp) return higher_is_risk_off ? theme.down : theme.up;
  if (isDown) return higher_is_risk_off ? theme.up : theme.down;
  return theme.muted;
}

/** Centred z-score bar spanning from −3..+3, zero at centre.
 * Color follows risk direction: if higher_is_risk_off, a positive z is risk →
 * red; if higher_is_risk_off=false, a positive z is good → green. */
function ZBar({ zscore, higher_is_risk_off }: { zscore: number | null; higher_is_risk_off: boolean }) {
  if (zscore === null) {
    return <div style={{ height: 4, background: theme.grid, borderRadius: 2 }} />;
  }
  const clamped = Math.max(-3, Math.min(3, zscore));
  const pct = Math.abs(clamped) / 3; // 0..1
  const isPos = clamped >= 0;
  // Risk direction: a high value is "bad" (red) when higher_is_risk_off, so the bar
  // is red exactly when isPos agrees with higher_is_risk_off (matches tileAccent).
  const color = (isPos === higher_is_risk_off) ? theme.down : theme.up;

  return (
    <div
      style={{
        height: 4,
        background: theme.grid,
        borderRadius: 2,
        position: "relative",
        overflow: "hidden",
      }}
    >
      {/* filled half starts at centre */}
      <div
        style={{
          position: "absolute",
          top: 0,
          bottom: 0,
          width: `${pct * 50}%`,
          ...(isPos ? { left: "50%" } : { right: "50%" }),
          background: color,
          borderRadius: 2,
        }}
      />
      {/* centre tick */}
      <div
        style={{
          position: "absolute",
          top: 0,
          bottom: 0,
          left: "50%",
          width: 1,
          background: theme.border,
        }}
      />
    </div>
  );
}

function Tile({ tile }: { tile: ConditionsTile }) {
  const accent = tileAccent(tile);
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
        minWidth: 0,
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
        {tile.label.toUpperCase()}
      </div>

      {/* value + unit + trend arrow */}
      <div style={{ display: "flex", alignItems: "baseline", gap: 4 }}>
        <span className="num" style={{ fontSize: theme.fontSize.lg, fontWeight: 700, color: accent }}>
          {fmtNum(tile.value)}
        </span>
        {tile.unit && (
          <span style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>{tile.unit}</span>
        )}
        <span
          style={{
            marginLeft: "auto",
            fontSize: theme.fontSize.sm,
            color: trendColor(tile.trend, tile.higher_is_risk_off),
          }}
        >
          {trendArrow(tile.trend)}
        </span>
      </div>

      {/* z-score bar */}
      <ZBar zscore={tile.zscore} higher_is_risk_off={tile.higher_is_risk_off} />

      {/* percentile + date footer */}
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          fontSize: theme.fontSize.micro,
          color: theme.muted,
        }}
      >
        <span>
          {tile.percentile !== null ? `${tile.percentile.toFixed(0)}th pct` : "—"}
        </span>
        {tile.date && <span>{tile.date}</span>}
      </div>
    </div>
  );
}

function MarketConditions() {
  // Bumping retryKey re-runs useAsync's fetch — it's already part of the
  // hook's public contract (any deps change re-triggers the effect), so
  // Retry needs no change to useAsync.ts itself.
  const [retryKey, setRetryKey] = useState(0);
  const { data: env, loading, error } = useAsync(() => api.marketConditions(), [retryKey], 3_600_000);
  if (loading && !env) return <PanelState kind="loading" />;
  if (error) return <PanelState kind="error" message={error} onRetry={() => setRetryKey((k) => k + 1)} />;
  const d = env?.data;
  if (!d) return <PanelState kind="empty" message="no data" />;

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        deck={deckLine("market-conditions")}
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
          {d.tiles.map((tile) => (
            <Tile key={tile.key} tile={tile} />
          ))}
        </div>
      </div>
    </div>
  );
}

registerWidget({
  id: "market-conditions",
  title: "Financial Conditions",
  component: MarketConditions,
  defaultSize: { w: 7, h: 9 },
  minSize: { w: 4, h: 6 },
  defaultConfig: {},
  category: "Market Research",
  description: "Financial conditions tile grid: value, z-score bar, percentile, and trend per indicator",
});
