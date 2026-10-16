// Market Regime composite (Phase 10a). Headline regime state badge + score gauge,
// per-component diverging bars, and a score history AreaChart. Flags recession
// (Sahm rule from FRED). Keyless except recession_flag which uses cached FRED data.
import {
  Area,
  AreaChart,
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useState } from "react";

import { api } from "../api/client";
import type { RegimeComponent } from "../api/types";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import { Provenance } from "./Provenance";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";

const fmtNum = (v: number | null, dp = 2): string => (v === null ? "—" : v.toFixed(dp));

function stateColor(state: string): string {
  const s = state.toLowerCase();
  if (s.includes("risk-on") || s.includes("risk on")) return theme.up;
  if (s.includes("risk-off") || s.includes("risk off")) return theme.down;
  return theme.muted;
}

// The score itself now leads in PanelHead's figure; this badge is just the
// qualitative state read (risk-on/off/neutral) that goes with it.
function StateBadge({ state, noReading }: { state: string; noReading?: boolean }) {
  if (noReading) {
    return (
      <div style={{ display: "flex", justifyContent: "center", padding: "6px 0 4px" }}>
        <span
          style={{
            fontSize: theme.fontSize.micro,
            letterSpacing: "0.08em",
            color: theme.muted,
            border: `1px dashed ${theme.border}`,
            borderRadius: 3,
            padding: "2px 8px",
          }}
        >
          NO READING
        </span>
      </div>
    );
  }
  const color = stateColor(state);
  return (
    <div style={{ display: "flex", justifyContent: "center", padding: "6px 0 4px" }}>
      <span
        style={{
          fontSize: theme.fontSize.micro,
          fontWeight: 700,
          letterSpacing: "0.08em",
          color,
          border: `1px solid ${color}`,
          borderRadius: 3,
          padding: "2px 8px",
        }}
      >
        {state.toUpperCase()}
      </span>
    </div>
  );
}

function ComponentBar({ c }: { c: RegimeComponent }) {
  // Diverging bar: 0 is center; positive = right (green), negative = left (red).
  const val = c.score ?? 0;
  const pct = Math.min(100, Math.abs(val) * 100);
  const isPos = val >= 0;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 5, fontSize: theme.fontSize.micro, padding: "1px 0" }}>
      <span
        style={{
          width: 80,
          color: theme.muted,
          fontSize: theme.fontSize.micro,
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
        }}
      >
        {c.label}
      </span>
      {/* left (negative) side */}
      <div style={{ flex: 1, display: "flex", justifyContent: "flex-end" }}>
        {!isPos && (
          <div
            style={{
              width: `${pct}%`,
              height: 6,
              background: theme.down,
              borderRadius: "2px 0 0 2px",
            }}
          />
        )}
      </div>
      {/* center tick */}
      <div style={{ width: 1, height: 10, background: theme.muted, flexShrink: 0 }} />
      {/* right (positive) side */}
      <div style={{ flex: 1 }}>
        {isPos && (
          <div
            style={{
              width: `${pct}%`,
              height: 6,
              background: theme.up,
              borderRadius: "0 2px 2px 0",
            }}
          />
        )}
      </div>
      <span className="num" style={{ width: 36, textAlign: "right", color: c.score === null ? theme.muted : isPos ? theme.up : theme.down }}>
        {fmtNum(c.score)}
      </span>
    </div>
  );
}

