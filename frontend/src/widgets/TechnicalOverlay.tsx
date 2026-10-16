// Technical Overlay (Phase 10b). Price + SMA50 + SMA200 line chart (recharts)
// with stat strip: Last, vs 200-DMA (dist_sma200_pct), 52w range + position bar,
// RSI-14, and trend badge. Symbol configurable. Registered lazily in index.ts.
import { useEffect, useState } from "react";
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { api } from "../api/client";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import { Provenance } from "./Provenance";
import type { WidgetProps } from "./registry";
import { useAsync } from "./useAsync";

interface Config {
  symbol: string;
}

const symInputStyle = {
  background: theme.bg,
  color: theme.text,
  border: `1px solid ${theme.border}`,
  borderRadius: 4,
  padding: "2px 5px",
  font: "inherit",
  fontSize: theme.fontSize.micro,
};

const SMA50_COLOR = theme.chart[0];
const SMA200_COLOR = theme.chart[4];

export function TechnicalOverlay({ config, onConfigChange }: WidgetProps<Config>) {
  const [symInput, setSymInput] = useState(config.symbol);
  useEffect(() => setSymInput(config.symbol), [config.symbol]);

  // Bumping retryKey re-runs useAsync's fetch — it's already part of the
  // hook's public contract (any deps change re-triggers the effect), so
  // Retry needs no change to useAsync.ts itself.
  const [retryKey, setRetryKey] = useState(0);
  const { data: env, loading, error } = useAsync(
    () => api.researchTechnicals(config.symbol),
    [config.symbol, retryKey],
  );

  const d = env?.data;

  const distColor =
    d?.dist_sma200_pct == null
      ? theme.muted
      : d.dist_sma200_pct >= 0
        ? theme.up
        : theme.down;

  const rsiColor =
    d?.rsi_14 == null
      ? theme.muted
      : d.rsi_14 > 70
        ? theme.down
        : d.rsi_14 < 30
          ? theme.up
          : theme.muted;

  const trendColor =
    d?.trend === "uptrend" ? theme.up : d?.trend === "downtrend" ? theme.down : theme.muted;

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        deck={deckLine("research-technicals")}
        agate={
          env && (
            <Provenance
              source={env.source}
              cacheHit={env.cache_hit}
              stale={env.stale}
              warnings={env.warnings}
              fetchedAt={env.fetched_at}
            />
          )
        }
      >
        <form
          style={{ display: "flex", alignItems: "center" }}
          onSubmit={(e) => {
            e.preventDefault();
            const sym = symInput.trim().toUpperCase();
            if (sym) onConfigChange({ ...config, symbol: sym });
          }}
        >
          <input
            value={symInput}
            onChange={(e) => setSymInput(e.target.value)}
            style={{ ...symInputStyle, width: 70 }}
            aria-label="symbol"
          />
        </form>
      </PanelHead>

      {loading && !d && <PanelState kind="loading" />}
      {error && <PanelState kind="error" message={error} onRetry={() => setRetryKey((k) => k + 1)} />}
      {d && (
        <div style={{ flex: 1, minHeight: 0, display: "flex", flexDirection: "column" }}>
          {/* recharts line chart */}
          <div style={{ flex: 1, minHeight: 0 }}>
            <ResponsiveContainer width="100%" height="100%">
              <LineChart
                data={d.series}
                margin={{ top: 4, right: 8, bottom: 4, left: 2 }}
              >
                <CartesianGrid stroke={theme.grid} strokeDasharray="3 3" />
                <XAxis
                  dataKey="date"
                  tick={{ fontSize: theme.fontSize.micro, fill: theme.muted }}
                  tickFormatter={(s: string) => s.slice(0, 7)}
                  interval="preserveStartEnd"
                />
                <YAxis
                  tick={{ fontSize: theme.fontSize.micro, fill: theme.muted }}
                  tickFormatter={(v: number) => v.toFixed(0)}
                  width={42}
                  domain={["auto", "auto"]}
                />
                <Tooltip
                  contentStyle={{
                    background: theme.panel,
                    border: `1px solid ${theme.border}`,
                    fontSize: theme.fontSize.micro,
                    color: theme.text,
                  }}
                  labelStyle={{ color: theme.muted }}
                  itemStyle={{ color: theme.text }}
                />
                <Line
                  type="monotone"
                  dataKey="close"
                  stroke={theme.accent}
                  strokeWidth={1.5}
                  dot={false}
                  isAnimationActive={false}
                  name="Close"
                />
                <Line
                  type="monotone"
                  dataKey="sma50"
                  stroke={SMA50_COLOR}
                  strokeWidth={1}
                  dot={false}
                  isAnimationActive={false}
                  name="SMA 50"
                  connectNulls
                />
                <Line
                  type="monotone"
                  dataKey="sma200"
                  stroke={SMA200_COLOR}
                  strokeWidth={1}
                  dot={false}
                  isAnimationActive={false}
                  name="SMA 200"
                  connectNulls
                />
              </LineChart>
            </ResponsiveContainer>
          </div>

          {/* stat strip */}
          <div
            style={{
              padding: "5px 8px",
              borderTop: `1px solid ${theme.border}`,
              display: "flex",
              flexWrap: "wrap",
              gap: 12,
              alignItems: "center",
            }}
          >
            {/* Last */}
            <div>
              <span style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>Last </span>
              <span className="num" style={{ fontSize: theme.fontSize.micro, color: theme.text }}>
                {d.last !== null ? d.last.toFixed(2) : "—"}
              </span>
            </div>

            {/* vs 200-DMA */}
            <div>
              <span style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>vs 200-DMA </span>
              <span className="num" style={{ fontSize: theme.fontSize.micro, color: distColor }}>
                {d.dist_sma200_pct !== null
                  ? `${d.dist_sma200_pct >= 0 ? "+" : ""}${d.dist_sma200_pct.toFixed(1)}%`
                  : "—"}
              </span>
            </div>

            {/* 52w range + position bar */}
            <div style={{ minWidth: 90 }}>
              <div style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>
                <span className="num">
                  {d.low_52w !== null ? d.low_52w.toFixed(0) : "—"}
                </span>
                <span style={{ margin: "0 3px" }}>—</span>
                <span className="num">
                  {d.high_52w !== null ? d.high_52w.toFixed(0) : "—"}
                </span>
              </div>
              {d.pct_of_52w_range !== null && (
                <div
                  style={{
                    marginTop: 2,
                    height: 3,
                    background: theme.grid,
                    borderRadius: 2,
                    overflow: "hidden",
                  }}
                >
                  <div
                    style={{
                      width: `${Math.max(0, Math.min(100, d.pct_of_52w_range))}%`,
                      height: "100%",
                      background: theme.accent,
                      borderRadius: 2,
                    }}
                  />
                </div>
              )}
            </div>

            {/* RSI-14 */}
            <div>
              <span style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>RSI-14 </span>
              <span className="num" style={{ fontSize: theme.fontSize.micro, color: rsiColor }}>
                {d.rsi_14 !== null ? d.rsi_14.toFixed(1) : "—"}
              </span>
            </div>

            {/* trend badge */}
            <div
              style={{
                fontSize: theme.fontSize.micro,
                color: trendColor,
                padding: "1px 5px",
                border: `1px solid ${trendColor}`,
                borderRadius: 3,
                textTransform: "uppercase",
                letterSpacing: "0.04em",
                whiteSpace: "nowrap",
              }}
            >
              {d.trend}
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
