import { createChart, type IChartApi, type ISeriesApi } from "lightweight-charts";
import { useEffect, useRef, useState } from "react";

import { api } from "../api/client";
import type { NetWorthScope } from "../api/types";
import { LOCALE } from "../format";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import { chartOptions, controlStyle } from "./chartTheme";
import type { WidgetProps } from "./registry";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";

interface NetWorthConfig {
  months: number;
  // "all" = investments + portfolio cash + ledger bank cash; "investments" = holdings only.
  scope?: NetWorthScope;
}

const fmt0 = (v: string | number) =>
  Number(v).toLocaleString(LOCALE, { maximumFractionDigits: 0 });

function Chip({ label, value, color }: { label: string; value: string; color: string }) {
  return (
    <span style={{ display: "inline-flex", alignItems: "center", gap: 4, whiteSpace: "nowrap" }}>
      <span style={{ width: 7, height: 7, borderRadius: 2, background: color }} />
      <span style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.04em" }}>{label}</span>
      <span className="num" style={{ fontSize: theme.fontSize.micro }}>
        {fmt0(value)}
      </span>
    </span>
  );
}

// Net worth = the imported portfolio + the ledger's bank/wallet cash.
function NetWorth({ config, onConfigChange }: WidgetProps<NetWorthConfig>) {
  const elRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Area"> | null>(null);
  const [ready, setReady] = useState(false);

  const scope: NetWorthScope = config.scope ?? "all";
  // Bumping retryKey re-runs useAsync's fetch — it's already part of the
  // hook's public contract (any deps change re-triggers the effect), so
  // Retry needs no change to useAsync.ts itself.
  const [retryKey, setRetryKey] = useState(0);
  const { data, loading, error } = useAsync(
    () => api.networth(config.months, scope),
    [config.months, scope, retryKey],
    300_000,
  );

  useEffect(() => {
    if (!elRef.current) return;
    const chart = createChart(elRef.current, {
      ...chartOptions(),
      width: elRef.current.clientWidth,
      height: elRef.current.clientHeight,
    });
    const series = chart.addAreaSeries({
      lineColor: theme.accent,
      topColor: "rgba(240,136,62,0.35)",
      bottomColor: "rgba(240,136,62,0.02)",
      lineWidth: 2,
    });
    chartRef.current = chart;
    seriesRef.current = series;
    const ro = new ResizeObserver(() => {
      if (elRef.current)
        chart.applyOptions({
          width: elRef.current.clientWidth,
          height: elRef.current.clientHeight,
        });
    });
    ro.observe(elRef.current);
    setReady(true);
    return () => {
      ro.disconnect();
      chart.remove();
      setReady(false);
    };
  }, []);

  useEffect(() => {
    if (!ready || !seriesRef.current || !data) return;
    seriesRef.current.setData(
      data.points.map((p) => ({ time: p.date as never, value: Number(p.value) })),
    );
    chartRef.current?.timeScale().fitContent();
  }, [data, ready]);

  const cur = data?.currency ?? "EUR";
  const c = data?.components;

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        figure={data ? fmt0(data.total) : undefined}
        unit={cur}
        deck={deckLine("net-worth")}
        agate={
          c &&
          (scope === "investments" ? (
            // Investments-only: the headline already IS investments, so the cash
            // chips would mislead — show just a scope tag.
            <span style={{ letterSpacing: "0.04em" }}>MARKET-PRICED HOLDINGS ONLY · CASH EXCLUDED</span>
          ) : (
            <>
              <Chip label="INVEST" value={c.investments} color={theme.accent} />
              <Chip label="PORTFOLIO CASH" value={c.wf_cash} color={theme.chart[0]} />
              <Chip label="BANK" value={c.ledger_cash} color={theme.up} />
            </>
          ))
        }
      >
        <select
          value={scope}
          onChange={(e) => onConfigChange({ ...config, scope: e.target.value as NetWorthScope })}
          style={controlStyle}
          title="All net worth vs. just market-priced investments"
          aria-label="net worth scope"
        >
          <option value="all">All</option>
          <option value="investments">Investments</option>
        </select>
        <select
          value={String(config.months)}
          onChange={(e) => onConfigChange({ ...config, months: Number(e.target.value) })}
          style={controlStyle}
          aria-label="net worth time range"
        >
          <option value="6">6M</option>
          <option value="12">12M</option>
          <option value="24">24M</option>
        </select>
      </PanelHead>
      <div ref={elRef} style={{ flex: 1, minHeight: 0 }} />
      {error && <PanelState kind="error" message={error} onRetry={() => setRetryKey((k) => k + 1)} />}
      {loading && !data && <PanelState kind="loading" />}
    </div>
  );
}

registerWidget<NetWorthConfig>({
  id: "net-worth",
  title: "Net Worth",
  component: NetWorth,
  defaultSize: { w: 6, h: 8 },
  minSize: { w: 4, h: 6 },
  defaultConfig: { months: 12, scope: "all" },
});
