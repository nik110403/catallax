import { useState } from "react";

import { api } from "../api/client";
import type { WealthfolioHoldings } from "../api/types";
import { LOCALE } from "../format";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import { controlStyle } from "./chartTheme";
import type { WidgetProps } from "./registry";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";

interface AllocConfig {
  view: "class" | "account";
}

interface Slice {
  key: string;
  value: number;
  weight: number;
}

const CLASS_COLORS: Record<string, string> = {
  crypto: theme.accent,
  equity: theme.up,
  etf: theme.chart[0],
  fx: theme.chart[4],
  cash: theme.muted,
  other: theme.chart[5],
};
const PALETTE = theme.chart;
const colorFor = (key: string, i: number) => CLASS_COLORS[key.toLowerCase()] ?? PALETTE[i % PALETTE.length];
const fmt = (v: number) => v.toLocaleString(LOCALE, { maximumFractionDigits: 0 });

// Allocation from the imported portfolio. Switchable: by asset class or
// by exporter account.
function buildSlices(data: WealthfolioHoldings | undefined, view: AllocConfig["view"]): Slice[] {
  if (!data) return [];
  const totals = new Map<string, number>();
  if (view === "class") {
    for (const a of data.by_asset) {
      const k = (a.asset_class || "other").toLowerCase();
      totals.set(k, (totals.get(k) ?? 0) + Number(a.value));
    }
  } else {
    for (const a of data.by_account) {
      if (Number(a.value) !== 0) totals.set(a.account, (totals.get(a.account) ?? 0) + Number(a.value));
    }
  }
  const sum = [...totals.values()].reduce((s, v) => s + v, 0) || 1;
  return [...totals.entries()]
    .map(([key, value]) => ({ key, value, weight: Math.round((value / sum) * 1000) / 10 }))
    .sort((a, b) => b.value - a.value);
}

function Donut({ slices }: { slices: Slice[] }) {
  // Arc lengths from exact values (not the 1-decimal display weights) so segments
  // never accumulate rounding drift / leave a seam gap with 3+ slices.
  const total = slices.reduce((s, x) => s + x.value, 0) || 1;
  let acc = 0;
  return (
    <svg viewBox="0 0 42 42" style={{ width: 100, height: 100, flex: "0 0 auto" }}>
      <circle cx="21" cy="21" r="15.91549" fill="transparent" stroke={theme.border} strokeWidth="6" />
      {slices.map((s, i) => {
        const pct = (s.value / total) * 100;
        const el = (
          <circle
            key={s.key}
            cx="21"
            cy="21"
            r="15.91549"
            fill="transparent"
            stroke={colorFor(s.key, i)}
            strokeWidth="6"
            strokeDasharray={`${pct} ${100 - pct}`}
            strokeDashoffset={25 - acc}
          />
        );
        acc += pct;
        return el;
      })}
    </svg>
  );
}

function Allocation({ config, onConfigChange }: WidgetProps<AllocConfig>) {
  const view = config.view ?? "class";
  // Bumping retryKey re-runs useAsync's fetch — it's already part of the
  // hook's public contract (any deps change re-triggers the effect), so
  // Retry needs no change to useAsync.ts itself.
  const [retryKey, setRetryKey] = useState(0);
  const { data, loading, error } = useAsync(() => api.wealthfolioHoldings(), [retryKey], 60_000);
  const slices = buildSlices(data, view);
  const cur = data?.base ?? "EUR";

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        // Top-slice weight, not the portfolio total — that's Holdings' hero,
        // same number, same tab.
        // This widget is about the split, so its own headline is the largest slice.
        figure={slices.length ? `${slices[0].weight}%` : undefined}
        unit={slices[0]?.key.toUpperCase()}
        deck={deckLine("allocation")}
      >
        <select
          value={view}
          onChange={(e) =>
            onConfigChange({ ...config, view: e.target.value as AllocConfig["view"] })
          }
          style={controlStyle}
          aria-label="allocation grouping"
        >
          <option value="class">By class</option>
          <option value="account">By account</option>
        </select>
      </PanelHead>
      {/* PanelHead now renders unconditionally above — loading/error/empty
          occupy only the body area instead of replacing the whole panel
          (design review
          as data landed; fixed at the source in Holdings.tsx). */}
      {loading && !data ? (
        <PanelState kind="loading" fill />
      ) : error ? (
        <PanelState kind="error" message={error} onRetry={() => setRetryKey((k) => k + 1)} fill />
      ) : !slices.length ? (
        <PanelState kind="empty" message="no holdings" fill />
      ) : (
        <div
          tabIndex={0}
          style={{
            display: "flex",
            gap: 10,
            padding: 10,
            alignItems: "center",
            flexWrap: "wrap",
            flex: 1,
            minHeight: 0,
            overflowY: "auto",
          }}
        >
          <Donut slices={slices} />
          <div style={{ flex: 1, minWidth: 160 }}>
            {slices.map((s, i) => (
              <div key={s.key} className="kv" style={{ borderBottom: "none" }}>
                <span style={{ color: theme.muted }}>
                  <span
                    style={{
                      display: "inline-block",
                      width: 8,
                      height: 8,
                      borderRadius: 2,
                      background: colorFor(s.key, i),
                      marginRight: 6,
                    }}
                  />
                  {s.key}
                </span>
                <span className="num" style={{ whiteSpace: "nowrap", fontSize: theme.fontSize.micro }}>
                  {s.weight}% · {fmt(s.value)} {cur}
                </span>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

registerWidget<AllocConfig>({
  id: "allocation",
  title: "Allocation",
  component: Allocation,
  defaultSize: { w: 5, h: 7 },
  minSize: { w: 3, h: 5 },
  defaultConfig: { view: "class" },
});
