// Stress test (Phase 8.5). Applies static, historically-calibrated crash-window
// shocks (GFC / COVID / Crypto-Winter / Rates) to the CURRENT portfolio weights,
// per asset class + an FX overlay. Proxy shocks (not a per-asset historical replay)
// so it works despite shallow crypto/ETF history — labelled accordingly.
import { useState } from "react";

import { api } from "../api/client";
import { LOCALE } from "../format";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import { Provenance } from "./Provenance";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";

const eur = (v: number) => `€${Math.round(v).toLocaleString(LOCALE)}`;
const sign = (v: number) => `${v >= 0 ? "+" : ""}${v.toFixed(1)}%`;

function StressTest() {
  // Bumping retryKey re-runs useAsync's fetch — it's already part of the
  // hook's public contract (any deps change re-triggers the effect), so
  // Retry needs no change to useAsync.ts itself.
  const [retryKey, setRetryKey] = useState(0);
  const { data: env, loading, error } = useAsync(() => api.riskStress(), [retryKey], 3_600_000);
  if (loading && !env) return <PanelState kind="loading" />;
  if (error) return <PanelState kind="error" message={error} onRetry={() => setRetryKey((k) => k + 1)} />;
  const s = env?.data;
  if (!s || !s.scenarios.length) return <PanelState kind="empty" message="stress testing disabled" />;
  const worst = s.scenarios.reduce((w, sc) => (sc.portfolio_return_pct < w.portfolio_return_pct ? sc : w), s.scenarios[0]);

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        // No hero figure: the worst scenario's return already has a cell in
        // the scenario table below, one row among several — pulling just that
        // row's number up top would leave its own table cell looking blank
        // next to every other populated row (design review P1, 2026-08-15:
        // StressTest.tsx:28 = the table row at :57). The value stays visible
        // here as agate context instead of a duplicate hero.
        deck={deckLine("stress-test")}
        agate={
          <>
            <span style={{ letterSpacing: "0.04em" }}>
              WORST: {worst.name.toUpperCase()} {sign(worst.portfolio_return_pct)}
            </span>
            {env && <Provenance source={env.source} stale={env.stale} warnings={env.warnings} fetchedAt={env.fetched_at} />}
          </>
        }
      />
      <div tabIndex={0} style={{ flex: 1, minHeight: 0, overflowY: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse", fontSize: theme.fontSize.micro }}>
          <thead>
            <tr style={{ color: theme.muted, fontSize: theme.fontSize.micro }}>
              <th style={{ textAlign: "left", padding: "3px 8px", fontWeight: 400 }}>SCENARIO</th>
              <th style={{ textAlign: "right", padding: "3px 8px", fontWeight: 400 }}>RETURN</th>
              <th style={{ textAlign: "right", padding: "3px 8px", fontWeight: 400 }}>P&amp;L</th>
              <th style={{ textAlign: "left", padding: "3px 8px", fontWeight: 400 }}>WORST</th>
            </tr>
          </thead>
          <tbody>
            {s.scenarios.map((sc) => (
              <tr key={sc.name} style={{ borderBottom: `1px solid ${theme.grid}` }}>
                <td style={{ padding: "4px 8px", fontWeight: 600 }}>
                  {sc.name}
                  {sc.synthetic_classes.length > 0 && (
                    <span title={`proxy (synthetic) for: ${sc.synthetic_classes.join(", ")}`} style={{ color: theme.warn, fontSize: theme.fontSize.micro }}> ⚠</span>
                  )}
                </td>
                <td className="num" style={{ textAlign: "right", padding: "4px 8px", color: theme.down, fontWeight: 700 }}>
                  {sign(sc.portfolio_return_pct)}
                </td>
                <td className="num" style={{ textAlign: "right", padding: "4px 8px", color: theme.down }}>
                  {eur(sc.portfolio_pnl_eur)}
                </td>
                <td style={{ padding: "4px 8px", color: theme.muted, fontSize: theme.fontSize.micro }}>
                  {sc.worst.slice(0, 2).map((w) => w.key).join(", ")}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, padding: "5px 8px", lineHeight: 1.4 }} title={s.assumption}>
          Asset-class proxy shocks on current weights. ⚠ = class didn't exist in the window.
        </div>
      </div>
    </div>
  );
}

registerWidget({
  id: "stress-test",
  title: "Stress Test",
  component: StressTest,
  defaultSize: { w: 5, h: 7 },
  minSize: { w: 3, h: 5 },
  defaultConfig: {},
  category: "Risk",
  description: "GFC / COVID / crypto-winter shocks applied to current weights",
});
