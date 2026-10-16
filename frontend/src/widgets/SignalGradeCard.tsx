// Signal grade card. Drills one signal family: the cross-sectional
// Rank-IC time series, the quintile long/short spread (monotonicity check), and the
// headline ICIR / t-stat — all COMPUTED from F0 forward returns under the strict
// lookahead rule (entry = the first bar after the decision date, only matured +
// settled windows graded). The methodology footer makes that discipline visible.
import { useState } from "react";

import { api } from "../api/client";
import type { SignalGrade } from "../api/types";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import { Provenance } from "./Provenance";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";

interface Cfg {
  signalId?: string;
}

const fmt = (v: number | null | undefined, dp = 2) =>
  v === null || v === undefined ? "—" : v.toFixed(dp);
const signed = (v: number | null | undefined, dp = 1) =>
  v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(dp)}%`;
const col = (v: number | null | undefined) =>
  v === null || v === undefined ? theme.muted : v > 0 ? theme.up : theme.down;

function Stat({ label, value, color, sup }: { label: string; value: string; color?: string; sup?: string }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 1, minWidth: 0 }}>
      <span style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.05em", whiteSpace: "nowrap" }}>{label}</span>
      <span className="num" style={{ fontSize: theme.fontSize.lg, fontWeight: 700, color: color ?? theme.text, lineHeight: 1.1 }}>
        {value}
        {sup && <span style={{ fontSize: theme.fontSize.base, color: theme.up, marginLeft: 3 }}>{sup}</span>}
      </span>
    </div>
  );
}

// Per-date Rank-IC: a row of bars growing up (green) / down (red) from a zero line.
function ICSeries({ g }: { g: SignalGrade }) {
  const pts = g.per_date.filter((p) => p.rank_ic !== null);
  if (!pts.length) return null;
  const max = Math.max(0.05, ...pts.map((p) => Math.abs(p.rank_ic as number)));
  return (
    <div>
      <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.05em", marginBottom: 3 }}>
        RANK-IC BY DATE · {pts.length} cross-sections
      </div>
      <div style={{ display: "flex", alignItems: "stretch", gap: 1, height: 66, borderBottom: `1px dashed ${theme.border}`, position: "relative" }}>
        {/* zero baseline at mid-height (muted so it reads as a reference line) */}
        <div style={{ position: "absolute", left: 0, right: 0, top: "50%", borderTop: `1px solid ${theme.muted}`, opacity: 0.5 }} />
        {pts.map((p) => {
          const v = p.rank_ic as number;
          const h = (Math.abs(v) / max) * 50; // % of half-height
          return (
            <div key={p.date} title={`${p.date}: Rank-IC ${v.toFixed(3)} · ${p.n_names} names`} style={{ flex: 1, display: "flex", flexDirection: "column", justifyContent: "center" }}>
              <div style={{ height: "50%", display: "flex", alignItems: "flex-end" }}>
                {v > 0 && <div style={{ width: "100%", height: `${h}%`, background: theme.up }} />}
              </div>
              <div style={{ height: "50%", display: "flex", alignItems: "flex-start" }}>
                {v < 0 && <div style={{ width: "100%", height: `${h}%`, background: theme.down }} />}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// Quintile mean forward return per score bucket — a monotone ramp = real ranking edge.
function Quintiles({ g }: { g: SignalGrade }) {
  const bk = g.quantile_buckets.filter((b) => b.mean_forward_return_pct !== null);
  if (!bk.length) return null;
  const max = Math.max(...bk.map((b) => Math.abs(b.mean_forward_return_pct as number)), 0.1);
  return (
    <div>
      <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.05em", marginBottom: 3 }}>
        MEAN FWD RETURN BY SCORE QUINTILE (Q1 low → Q{g.quantiles} high)
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
        {bk.map((b) => {
          const v = b.mean_forward_return_pct as number;
          return (
            <div key={b.bucket} style={{ display: "flex", alignItems: "center", gap: 6, fontSize: theme.fontSize.micro }}>
              <span style={{ width: 20, color: theme.muted }}>Q{b.bucket}</span>
              <div style={{ flex: 1, background: theme.grid, height: 10, borderRadius: 2, overflow: "hidden" }}>
                {/* min-width so a near-zero bucket (e.g. a flat Q3) still renders, not vanishes */}
                <div style={{ width: `${(Math.abs(v) / max) * 100}%`, minWidth: 2, height: "100%", background: col(v) }} />
              </div>
              <span className="num" style={{ width: 48, textAlign: "right", color: col(v) }}>{signed(v)}</span>
            </div>
          );
        })}
      </div>
    </div>
  );
}

function SignalGradeCard({ config, onConfigChange }: { config: Cfg; onConfigChange: (c: Cfg) => void }) {
  const cat = useAsync(() => api.signalCatalog(), []);
  const ids = cat.data?.data.signals.map((s) => s.signal_id) ?? [];
  const selected = config.signalId && ids.includes(config.signalId) ? config.signalId : ids[0];

  // Bumping retryKey re-runs useAsync's fetch — it's already part of the
  // hook's public contract (any deps change re-triggers the effect), so
  // Retry needs no change to useAsync.ts itself.
  const [retryKey, setRetryKey] = useState(0);
  const { data: env, loading, error } = useAsync(
    () => (selected ? api.signalGrade(selected) : Promise.resolve(null)),
    [selected, retryKey],
    3_600_000,
  );

  if (cat.loading && !cat.data) return <PanelState kind="loading" message="loading signals…" />;
  if (!ids.length) return <PanelState kind="empty" message="No signals logged — seed them from the Scoreboard." />;

  const g = env?.data;

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        figure={g ? fmt(g.icir) : undefined}
        deck={deckLine("signal-grade-card")}
        agate={env && <Provenance source={env.source} stale={env.stale} warnings={env.warnings} fetchedAt={env.fetched_at} />}
      >
        <select
          value={selected}
          onChange={(e) => onConfigChange({ ...config, signalId: e.target.value })}
          style={{ background: theme.panelHeader, color: theme.text, border: `1px solid ${theme.border}`, borderRadius: 3, fontSize: theme.fontSize.micro, padding: "2px 4px", maxWidth: "60%" }}
          aria-label="signal to grade"
        >
          {ids.map((id) => (
            <option key={id} value={id}>{id}</option>
          ))}
        </select>
      </PanelHead>

      {loading && !env ? (
        <PanelState kind="loading" message="grading…" />
      ) : error ? (
        <PanelState kind="error" message={error} onRetry={() => setRetryKey((k) => k + 1)} />
      ) : !g ? (
        <PanelState kind="empty" message="no data" />
      ) : (
        <div tabIndex={0} style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "8px 10px", display: "flex", flexDirection: "column", gap: 10 }}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(3, minmax(0, 1fr))", gap: 8, rowGap: 10 }}>
            <Stat label="ICIR" value={fmt(g.icir)} color={col(g.icir)} />
            <Stat label="t-STAT" value={fmt(g.t_stat)} color={theme.text} sup={g.significant ? "★" : undefined} />
            <Stat label="MEAN RANK-IC" value={fmt(g.mean_rank_ic, 3)} color={col(g.mean_rank_ic)} />
            <Stat label="L/S CUMULATIVE" value={signed(g.ls_cumulative_pct)} color={col(g.ls_cumulative_pct)} />
            <Stat label="HIT (WILSON LB)" value={g.hit_rate === null ? "—" : `${(g.hit_rate * 100).toFixed(0)}%`} color={theme.text} />
            <Stat label="MONOTONICITY" value={fmt(g.monotonicity, 2)} color={col(g.monotonicity)} />
          </div>

          <ICSeries g={g} />
          <Quintiles g={g} />

          <div style={{ display: "flex", justifyContent: "space-between", fontSize: theme.fontSize.micro, color: theme.muted }}>
            <span>long basket {signed(g.long_mean_fwd_pct)}</span>
            <span>short basket {signed(g.short_mean_fwd_pct)}</span>
            <span>{g.coverage.n_pairs_matured} matured / {g.coverage.n_pairs_pending} pending</span>
          </div>

          <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, lineHeight: 1.5, borderTop: `1px solid ${theme.grid}`, paddingTop: 6 }}>
            Entry = close of the first bar after the decision date; exit = {g.horizon_bars} trading bars later, graded only
            once settled ({g.settle_days}+ bars trail it). {g.methodology}; t = ICIR·√N.
            {g.low_confidence && " Thin panel — significance withheld."}
          </div>
        </div>
      )}
    </div>
  );
}

registerWidget({
  id: "signal-grade-card",
  title: "Signal Grade",
  component: SignalGradeCard,
  defaultSize: { w: 5, h: 13 },
  minSize: { w: 4, h: 9 },
  defaultConfig: {} as Cfg,
  category: "Signals",
  description: "Drill one signal: Rank-IC over time, quintile spread, ICIR / t-stat, lookahead-clean",
});
