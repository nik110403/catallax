// Risk X-ray (Phase 8.5). Headline portfolio risk computed over the real
// imported portfolio from F0 daily history: annualised vol, composite beta, max
// drawdown, concentration (HHI / top-N), risk contributors, and exposure by
// asset-class / currency / geography. All EUR. Coverage + assumptions surfaced.
import { useState } from "react";

import { api } from "../api/client";
import type { RiskBucket, RiskCoverage } from "../api/types";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import { Provenance } from "./Provenance";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";

const pct = (v: number | null | undefined, dp = 1) =>
  v === null || v === undefined ? "—" : `${v.toFixed(dp)}%`;
const num = (v: number | null | undefined, dp = 2) =>
  v === null || v === undefined ? "—" : v.toFixed(dp);

function Stat({ label, value, sub, color }: { label: string; value: string; sub?: string; color?: string }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", minWidth: 0 }}>
      <span style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.05em" }}>{label}</span>
      <span className="num" style={{ fontSize: theme.fontSize.lg, fontWeight: 700, color: color ?? theme.text }}>{value}</span>
      {sub && <span style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>{sub}</span>}
    </div>
  );
}

const BAR_COLORS: Record<string, string> = {
  CRYPTO: theme.accent, EQUITY: theme.chart[0], CASH: theme.up,
  USD: theme.chart[0], EUR: theme.up, GBP: theme.accent, CHF: theme.chart[3],
  US: theme.chart[0], EU: theme.up, UK: theme.accent, CH: theme.chart[3], OTHER: theme.muted,
};

function BarRow({ label, weight, color }: { label: string; weight: number; color: string }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: theme.fontSize.micro, padding: "1px 0" }}>
      <span style={{ width: 56, color: theme.muted, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>{label}</span>
      <div style={{ flex: 1, height: 8, background: theme.bg, borderRadius: 2, overflow: "hidden" }}>
        <div style={{ width: `${Math.max(0, Math.min(100, weight))}%`, height: "100%", background: color }} />
      </div>
      <span className="num" style={{ width: 42, textAlign: "right" }}>{pct(weight)}</span>
    </div>
  );
}

function Bars({ title, items }: { title: string; items: RiskBucket[] }) {
  return (
    <div style={{ flex: 1, minWidth: 130 }}>
      <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.05em", marginBottom: 2 }}>{title}</div>
      {items.map((b) => (
        <BarRow key={b.bucket} label={b.bucket} weight={b.weight_pct} color={BAR_COLORS[b.bucket] ?? theme.muted} />
      ))}
    </div>
  );
}

function CoverageBadge({ c }: { c: RiskCoverage }) {
  // Only alarm (red) when coverage is genuinely thin (<60%, where VaR confidence
  // degrades); amber 60–80; muted above. Avoids a permanently-red "error" badge.
  const cw = c.covered_weight_pct;
  const color = cw < 60 ? theme.down : cw < 80 ? theme.warn : theme.muted;
  return (
    <span
      title={`${c.n_covered}/${c.n_assets_total} assets · obs ${c.n_observations}\n${c.assumption}`}
      style={{ fontSize: theme.fontSize.micro, color, border: `1px solid ${theme.border}`, borderRadius: 3, padding: "1px 4px" }}
    >
      {cw.toFixed(0)}% covered
    </span>
  );
}

function RiskXray() {
  // Bumping retryKey re-runs useAsync's fetch — it's already part of the
  // hook's public contract (any deps change re-triggers the effect), so
  // Retry needs no change to useAsync.ts itself.
  const [retryKey, setRetryKey] = useState(0);
  const { data: env, loading, error } = useAsync(() => api.riskXray(), [retryKey], 3_600_000);
  if (loading && !env) return <PanelState kind="loading" message="computing risk…" />;
  if (error) return <PanelState kind="error" message={error} onRetry={() => setRetryKey((k) => k + 1)} />;
  const x = env?.data;
  if (!x) return <PanelState kind="empty" message="no data" />;

  const ddColor = (x.max_drawdown_pct ?? 0) < -25 ? theme.down : theme.text;

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        figure={x.vol_annual_pct != null ? pct(x.vol_annual_pct) : undefined}
        deck={deckLine("risk-xray")}
        agate={
          <>
            <CoverageBadge c={x.coverage} />
            {env && <Provenance source={env.source} stale={env.stale} warnings={env.warnings} fetchedAt={env.fetched_at} />}
          </>
        }
      />
      <div tabIndex={0} style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "6px 8px" }}>
        {/* headline stats — VOL (ann.) dropped: it's now the PanelHead hero figure
           . */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, minmax(0, 1fr))", gap: 10, marginBottom: 8 }}>
          <Stat label="BETA" value={num(x.beta)} sub={`S&P ${num(x.beta_vs_sp500)} · BTC ${num(x.beta_vs_btc)}`} />
          <Stat label="MAX DD" value={pct(x.max_drawdown_pct)} color={ddColor} sub={x.max_drawdown_trough ?? undefined} />
          <Stat label="EFF. N" value={num(x.effective_n, 1)} sub={`HHI ${num(x.hhi, 3)}`} />
          <Stat label="ALPHA" value={pct(x.alpha_annual_pct)} sub={`R² ${num(x.r_squared, 2)}`} />
        </div>

        {/* concentration */}
        <div style={{ borderTop: `1px solid ${theme.grid}`, paddingTop: 5, marginBottom: 6 }}>
          <div style={{ display: "flex", justifyContent: "space-between", fontSize: theme.fontSize.micro }}>
            <span style={{ color: theme.muted }}>CONCENTRATION</span>
            <span className="num">top1 {pct(x.top1_pct)} · top3 {pct(x.top3_pct)} · top5 {pct(x.top5_pct)}</span>
          </div>
          {x.top_holdings.slice(0, 6).map((h) => (
            <BarRow key={h.key} label={h.key} weight={h.weight_pct} color={h.key === "CASH" ? theme.up : theme.accent} />
          ))}
        </div>

        {/* risk contributors */}
        {x.risk_contributors.length > 0 && (
          <div style={{ borderTop: `1px solid ${theme.grid}`, paddingTop: 5, marginBottom: 6 }}>
            <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, marginBottom: 2 }}>RISK CONTRIBUTION (of variance)</div>
            {/* amber, not red — this is a variance-share decomposition, not a loss */}
            {x.risk_contributors.slice(0, 5).map((c) => (
              <BarRow key={c.symbol} label={c.symbol} weight={c.contribution_pct} color={theme.warn} />
            ))}
          </div>
        )}

        {/* exposure */}
        <div style={{ borderTop: `1px solid ${theme.grid}`, paddingTop: 5, display: "flex", gap: 12, flexWrap: "wrap" }}>
          <Bars title="BY CLASS" items={x.exposure_by_class} />
          <Bars title="BY CURRENCY" items={x.exposure_by_currency} />
          <Bars title="BY GEO" items={x.exposure_by_geo} />
        </div>
      </div>
    </div>
  );
}

registerWidget({
  id: "risk-xray",
  title: "Risk X-ray",
  component: RiskXray,
  defaultSize: { w: 7, h: 11 },
  minSize: { w: 4, h: 7 },
  defaultConfig: {},
  category: "Risk",
  description: "Vol / beta / drawdown / concentration / exposure for your portfolio",
});
