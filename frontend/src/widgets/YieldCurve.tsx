// Yield curve (Phase 8, Rates). US (federal_reserve) or EU (ecb) government curve
// snapshot — rate vs maturity. Drawn with recharts (LineChart) on a log maturity
// x-axis: tenor is not a time axis, so the time-series lib (lightweight-charts) is the
// wrong fit; recharts gives real axes + a responsive container, so nothing stretches.
// Shows the 10y-2y / 10y-3m spreads and an inversion flag (recession signal). Keyless.
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
import type { CurvePoint } from "../api/types";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import { controlStyle } from "./chartTheme";
import { Provenance } from "./Provenance";
import type { WidgetProps } from "./registry";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";

interface CurveConfig {
  region: string; // US | EU
}

// recharts hands the hovered datum through payload[0].payload — our CurvePoint.
function CurveTooltip({ active, payload }: { active?: boolean; payload?: { payload: CurvePoint }[] }) {
  if (!active || !payload?.length) return null;
  const p = payload[0].payload;
  return (
    <div
      style={{
        background: theme.panel,
        border: `1px solid ${theme.border}`,
        borderRadius: 3,
        padding: "2px 6px",
        fontSize: theme.fontSize.micro,
        whiteSpace: "nowrap",
      }}
    >
      <span style={{ color: theme.muted }}>{p.maturity}</span>{" "}
      <span className="num" style={{ color: theme.accent }}>
        {p.rate.toFixed(2)}%
      </span>
    </div>
  );
}

function Curve({ points }: { points: CurvePoint[] }) {
  if (points.length < 2) return null;
  // Log x-scale on maturity (years) keeps the short end readable while proportioning
  // the long end correctly, so US (1M start) and EU (3M start) curves stay comparable.
  // Axis is labelled by tenor, not raw years: map each year back to its maturity
  // string and only tick the canonical tenors so the axis stays uncluttered.
  const labelByYears = new Map(points.map((p) => [p.years, p.maturity]));
  const tickTenors = ["3M", "6M", "1Y", "2Y", "5Y", "10Y", "30Y"];
  const ticks = points
    .filter((p, i) => i === 0 || i === points.length - 1 || tickTenors.includes(p.maturity))
    .map((p) => p.years);
  return (
    <ResponsiveContainer width="100%" height="100%">
      <LineChart data={points} margin={{ top: 8, right: 14, bottom: 2, left: 0 }}>
        <CartesianGrid stroke={theme.grid} strokeWidth={0.5} vertical={false} />
        <XAxis
          dataKey="years"
          type="number"
          scale="log"
          domain={["dataMin", "dataMax"]}
          ticks={ticks}
          tickFormatter={(v: number) => labelByYears.get(v) ?? ""}
          tick={{ fill: theme.muted, fontSize: theme.fontSize.micro }}
          stroke={theme.border}
          tickLine={false}
        />
        <YAxis
          domain={[(min: number) => min - 0.3, (max: number) => max + 0.3]}
          tickFormatter={(v: number) => v.toFixed(1)}
          tick={{ fill: theme.muted, fontSize: theme.fontSize.micro }}
          stroke={theme.border}
          tickLine={false}
          width={36}
        />
        <Tooltip
          content={<CurveTooltip />}
          cursor={{ stroke: theme.muted, strokeWidth: 0.5, strokeDasharray: "2 2" }}
          wrapperStyle={{ outline: "none" }}
        />
        <Line
          type="monotone"
          dataKey="rate"
          stroke={theme.accent}
          strokeWidth={1.5}
          dot={{ r: 1.8, fill: theme.accent, strokeWidth: 0 }}
          activeDot={{ r: 3, fill: theme.accent, strokeWidth: 0 }}
          isAnimationActive={false}
        />
      </LineChart>
    </ResponsiveContainer>
  );
}

const bps = (v: number | null) => (v === null ? "—" : `${v >= 0 ? "+" : ""}${(v * 100).toFixed(0)}bp`);

function YieldCurve({ config, onConfigChange }: WidgetProps<CurveConfig>) {
  const region = config.region ?? "US";
  const { data: env, loading, error, errorCode } = useAsync(() => api.macroYieldCurve(region), [region], 3_600_000);
  if (loading && !env) return <PanelState kind="loading" />;
  if (errorCode === "SIDECAR_NOT_ENABLED") return <PanelState kind="disabled" profile="macro" />;
  if (error) return <PanelState kind="error" message={error} />;
  const c = env?.data;

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        // Canonical hero: the 10Y-2Y spread (design review P1, 2026-08-15 —
        // this widget had none). figureColor reuses the same sign law the
        // footer already applies to this exact value, so hero and footer
        // agree instead of one being neutral and one coloured.
        figure={bps(c?.spread_10y_2y ?? null)}
        figureColor={(c?.spread_10y_2y ?? 0) < 0 ? theme.down : theme.up}
        deck={deckLine("yield-curve")}
        agate={
          <>
            {/* status → agate, controls → children (design review,
                2026-08-15: this badge was in children, RiskVaR's LOW n in
                agate — one rule now applies to both). */}
            {c?.inverted && (
              <span
                style={{
                  color: theme.down,
                  border: `1px solid ${theme.down}`,
                  borderRadius: 3,
                  padding: "1px 4px",
                  letterSpacing: "0.04em",
                }}
              >
                INVERTED
              </span>
            )}
            {env && <Provenance source={env.source} cacheHit={env.cache_hit} stale={env.stale} warnings={env.warnings} fetchedAt={env.fetched_at} />}
          </>
        }
      >
        {/* bold region tag so the two YIELD CURVE panels read apart at a glance */}
        <span style={{ fontWeight: 700, color: theme.accent, fontSize: theme.fontSize.micro, letterSpacing: "0.04em" }}>
          {region === "EU" ? "EU" : "US"}
        </span>
        <select
          value={region}
          onChange={(e) => onConfigChange({ ...config, region: e.target.value })}
          style={controlStyle}
          aria-label="yield curve region"
        >
          <option value="US">US Treasury</option>
          <option value="EU">Euro Area</option>
        </select>
      </PanelHead>
      <div style={{ flex: 1, minHeight: 0, padding: 4 }}>{c && <Curve key={region} points={c.points} />}</div>
      <div
        style={{
          display: "flex",
          gap: 14,
          padding: "4px 8px",
          borderTop: `1px solid ${theme.border}`,
          fontSize: theme.fontSize.micro,
          color: theme.muted,
        }}
      >
        <span>
          10Y–2Y{" "}
          <span className="num" style={{ color: (c?.spread_10y_2y ?? 0) < 0 ? theme.down : theme.up }}>
            {bps(c?.spread_10y_2y ?? null)}
          </span>
        </span>
        <span>
          10Y–3M{" "}
          <span className="num" style={{ color: (c?.spread_10y_3m ?? 0) < 0 ? theme.down : theme.up }}>
            {bps(c?.spread_10y_3m ?? null)}
          </span>
        </span>
        {c?.date && <span style={{ marginLeft: "auto" }}>{c.date}</span>}
      </div>
    </div>
  );
}

registerWidget<CurveConfig>({
  id: "yield-curve",
  title: "Yield Curve",
  component: YieldCurve,
  defaultSize: { w: 5, h: 7 },
  minSize: { w: 3, h: 5 },
  defaultConfig: { region: "US" },
  category: "Rates",
  description: "US / Euro-area government yield curve + inversion flag",
});
