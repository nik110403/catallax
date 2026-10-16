// Value-at-Risk (Phase 8.5). Historical-simulation VaR/CVaR @95% (and 99%) on the
// current portfolio's weighted EUR P&L distribution, with a Gaussian-VaR comparison
// that exposes the crypto fat tail (hist > Gaussian for a fat-tailed book). 1-day.
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

const eur = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : `€${Math.abs(v).toLocaleString(LOCALE, { maximumFractionDigits: 0 })}`;
const pct = (v: number | null | undefined, dp = 2) =>
  v === null || v === undefined ? "—" : `${Math.abs(v).toFixed(dp)}%`;

function Row({
  label,
  eurVal,
  pctVal,
  strong,
  hideEur,
}: {
  label: string;
  eurVal: number | null;
  pctVal?: number | null;
  strong?: boolean;
  // The VaR row's EUR figure duplicates the PanelHead hero directly above it
  //.
  // hideEur drops the redundant number and promotes the row's own pctVal —
  // information the hero can't carry — into its place.
  hideEur?: boolean;
}) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "baseline", padding: "3px 0", borderBottom: `1px solid ${theme.grid}` }}>
      <span style={{ fontSize: strong ? 12 : 11, color: strong ? theme.text : theme.muted }}>{label}</span>
      <span style={{ display: "flex", gap: 8, alignItems: "baseline" }}>
        {!hideEur && (
          <span className="num" style={{ color: theme.down, fontWeight: strong ? 700 : 400, fontSize: strong ? 14 : 12 }}>{eur(eurVal)}</span>
        )}
        {pctVal !== undefined && (
          <span
            className="num"
            style={{
              color: hideEur ? theme.down : theme.muted,
              fontWeight: hideEur && strong ? 700 : 400,
              fontSize: hideEur && strong ? 14 : theme.fontSize.micro,
              width: hideEur ? undefined : 44,
              textAlign: "right",
            }}
          >
            {pct(pctVal)}
          </span>
        )}
      </span>
    </div>
  );
}

function RiskVaR() {
  // Bumping retryKey re-runs useAsync's fetch — it's already part of the
  // hook's public contract (any deps change re-triggers the effect), so
  // Retry needs no change to useAsync.ts itself.
  const [retryKey, setRetryKey] = useState(0);
  const { data: env, loading, error } = useAsync(() => api.riskVar(), [retryKey], 3_600_000);
  if (loading && !env) return <PanelState kind="loading" message="computing VaR…" />;
  if (error) return <PanelState kind="error" message={error} onRetry={() => setRetryKey((k) => k + 1)} />;
  const v = env?.data;
  if (!v) return <PanelState kind="empty" message="no data" />;

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        figure={v.var_eur != null ? eur(v.var_eur) : undefined}
        deck={deckLine("risk-var")}
        agate={
          <>
            <span style={{ letterSpacing: "0.04em" }}>1-DAY · {(v.confidence * 100).toFixed(0)}%</span>
            {v.low_confidence && (
              <span title={`only ${v.n_scenarios} scenarios`} style={{ color: theme.warn, border: `1px solid ${theme.warn}`, borderRadius: 3, padding: "1px 4px" }}>
                LOW n
              </span>
            )}
            {env && <Provenance source={env.source} stale={env.stale} warnings={env.warnings} fetchedAt={env.fetched_at} />}
          </>
        }
      />
      <div tabIndex={0} style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "4px 10px" }}>
        {/* the 95% lives in the panel subtitle — keep the rows terse. eurVal
            is hidden here: it's the PanelHead hero above, so this row now
            surfaces var_pct instead of repeating it. */}
        <Row label="VaR" eurVal={v.var_eur} pctVal={v.var_pct} strong hideEur />
        <Row label="CVaR / shortfall" eurVal={v.cvar_eur} pctVal={v.cvar_pct} />
        <Row label="VaR 99%" eurVal={v.var99_eur} />
        <Row label="CVaR 99%" eurVal={v.cvar99_eur} />
        <Row label="Gaussian VaR 95%" eurVal={v.gaussian_var_eur} />
        <div style={{ display: "flex", justifyContent: "space-between", padding: "5px 0", fontSize: theme.fontSize.micro, color: theme.muted }}>
          <span>fat-tail (CVaR/VaR)</span>
          <span className="num" style={{ color: theme.text }}>{v.fat_tail_ratio?.toFixed(2) ?? "—"}×</span>
        </div>
        <div style={{ display: "flex", justifyContent: "space-between", fontSize: theme.fontSize.micro, color: theme.muted }}>
          <span>hist vs Gaussian</span>
          <span className="num" style={{ color: (v.hist_vs_gaussian ?? 1) > 1.1 ? theme.down : theme.text }}>
            {v.hist_vs_gaussian?.toFixed(2) ?? "—"}×
          </span>
        </div>
        <div style={{ display: "flex", justifyContent: "space-between", fontSize: theme.fontSize.micro, color: theme.muted, marginTop: 4 }}>
          <span>worst day</span>
          <span className="num">{v.worst_day ?? "—"} · {pct(v.worst_day_pct)}</span>
        </div>
        <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, marginTop: 6, lineHeight: 1.4 }}>
          Empirical 1-day loss on {v.n_scenarios} historical scenarios at current weights ({v.coverage.covered_weight_pct.toFixed(0)}% of value).
        </div>
      </div>
    </div>
  );
}

registerWidget({
  id: "risk-var",
  title: "Value at Risk",
  component: RiskVaR,
  defaultSize: { w: 5, h: 7 },
  minSize: { w: 3, h: 5 },
  defaultConfig: {},
  category: "Risk",
  description: "Historical-simulation VaR / CVaR with a fat-tail (Gaussian) comparison",
});