function RegimeComposite() {
  // Bumping retryKey re-runs useAsync's fetch — it's already part of the
  // hook's public contract (any deps change re-triggers the effect), so
  // Retry needs no change to useAsync.ts itself.
  const [retryKey, setRetryKey] = useState(0);
  const { data: env, loading, error } = useAsync(() => api.marketRegime(), [retryKey], 3_600_000);
  if (loading && !env) return <PanelState kind="loading" />;
  if (error) return <PanelState kind="error" message={error} onRetry={() => setRetryKey((k) => k + 1)} />;
  const d = env?.data;
  if (!d) return <PanelState kind="empty" message="no data" />;
  // No component has a value → there is no verdict to show (a NEUTRAL badge
  // would read as a real reading), and null scores must not draw coloured dashes.
  const noReading = d.components.every((c) => c.score === null && c.value === null);

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        figure={!noReading && d.score !== null ? fmtNum(d.score) : undefined}
        deck={deckLine("market-regime")}
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
      />

      <div
        tabIndex={0}
        style={{
          flex: 1,
          minHeight: 0,
          overflowY: "auto",
          padding: "0 8px 6px",
          display: "flex",
          flexDirection: "column",
        }}
      >
        {/* headline state badge */}
        <StateBadge state={d.state} noReading={noReading} />

        {/* component bars */}
        {!noReading && d.components.length > 0 && (
          <div style={{ flexShrink: 0, borderTop: `1px solid ${theme.grid}`, paddingTop: 5, marginBottom: 6 }}>
            <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.05em", marginBottom: 3 }}>
              COMPONENTS
            </div>
            {d.components.map((c) => (
              <ComponentBar key={c.key} c={c} />
            ))}
          </div>
        )}

        {/* history chart — flex-grows to absorb whatever height the panel has
            left (design review
            below the footer whenever the panel was taller than the content's
            minimum; a taller trend chart is a better use of that height than
            an empty scroll void). */}
        {d.history.length > 1 && (
          <div
            style={{
              flex: 1,
              minHeight: 80,
              display: "flex",
              flexDirection: "column",
              borderTop: `1px solid ${theme.grid}`,
              paddingTop: 5,
              marginBottom: 6,
            }}
          >
            <div style={{ flexShrink: 0, fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.05em", marginBottom: 3 }}>
              SCORE HISTORY
            </div>
            <div style={{ flex: 1, minHeight: 0 }}>
              <ResponsiveContainer width="100%" height="100%">
                <AreaChart
                  data={d.history}
                  margin={{ top: 4, right: 4, bottom: 0, left: 0 }}
                >
                  <CartesianGrid stroke={theme.grid} strokeWidth={0.5} vertical={false} />
                  <ReferenceLine y={0} stroke={theme.border} strokeWidth={0.8} />
                  <XAxis
                    dataKey="date"
                    tick={false}
                    stroke={theme.border}
                    tickLine={false}
                    axisLine={false}
                  />
                  <YAxis
                    domain={[-1, 1]}
                    tick={{ fill: theme.muted, fontSize: theme.fontSize.micro }}
                    stroke={theme.border}
                    tickLine={false}
                    width={24}
                    ticks={[-1, 0, 1]}
                  />
                  <Tooltip
                    content={({ active, payload }) => {
                      if (!active || !payload?.length) return null;
                      const p = payload[0].payload as { date: string; score: number | null };
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
                            {fmtNum(p.score)}
                          </span>
                        </div>
                      );
                    }}
                    wrapperStyle={{ outline: "none" }}
                  />
                  <Area
                    type="monotone"
                    dataKey="score"
                    stroke={theme.accent}
                    strokeWidth={1.2}
                    fill={theme.accent}
                    fillOpacity={0.12}
                    dot={false}
                    isAnimationActive={false}
                  />
                </AreaChart>
              </ResponsiveContainer>
            </div>
          </div>
        )}

        {/* footer: recession flag + sahm */}
        <div
          style={{
            flexShrink: 0,
            display: "flex",
            gap: 10,
            alignItems: "center",
            borderTop: `1px solid ${theme.grid}`,
            paddingTop: 4,
            fontSize: theme.fontSize.micro,
            color: theme.muted,
          }}
        >
          {d.recession_flag && (
            <span
              style={{
                fontSize: theme.fontSize.micro,
                fontWeight: 700,
                color: theme.down,
                border: `1px solid ${theme.down}`,
                borderRadius: 3,
                padding: "1px 5px",
                letterSpacing: "0.06em",
              }}
            >
              RECESSION
            </span>
          )}
          <span>
            Sahm{" "}
            <span className="num" style={{ color: theme.text }}>
              {fmtNum(d.sahm)}
            </span>
          </span>
          <span style={{ marginLeft: "auto" }}>{d.as_of.slice(0, 10)}</span>
        </div>
      </div>
    </div>
  );
}

registerWidget({
  id: "market-regime",
  title: "Market Regime",
  component: RegimeComposite,
  defaultSize: { w: 5, h: 11 },
  minSize: { w: 3, h: 8 },
  defaultConfig: {},
  category: "Market Research",
  description: "Composite risk-on/off regime score, components, and history",
});
