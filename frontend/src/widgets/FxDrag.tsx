// FX drag (Phase 8.5). Splits the EUR portfolio return into its asset-local
// component and the currency (FX) component an EUR investor bears on USD/GBP/CHF
// holdings — log-additive (r_eur = r_local - r_fx). By currency + per asset.
import { useState } from "react";

import { api } from "../api/client";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import { Provenance } from "./Provenance";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";

const sign = (v: number | null | undefined, dp = 2) =>
  v === null || v === undefined ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(dp)}%`;
const col = (v: number | null | undefined) => ((v ?? 0) > 0 ? theme.up : (v ?? 0) < 0 ? theme.down : theme.muted);

function FxDrag() {
  // Bumping retryKey re-runs useAsync's fetch — it's already part of the
  // hook's public contract (any deps change re-triggers the effect), so
  // Retry needs no change to useAsync.ts itself.
  const [retryKey, setRetryKey] = useState(0);
  const { data: env, loading, error } = useAsync(() => api.riskFxDrag(), [retryKey], 3_600_000);
  if (loading && !env) return <PanelState kind="loading" message="computing FX drag…" />;
  if (error) return <PanelState kind="error" message={error} onRetry={() => setRetryKey((k) => k + 1)} />;
  const f = env?.data;
  if (!f) return <PanelState kind="empty" message="no data" />;

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        // No hero figure: the body decomposition three lines down is the
        // widget's actual headline (EUR RETURN = LOCAL + FX DRAG) and can't be
        // reduced to one number without breaking the equation it's making —
        // duplicating just the FX DRAG term here also rendered it neutral
        // while the body colours it up/down (design review P1, 2026-08-15:
        // FxDrag.tsx:27 vs :41, "same number, two colours").
        deck={deckLine("fx-drag")}
        agate={
          <>
            <span style={{ letterSpacing: "0.04em" }}>{f.lookback_days}D LOOKBACK</span>
            {env && <Provenance source={env.source} stale={env.stale} warnings={env.warnings} fetchedAt={env.fetched_at} />}
          </>
        }
      />
      <div tabIndex={0} style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "6px 8px" }}>
        {/* headline decomposition */}
        <div style={{ display: "flex", gap: 12, marginBottom: 6 }}>
          <div><div style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>EUR RETURN</div><div className="num" style={{ fontSize: theme.fontSize.lg, fontWeight: 700, color: col(f.portfolio_eur_return_pct) }}>{sign(f.portfolio_eur_return_pct)}</div></div>
          <div style={{ alignSelf: "center", color: theme.text, fontSize: theme.fontSize.base }}>=</div>
          <div><div style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>LOCAL</div><div className="num" style={{ fontSize: theme.fontSize.lg, color: col(f.portfolio_local_return_pct) }}>{sign(f.portfolio_local_return_pct)}</div></div>
          <div style={{ alignSelf: "center", color: theme.text, fontSize: theme.fontSize.base }}>+</div>
          <div><div style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>FX DRAG</div><div className="num" style={{ fontSize: theme.fontSize.lg, fontWeight: 700, color: col(f.portfolio_fx_drag_pct) }}>{sign(f.portfolio_fx_drag_pct)}</div></div>
        </div>

        {/* by currency */}
        <div style={{ borderTop: `1px solid ${theme.grid}`, paddingTop: 4 }}>
          <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, marginBottom: 2 }}>FX DRAG BY CURRENCY</div>
          {f.by_currency.map((b) => (
            <div key={b.currency} style={{ display: "flex", justifyContent: "space-between", fontSize: theme.fontSize.micro, padding: "1px 0" }}>
              <span>{b.currency} <span style={{ color: theme.muted, fontSize: theme.fontSize.micro }}>{b.weight_pct.toFixed(0)}%</span></span>
              <span className="num" style={{ color: col(b.fx_drag_pct) }}>{sign(b.fx_drag_pct)}</span>
            </div>
          ))}
        </div>

        {/* per-asset top movers */}
        <div style={{ borderTop: `1px solid ${theme.grid}`, paddingTop: 4, marginTop: 4 }}>
          <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, marginBottom: 2 }}>TOP FX MOVERS (local → EUR)</div>
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: theme.fontSize.micro }}>
            <thead><tr style={{ color: theme.muted, fontSize: theme.fontSize.micro }}>
              <th style={{ textAlign: "left", fontWeight: 400 }}>SYM</th><th style={{ textAlign: "right", fontWeight: 400 }}>LOCAL</th>
              <th style={{ textAlign: "right", fontWeight: 400 }}>EUR</th><th style={{ textAlign: "right", fontWeight: 400 }}>FX</th>
            </tr></thead>
            <tbody>
              {f.by_asset.filter((a) => a.currency !== "EUR").slice(0, 8).map((a) => (
                <tr key={a.symbol}>
                  <td title={`${a.currency} · ${a.currency_source}`}>{a.symbol}<span style={{ color: theme.muted, fontSize: theme.fontSize.micro }}> {a.currency}</span></td>
                  <td className="num" style={{ textAlign: "right", color: col(a.local_return_pct) }}>{sign(a.local_return_pct, 1)}</td>
                  <td className="num" style={{ textAlign: "right", color: col(a.eur_return_pct) }}>{sign(a.eur_return_pct, 1)}</td>
                  <td className="num" style={{ textAlign: "right", color: col(a.fx_contrib_pct) }}>{sign(a.fx_contrib_pct, 1)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, marginTop: 5, lineHeight: 1.4 }} title={f.assumption}>
          Listing-currency basis · EUR holdings bear no FX drag.
        </div>
      </div>
    </div>
  );
}

registerWidget({
  id: "fx-drag",
  title: "FX Drag",
  component: FxDrag,
  defaultSize: { w: 5, h: 9 },
  minSize: { w: 3, h: 6 },
  defaultConfig: {},
  category: "Risk",
  description: "EUR vs asset-local return split — the currency drag on the book",
});
