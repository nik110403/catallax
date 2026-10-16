// Backtest report (Phase 9, engine). Renders one finished backtest: the post-cost
// equity curve (+ benchmark), the drawdown, and the qlib metric bundle — all
// COMPUTED off the F0 backbone by the off-API worker under the lookahead-clean
// entry rule. Honesty caveats (thin/dominated book, synthetic benchmark,
// survivorship, dropped unsettled tail) ride Envelope.warnings and render as
// VISIBLE chips — never buried. Registered lazily (lightweight-charts chunk).
import { createChart } from "lightweight-charts";
import { useEffect, useRef, useState } from "react";

import { api } from "../api/client";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import { chartOptions } from "./chartTheme";
import { Provenance } from "./Provenance";
import { useAsync } from "./useAsync";

interface Cfg {
  jobId?: string;
}

const fmt = (v: number | null | undefined, dp = 2) =>
  v === null || v === undefined ? "—" : v.toFixed(dp);
const pct = (v: number | null | undefined, dp = 1) =>
  v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(dp)}%`;
// Magnitude (no leading sign) — for non-negative quantities like volatility, where
// a "+" would read as a signed verdict.
const mag = (v: number | null | undefined, dp = 1) =>
  v === null || v === undefined ? "—" : `${v.toFixed(dp)}%`;
const col = (v: number | null | undefined) =>
  v === null || v === undefined ? theme.muted : v > 0 ? theme.up : theme.down;

function Metric({ label, value, color, title }: { label: string; value: string; color?: string; title?: string }) {
  return (
    <div title={title} style={{ display: "flex", flexDirection: "column", gap: 1, minWidth: 0 }}>
      <span style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.05em", whiteSpace: "nowrap" }}>{label}</span>
      <span className="num" style={{ fontSize: theme.fontSize.lg, fontWeight: 700, color: color ?? theme.text, lineHeight: 1.1 }}>
        {value}
      </span>
    </div>
  );
}

export function BacktestReport({ config }: { config: Cfg; onConfigChange?: (c: Cfg) => void }) {
  const jobId = config?.jobId;
  // Bumping retryKey re-runs useAsync's fetch — it's already part of the
  // hook's public contract (any deps change re-triggers the effect), so
  // Retry needs no change to useAsync.ts itself.
  const [retryKey, setRetryKey] = useState(0);
  const { data: env, loading, error } = useAsync(
    () => (jobId ? api.backtestReport(jobId) : Promise.resolve(null)),
    [jobId, retryKey],
  );
  const eqRef = useRef<HTMLDivElement>(null);
  const ddRef = useRef<HTMLDivElement>(null);

  const rep = env?.data;

  // Create the charts only once the report (and thus the chart divs) is in the DOM.
  // The sibling chart widgets create on a bare mount-once effect because THEIR chart
  // div is always rendered; here the divs live inside the `rep`-gated branch, so a
  // mount-once effect would run while the divs don't exist yet, early-return, and
  // never re-run (the blank-charts bug). Gating on `rep` fixes it (and recreates per
  // report). The width fallback covers a not-yet-laid-out panel; the ResizeObserver
  // corrects to the real size.
  useEffect(() => {
    if (!rep || !eqRef.current || !ddRef.current) return;
    const eq = createChart(eqRef.current, { ...chartOptions(), width: eqRef.current.clientWidth || 560, height: eqRef.current.clientHeight || 150 });
    const dd = createChart(ddRef.current, { ...chartOptions(), width: ddRef.current.clientWidth || 560, height: ddRef.current.clientHeight || 80 });
    const eqS = eq.addAreaSeries({ lineColor: theme.accent, topColor: "rgba(240,136,62,0.25)", bottomColor: "rgba(240,136,62,0.02)", lineWidth: 2, title: "equity" });
    const benchS = eq.addLineSeries({ color: theme.muted, lineWidth: 1, title: "bench" });
    const ddS = dd.addAreaSeries({ lineColor: theme.down, topColor: "rgba(248,81,73,0.02)", bottomColor: "rgba(248,81,73,0.28)", lineWidth: 1, title: "drawdown %" });
    eqS.setData(rep.equity.map((p) => ({ time: p.date as never, value: p.value })));
    if (rep.benchmark_equity) benchS.setData(rep.benchmark_equity.map((p) => ({ time: p.date as never, value: p.value })));
    ddS.setData(rep.drawdown.map((p) => ({ time: p.date as never, value: p.value })));
    eq.timeScale().fitContent();
    dd.timeScale().fitContent();
    const ro = new ResizeObserver(() => {
      if (eqRef.current) eq.applyOptions({ width: eqRef.current.clientWidth, height: eqRef.current.clientHeight });
      if (ddRef.current) dd.applyOptions({ width: ddRef.current.clientWidth, height: ddRef.current.clientHeight });
    });
    ro.observe(eqRef.current);
    ro.observe(ddRef.current);
    return () => {
      ro.disconnect();
      eq.remove();
      dd.remove();
    };
  }, [rep]);

  const m = rep?.metrics;

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        figure={rep ? pct(m?.cum_return_pct) : undefined}
        deck={deckLine("backtest-report")}
        agate={
          (rep || env) && (
            <>
              {rep && (
                <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {rep.name} · {rep.engine}
                </span>
              )}
              {env && <Provenance source={env.source} cacheHit={env.cache_hit} stale={env.stale} warnings={env.warnings} fetchedAt={env.fetched_at} />}
            </>
          )
        }
      />

      {!jobId ? (
        <PanelState kind="empty" message="Run a backtest from the Lab, or set a job id." />
      ) : loading && !env ? (
        <PanelState kind="loading" message="loading report…" />
      ) : error ? (
        <PanelState kind="error" message={error} onRetry={() => setRetryKey((k) => k + 1)} />
      ) : !rep ? (
        <PanelState kind="empty" message="no report" />
      ) : (
        <div tabIndex={0} style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "8px 10px", display: "flex", flexDirection: "column", gap: 8 }}>
          <div style={{ display: "grid", gridTemplateColumns: "repeat(4, minmax(0, 1fr))", gap: 8, rowGap: 10 }}>
            <Metric label="CUM RETURN" value={pct(m?.cum_return_pct)} color={col(m?.cum_return_pct)} />
            <Metric label="ANN RETURN" value={pct(m?.ann_return_pct)} color={col(m?.ann_return_pct)} />
            <Metric label="SHARPE" value={fmt(m?.sharpe)} color={col(m?.sharpe)} title="excess return / vol, ×√252 (rf below)" />
            <Metric label="INFO RATIO" value={fmt(m?.info_ratio)} color={col(m?.info_ratio)} title={`vs ${m?.benchmark ?? "—"}`} />
            <Metric label="MAX DD" value={pct(m?.max_drawdown_pct)} color={theme.down} />
            <Metric label="ANN VOL" value={mag(m?.ann_vol_pct)} color={theme.text} />
            <Metric label="TURNOVER" value={m?.turnover_annualised != null ? `${m.turnover_annualised.toFixed(1)}×` : "—"} color={theme.text} title="one-way, annualised" />
            <Metric label="MEAN IC" value={fmt(m?.mean_ic, 3)} color={col(m?.mean_ic)} title="cross-sectional Rank-IC per rebalance" />
          </div>

          <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.05em", marginTop: 2 }}>
            EQUITY (base 1.0, post-cost){rep.benchmark_equity ? " · grey = benchmark" : ""}
          </div>
          <div ref={eqRef} style={{ height: 150, minHeight: 150 }} />
          <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.05em" }}>DRAWDOWN %</div>
          <div ref={ddRef} style={{ height: 80, minHeight: 80 }} />

          <div style={{ display: "flex", justifyContent: "space-between", fontSize: theme.fontSize.micro, color: theme.muted }}>
            <span>{m?.n_matured_periods} matured / {m?.n_pending_periods} pending rebalances</span>
            <span>≈{fmt(m?.n_names_avg, 1)} names · {m?.n_days} days</span>
          </div>

          {rep.warnings.length > 0 && (
            <div style={{ display: "flex", flexDirection: "column", gap: 3, marginTop: 2 }}>
              {rep.warnings.map((w, i) => (
                <div key={i} style={{ fontSize: theme.fontSize.micro, color: theme.warn, background: "rgba(210,153,34,0.10)", border: "1px solid rgba(210,153,34,0.4)", borderRadius: 3, padding: "2px 6px", lineHeight: 1.4 }}>
                  ⚠ {w}
                </div>
              ))}
            </div>
          )}

          <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, lineHeight: 1.5, borderTop: `1px solid ${theme.grid}`, paddingTop: 6 }}>
            Entry = first bar strictly after each rebalance date; held to the next rebalance; costs on turnover; the
            unsettled tail is excluded. Metrics annualised ×252. This is descriptive backtest machinery — read the caveats above.
          </div>
        </div>
      )}
    </div>
  );
}
