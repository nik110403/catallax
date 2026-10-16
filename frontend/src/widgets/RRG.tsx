// Relative Rotation Graph (Phase 10a). recharts ScatterChart: x=rs_ratio,
// y=rs_momentum. Reference lines at 100/100 split 4 quadrants. Each series is
// drawn as a faint polyline tail ending in a labeled dot, colored by group.
import {
  CartesianGrid,
  ReferenceLine,
  ResponsiveContainer,
  Scatter,
  ScatterChart,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useState } from "react";

import { api } from "../api/client";
import type { RRGSeries, RRGTailPoint } from "../api/types";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import { controlStyle } from "./chartTheme";
import { Provenance } from "./Provenance";
import type { WidgetProps } from "./registry";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";

interface RRGConfig {
  group: string; // "all" | "sector" | "factor"
}

// A small palette for groups (cycles if more groups than colors).
const GROUP_COLORS = theme.chart;

function groupColor(group: string, allGroups: string[]): string {
  const i = allGroups.indexOf(group);
  return GROUP_COLORS[i % GROUP_COLORS.length] ?? theme.muted;
}

// Quadrant label positions (placed just inside each quadrant corner).
const QUADRANT_LABELS = [
  { label: "LEADING", x: "right", y: "top" },
  { label: "WEAKENING", x: "right", y: "bottom" },
  { label: "LAGGING", x: "left", y: "bottom" },
  { label: "IMPROVING", x: "left", y: "top" },
] as const;

// Tight, symmetric domain around 100 so the cluster fills the plot (RRG values
// hug 100 when sector dispersion is low — a wide domain wastes 2/3 of the space).
// Symmetric keeps the 100/100 crosshair centred; clamped to [85,115] so a
// degenerate backend value can never collapse the chart.
function domain(values: number[], center = 100, pad = 1.5): [number, number] {
  if (!values.length) return [center - 6, center + 6];
  const reach = Math.max(...values.map((v) => Math.abs(v - center)), 2.5) + pad;
  const r = Math.min(reach, 15); // never exceed ±15 of centre
  return [center - r, center + r];
}

// Custom dot that renders the symbol label.
function LabeledDot(props: {
  cx?: number;
  cy?: number;
  payload?: { symbol: string; color: string };
}) {
  const { cx, cy, payload } = props;
  if (cx === undefined || cy === undefined || !payload) return null;
  return (
    <g>
      <circle cx={cx} cy={cy} r={4} fill={payload.color} stroke="none" />
      <text
        x={cx + 6}
        y={cy + 3}
        fontSize={8}
        fill={payload.color}
        style={{ fontFamily: theme.fontMono }}
      >
        {payload.symbol}
      </text>
    </g>
  );
}

// Each series' tail rendered as an SVG polyline on top of the chart.
// We render tails as a separate SVG overlay — recharts doesn't expose a
// "connect dots with a line" for scatter; we draw it via a customized layer.
function TailLines({
  series,
  allGroups,
  xScale,
  yScale,
}: {
  series: RRGSeries[];
  allGroups: string[];
  xScale: (v: number) => number;
  yScale: (v: number) => number;
}) {
  return (
    <>
      {series.map((s) => {
        const pts = s.tail.filter(
          (p): p is RRGTailPoint & { rs_ratio: number; rs_momentum: number } =>
            p.rs_ratio !== null && p.rs_momentum !== null,
        );
        if (pts.length < 2) return null;
        const color = groupColor(s.group, allGroups);
        const points = pts.map((p) => `${xScale(p.rs_ratio)},${yScale(p.rs_momentum)}`).join(" ");
        return (
          <polyline
            key={s.symbol}
            points={points}
            fill="none"
            stroke={color}
            strokeWidth={1}
            strokeOpacity={0.35}
          />
        );
      })}
    </>
  );
}

