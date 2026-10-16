import { useState } from "react";

import { api } from "../api/client";
import { LOCALE } from "../format";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import type { WidgetProps } from "./registry";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";

const fmt = (v: string | number, dp = 2) =>
  Number(v).toLocaleString(LOCALE, { maximumFractionDigits: dp });

function CurrencyBreakdown(_: WidgetProps<Record<string, never>>) {
  // Bumping retryKey re-runs useAsync's fetch — it's already part of the
  // hook's public contract (any deps change re-triggers the effect), so
  // Retry needs no change to useAsync.ts itself.
  const [retryKey, setRetryKey] = useState(0);
  const { data, loading, error } = useAsync(() => api.currencies(), [retryKey], 120_000);
  const rows = data ?? [];
  const max = Math.max(...rows.map((r) => Math.abs(Number(r.value))), 1);
  const total = rows.reduce((s, r) => s + Number(r.value), 0);

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        figure={rows.length ? fmt(total, 0) : undefined}
        unit={rows[0]?.reporting_currency}
        deck={deckLine("currency-breakdown")}
      />
      {/* PanelHead now renders unconditionally above — loading/error/empty
          occupy only the body area (design review
          pattern from Holdings.tsx). */}
      {loading && !data ? (
        <PanelState kind="loading" fill />
      ) : error ? (
        <PanelState kind="error" message={error} onRetry={() => setRetryKey((k) => k + 1)} fill />
      ) : !rows.length ? (
        <PanelState kind="empty" message="no balances" fill />
      ) : (
      <div tabIndex={0} style={{ flex: 1, minHeight: 0, overflowY: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ color: theme.muted, fontSize: theme.fontSize.micro }}>
              <th style={{ textAlign: "left", padding: "3px 8px", fontWeight: 400 }}>CCY</th>
              <th style={{ textAlign: "right", padding: "3px 8px", fontWeight: 400 }}>UNITS</th>
              <th style={{ textAlign: "right", padding: "3px 8px", fontWeight: 400 }}>VALUE</th>
              {/* "SHARE" implied share-of-total, matching the new total hero above,
                  but these bars are actually share-of-max (design review
                  2026-08-15). Renamed rather than reworking the bar math: the
                  max-based scale is what keeps a single bar from overflowing
                  100% when balances carry mixed signs. */}
              <th style={{ textAlign: "left", padding: "3px 8px", fontWeight: 400 }}>OF MAX</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => {
              const v = Number(r.value);
              const pct = (Math.abs(v) / max) * 100;
              return (
                <tr key={r.currency} className="kv">
                  <td className="k" style={{ padding: "2px 8px" }}>
                    {r.currency}
                  </td>
                  <td className="num" style={{ textAlign: "right", padding: "2px 8px" }}>
                    {fmt(r.units, 4)}
                  </td>
                  <td className="num" style={{ textAlign: "right", padding: "2px 8px" }}>
                    {fmt(r.value, 0)} {r.reporting_currency}
                  </td>
                  <td style={{ width: "28%", padding: "2px 8px" }}>
                    <div
                      style={{
                        height: 6,
                        width: `${pct}%`,
                        minWidth: 2,
                        borderRadius: 2,
                        background: v >= 0 ? theme.accent : theme.down,
                      }}
                    />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      )}
    </div>
  );
}

registerWidget<Record<string, never>>({
  id: "currency-breakdown",
  title: "Currencies",
  component: CurrencyBreakdown,
  defaultSize: { w: 5, h: 6 },
  minSize: { w: 3, h: 3 },
  defaultConfig: {},
});
