// Volatility Regime (Phase 10a). Big VIX number + percentile, term structure
// state badge, realized vol, implied-minus-realized, MOVE index, and a VIX
// history LineChart.
import {
  CartesianGrid,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useState } from "react";

import { api } from "../api/client";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import { Provenance } from "./Provenance";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";

const fmtNum = (v: number | null, dp = 1): string => (v === null ? "—" : v.toFixed(dp));
const fmtPct = (v: number | null, dp = 1): string =>
  v === null ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(dp)}%`;

function termStateColor(state: string): string {
  const s = state.toLowerCase();
  if (s === "contango") return theme.up;
  if (s === "backwardation") return theme.down;
  return theme.muted;
}

function StatLine({
  label,
  value,
  color,
}: {
  label: string;
  value: string;
  color?: string;
}) {
  return (
    <div
      style={{
        display: "flex",
        justifyContent: "space-between",
        alignItems: "baseline",
        fontSize: theme.fontSize.micro,
        padding: "2px 0",
      }}
    >
      <span style={{ color: theme.muted }}>{label}</span>
      <span className="num" style={{ color: color ?? theme.text }}>
        {value}
      </span>
    </div>
  );
}

function VolRegime() {
  // Bumping retryKey re-runs useAsync's fetch — it's already part of the
  // hook's public contract (any deps change re-triggers the effect), so
  // Retry needs no change to useAsync.ts itself.
  const [retryKey, setRetryKey] = useState(0);
  const { data: env, loading, error } = useAsync(() => api.marketVolatility(), [retryKey], 3_600_000);
  if (loading && !env) return <PanelState kind="loading" />;
  if (error) return <PanelState kind="error" message={error} onRetry={() => setRetryKey((k) => k + 1)} />;
  const d = env?.data;
  if (!d) return <PanelState kind="empty" message="no data" />;

  const tsColor = termStateColor(d.term_state);
  // VIX color: elevated (>25) = red, moderate (>18) = amber, low = muted.
  const vixColor =
    (d.vix ?? 0) > 25 ? theme.down : (d.vix ?? 0) > 18 ? theme.warn : theme.text;

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        figure={d.vix !== null ? fmtNum(d.vix) : undefined}
        deck={deckLine("market-volatility")}
        agate={
          <>
            {d.vix_percentile !== null && (
              <span className="num" style={{ color: vixColor }}>
                {d.vix_percentile.toFixed(0)}th pctile
              </span>
            )}
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
        {/* term structure badge */}
        <div
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 8,
            marginBottom: 8,
            padding: "3px 8px",
            border: `1px solid ${tsColor}`,
            borderRadius: 3,
          }}
        >
          <span
            style={{
              fontSize: theme.fontSize.micro,
              fontWeight: 700,
              color: tsColor,
              letterSpacing: "0.06em",
            }}
          >
            {d.term_state.toUpperCase()}
          </span>
          <span className="num" style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>
            VIX3M {fmtNum(d.vix3m)} · ratio {fmtNum(d.term_ratio)}
          </span>
        </div>

        {/* stat lines */}
        <div style={{ borderTop: `1px solid ${theme.grid}`, paddingTop: 5, marginBottom: 6 }}>
          <StatLine label="Realized Vol" value={fmtPct(d.realized_vol_pct)} />
          <StatLine
            label="Implied − Realized"
            value={fmtNum(d.implied_minus_realized)}
            color={(d.implied_minus_realized ?? 0) > 0 ? theme.down : theme.up}
          />
          <StatLine label="MOVE (bonds)" value={fmtNum(d.move)} />
        </div>

        {/* VIX history */}
        {d.vix_history.length > 1 && (
          <div style={{ borderTop: `1px solid ${theme.grid}`, paddingTop: 5 }}>
            <div
              style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.05em", marginBottom: 3 }}
            >
              VIX HISTORY
            </div>
            <div style={{ height: 80 }}>
              <ResponsiveContainer width="100%" height="100%">
                <LineChart
                  data={d.vix_history}
                  margin={{ top: 4, right: 4, bottom: 0, left: 0 }}
                >
                  <CartesianGrid stroke={theme.grid} strokeWidth={0.5} vertical={false} />
                  <ReferenceLine y={20} stroke={theme.muted} strokeDasharray="2 2" strokeWidth={0.8} />
                  <XAxis dataKey="date" tick={false} stroke={theme.border} axisLine={false} tickLine={false} />
                  <YAxis
                    tick={{ fill: theme.muted, fontSize: theme.fontSize.micro }}
                    stroke={theme.border}
                    tickLine={false}
                    width={24}
                  />
                  <Tooltip
                    content={({ active, payload }) => {
                      if (!active || !payload?.length) return null;
                      const p = payload[0].payload as { date: string; value: number | null };
                      return (
                        <div
                          style={{
                            background: theme.panel,
                            border: `1px solid ${theme.border}`,
                            borderRadius: 3,
                            padding: "2px 6px",
                            fontSize: theme.fontSize.micro,
                          }}
                        >
                          <span style={{ color: theme.muted }}>{p.date}</span>{" "}
                          <span className="num" style={{ color: theme.accent }}>
                            {fmtNum(p.value)}
                          </span>
                        </div>
                      );
                    }}
                    wrapperStyle={{ outline: "none" }}
                  />
                  <Line
                    type="monotone"
                    dataKey="value"
                    stroke={theme.accent}
                    strokeWidth={1.2}
                    dot={false}
                    isAnimationActive={false}
                  />
                </LineChart>
              </ResponsiveContainer>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}

registerWidget({
  id: "market-volatility",
  title: "Volatility Regime",
  component: VolRegime,
  defaultSize: { w: 5, h: 9 },
  minSize: { w: 3, h: 7 },
  defaultConfig: {},
  category: "Market Research",
  description: "VIX level + percentile, term structure state, realized vol, MOVE index, and VIX history",
});
