import { useState } from "react";

import { api } from "../api/client";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import type { WidgetProps } from "./registry";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";

// Market risk composite: a single risk-on/off read derived from the
// indices group's average % change, plus advance/decline breadth.
function MarketComposite(_props: WidgetProps<Record<string, never>>) {
  // Bumping retryKey re-runs useAsync's fetch — it's already part of the
  // hook's public contract (any deps change re-triggers the effect), so
  // Retry needs no change to useAsync.ts itself.
  const [retryKey, setRetryKey] = useState(0);
  const { data, loading, error } = useAsync(() => api.marketsOverview(), [retryKey], 60_000);
  if (loading && !data) return <PanelState kind="loading" />;
  if (error) return <PanelState kind="error" message={error} onRetry={() => setRetryKey((k) => k + 1)} />;
  const c = data?.composite;
  if (!c || !c.total) return <PanelState kind="empty" message="no data" />;

  const col = c.score > 0 ? theme.up : c.score < 0 ? theme.down : theme.muted;
  // Map score (~ -3..+3 %) onto a 0–100 needle position, clamped.
  const pos = Math.max(0, Math.min(100, 50 + c.score * 16.6));
  const advPct = (c.advancers / c.total) * 100;

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        figure={`${c.score > 0 ? "+" : ""}${c.score.toFixed(2)}`}
        unit="%"
        deck={deckLine("market-composite")}
        agate={
          <span style={{ color: col, fontWeight: 700, letterSpacing: "0.06em" }}>{c.label.toUpperCase()}</span>
        }
      />
      <div
        tabIndex={0}
        style={{
          flex: 1,
          minHeight: 0,
          overflowY: "auto",
          padding: 12,
          display: "flex",
          flexDirection: "column",
          gap: 12,
        }}
      >
        {/* Risk-off ↔ risk-on track with a needle at the composite score. */}
        <div>
          <div
            style={{
              position: "relative",
              height: 8,
              borderRadius: 4,
              background: `linear-gradient(90deg, ${theme.down}, ${theme.muted}, ${theme.up})`,
            }}
          >
            <div
              style={{
                position: "absolute",
                left: `${pos}%`,
                top: -3,
                width: 3,
                height: 14,
                background: theme.text,
                transform: "translateX(-50%)",
                borderRadius: 2,
              }}
            />
          </div>
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              fontSize: theme.fontSize.micro,
              color: theme.muted,
              marginTop: 3,
              letterSpacing: "0.04em",
            }}
          >
            <span>RISK-OFF</span>
            <span>NEUTRAL</span>
            <span>RISK-ON</span>
          </div>
        </div>

        {/* Advance / decline breadth across the indices group. */}
        <div>
          <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, marginBottom: 3 }}>
            BREADTH · {c.advancers}↑ / {c.decliners}↓ of {c.total}
          </div>
          <div style={{ display: "flex", height: 10, borderRadius: 2, overflow: "hidden" }}>
            <div style={{ width: `${advPct}%`, background: theme.up }} />
            <div style={{ flex: 1, background: theme.down }} />
          </div>
        </div>
      </div>
    </div>
  );
}

registerWidget<Record<string, never>>({
  id: "market-composite",
  title: "Risk Composite",
  component: MarketComposite,
  defaultSize: { w: 4, h: 5 },
  minSize: { w: 3, h: 4 },
  defaultConfig: {},
});