function RRG({ config, onConfigChange }: WidgetProps<RRGConfig>) {
  const groupFilter = config.group ?? "all";
  // Bumping retryKey re-runs useAsync's fetch — it's already part of the
  // hook's public contract (any deps change re-triggers the effect), so
  // Retry needs no change to useAsync.ts itself.
  const [retryKey, setRetryKey] = useState(0);
  const { data: env, loading, error } = useAsync(() => api.marketRrg(), [retryKey], 3_600_000);
  if (loading && !env) return <PanelState kind="loading" />;
  if (error) return <PanelState kind="error" message={error} onRetry={() => setRetryKey((k) => k + 1)} />;
  const d = env?.data;
  if (!d) return <PanelState kind="empty" message="no data" />;

  // Group colors stay stable across the filter (computed over ALL series).
  const allGroups: string[] = [];
  for (const s of d.series) {
    if (!allGroups.includes(s.group)) allGroups.push(s.group);
  }
  // Optional sector/factor filter (declutters the plot — RRG is busy with all 18).
  const series = groupFilter === "all" ? d.series : d.series.filter((s) => s.group === groupFilter);

  // Build tip data for the latest (last) tail point per series.
  const tipPoints = series
    .map((s) => {
      const last = s.tail.at(-1);
      if (!last || last.rs_ratio === null || last.rs_momentum === null) return null;
      return {
        symbol: s.symbol,
        label: s.label,
        group: s.group,
        color: groupColor(s.group, allGroups),
        rs_ratio: last.rs_ratio,
        rs_momentum: last.rs_momentum,
      };
    })
    .filter((p): p is NonNullable<typeof p> => p !== null);

  // Gather all tail values to compute domains.
  const allX = series.flatMap((s) => s.tail.map((p) => p.rs_ratio).filter((v): v is number => v !== null));
  const allY = series.flatMap((s) => s.tail.map((p) => p.rs_momentum).filter((v): v is number => v !== null));
  const xDom = domain(allX);
  const yDom = domain(allY);

  // Map chart value → pixel. recharts exposes this internally but not easily —
  // we approximate linearly from domain to the fixed inner chart dimensions.
  // The chart margin is {top:8, right:16, bottom:20, left:32}; the outer
  // container is 100% so we can't know px at render time. Instead we render
  // tails as recharts Scatter series (invisible) + overlay via customShape so
  // that recharts handles the scaling.

  // Simpler approach: render each series as a Scatter but use a custom shape
  // that draws the polyline internally through the provided (cx,cy) sequence.
  // recharts passes one customized `shape` call per data point; we draw
  // individual dots there and handle the line via an SVG rendered into the
  // chart via a <Customized> component — but recharts' <Customized> receives
  // the full xAxis/yAxis scale fns. We use that.

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        deck={deckLine("market-rrg")}
        agate={
          <>
            <span style={{ whiteSpace: "nowrap" }} className="num">
              {d.tail_weeks}W · vs {d.benchmark}
            </span>
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
      >
        <select
          value={groupFilter}
          onChange={(e) => onConfigChange({ ...config, group: e.target.value })}
          style={controlStyle}
          aria-label="rotation group filter"
        >
          <option value="all">All</option>
          <option value="sector">Sectors</option>
          <option value="factor">Factors</option>
        </select>
      </PanelHead>

      <div style={{ flex: 1, minHeight: 0, padding: "4px 4px 0" }}>
        <ResponsiveContainer width="100%" height="100%">
          <ScatterChart margin={{ top: 16, right: 20, bottom: 20, left: 10 }}>
            <CartesianGrid stroke={theme.grid} strokeWidth={0.5} />
            <XAxis
              type="number"
              dataKey="rs_ratio"
              domain={xDom}
              name="RS Ratio"
              tickFormatter={(v: number) => v.toFixed(0)}
              allowDecimals={false}
              tick={{ fill: theme.muted, fontSize: theme.fontSize.micro }}
              stroke={theme.border}
              tickLine={false}
              label={{ value: "RS Ratio →", position: "insideBottomRight", fontSize: theme.fontSize.micro, fill: theme.muted, offset: -4 }}
            />
            <YAxis
              type="number"
              dataKey="rs_momentum"
              domain={yDom}
              name="RS Momentum"
              tickFormatter={(v: number) => v.toFixed(0)}
              allowDecimals={false}
              tick={{ fill: theme.muted, fontSize: theme.fontSize.micro }}
              stroke={theme.border}
              tickLine={false}
              width={30}
              label={{ value: "RS Momentum", angle: -90, position: "insideLeft", fontSize: theme.fontSize.micro, fill: theme.muted }}
            />
            {/* quadrant dividers */}
            <ReferenceLine x={100} stroke={theme.border} strokeWidth={1} />
            <ReferenceLine y={100} stroke={theme.border} strokeWidth={1} />

            {/* quadrant labels via reference lines at domain edges */}
            <ReferenceLine
              x={xDom[1]}
              y={yDom[1]}
              label={{ value: "LEADING", position: "insideTopRight", fontSize: theme.fontSize.micro, fill: theme.up }}
              stroke="none"
            />
            <ReferenceLine
              x={xDom[1]}
              y={yDom[0]}
              label={{ value: "WEAKENING", position: "insideBottomRight", fontSize: theme.fontSize.micro, fill: theme.down }}
              stroke="none"
            />
            <ReferenceLine
              x={xDom[0]}
              y={yDom[0]}
              label={{ value: "LAGGING", position: "insideBottomLeft", fontSize: theme.fontSize.micro, fill: theme.muted }}
              stroke="none"
            />
            <ReferenceLine
              x={xDom[0]}
              y={yDom[1]}
              label={{ value: "IMPROVING", position: "insideTopLeft", fontSize: theme.fontSize.micro, fill: theme.muted }}
              stroke="none"
            />

            <Tooltip
              cursor={{ strokeDasharray: "2 2", stroke: theme.muted }}
              content={({ active, payload }) => {
                if (!active || !payload?.length) return null;
                const p = payload[0].payload as (typeof tipPoints)[0];
                return (
                  <div
                    style={{
                      background: theme.panel,
                      border: `1px solid ${theme.border}`,
                      borderRadius: 3,
                      padding: "3px 7px",
                      fontSize: theme.fontSize.micro,
                    }}
                  >
                    <div style={{ color: p.color, fontWeight: 600 }}>{p.symbol}</div>
                    <div style={{ color: theme.muted }}>{p.label}</div>
                    <div className="num">
                      RS {p.rs_ratio.toFixed(2)} / Mom {p.rs_momentum.toFixed(2)}
                    </div>
                  </div>
                );
              }}
              wrapperStyle={{ outline: "none" }}
            />

            {/* Render tails: one Scatter per series as invisible dots, plus a
                custom shape that draws the polyline. We iterate series and
                create a Scatter for each so recharts handles coordinate mapping.
                The tail polyline is drawn via customShape on the first point. */}
            {series.map((s) => {
              const color = groupColor(s.group, allGroups);
              const pts = s.tail
                .filter(
                  (p): p is RRGTailPoint & { rs_ratio: number; rs_momentum: number } =>
                    p.rs_ratio !== null && p.rs_momentum !== null,
                )
                .map((p, i, arr) => ({
                  rs_ratio: p.rs_ratio,
                  rs_momentum: p.rs_momentum,
                  symbol: s.symbol,
                  label: s.label,
                  group: s.group,
                  color,
                  isLast: i === arr.length - 1,
                }));
              if (!pts.length) return null;
              return (
                <Scatter
                  key={s.symbol}
                  data={pts}
                  fill={color}
                  shape={(shapeProps: {
                    cx?: number;
                    cy?: number;
                    payload?: (typeof pts)[0];
                  }) => {
                    const { cx, cy, payload } = shapeProps;
                    if (cx === undefined || cy === undefined || !payload) return <g />;
                    if (payload.isLast) {
                      return (
                        <g>
                          <circle cx={cx} cy={cy} r={3.5} fill={payload.color} stroke={theme.panel} strokeWidth={0.5} />
                          <text
                            x={cx + 5}
                            y={cy + 3}
                            fontSize={8}
                            fill={payload.color}
                            style={{ fontFamily: theme.fontMono }}
                          >
                            {payload.symbol}
                          </text>
                        </g>
                      );
                    }
                    // Historical points draw no marker — the joint line is the tail;
                    // only the head (latest) carries a labeled dot (standard RRG).
                    return <g />;
                  }}
                  line={{ stroke: color, strokeWidth: 1, strokeOpacity: 0.35 }}
                  lineType="joint"
                  isAnimationActive={false}
                />
              );
            })}

            {/* tip points invisible scatter for tooltip */}
            <Scatter
              data={tipPoints}
              fill="transparent"
              shape={<LabeledDot />}
              isAnimationActive={false}
            />
          </ScatterChart>
        </ResponsiveContainer>
      </div>

      {/* group legend */}
      {allGroups.length > 0 && (
        <div
          style={{
            display: "flex",
            gap: 10,
            flexWrap: "wrap",
            padding: "3px 8px",
            borderTop: `1px solid ${theme.grid}`,
            fontSize: theme.fontSize.micro,
          }}
        >
          {allGroups.map((g) => (
            <span key={g} style={{ color: groupColor(g, allGroups), display: "flex", alignItems: "center", gap: 3 }}>
              <span
                style={{
                  width: 6,
                  height: 6,
                  borderRadius: "50%",
                  background: groupColor(g, allGroups),
                  flexShrink: 0,
                }}
              />
              {g}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

registerWidget<RRGConfig>({
  id: "market-rrg",
  title: "Relative Rotation (RRG)",
  component: RRG,
  defaultSize: { w: 6, h: 14 },
  minSize: { w: 4, h: 8 },
  defaultConfig: { group: "all" },
  category: "Market Research",
  description: "Relative Rotation Graph: RS-ratio vs RS-momentum with tail history per sector/factor",
});
