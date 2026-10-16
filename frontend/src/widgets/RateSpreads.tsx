// Treasury spread history (Phase 8, Rates). 10y-2y and 10y-3m over ~2 years via
// lightweight-charts, with a zero baseline so inversions (spread < 0) read at a
// glance. Keyless (federal_reserve). Registered lazily — see widgets/index.ts.
import { createChart, LineStyle, type IChartApi, type ISeriesApi } from "lightweight-charts";
import { useEffect, useRef, useState } from "react";

import { api } from "../api/client";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import { chartOptions } from "./chartTheme";
import { Provenance } from "./Provenance";
import { useAsync } from "./useAsync";

const bps = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : `${v >= 0 ? "+" : ""}${(v * 100).toFixed(0)}bp`;

export function RateSpreads() {
  const elRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const aRef = useRef<ISeriesApi<"Line"> | null>(null);
  const bRef = useRef<ISeriesApi<"Line"> | null>(null);
  const [ready, setReady] = useState(false);

  const { data: env, loading, error, errorCode } = useAsync(() => api.macroRates(), [], 3_600_000);
  // Canonical hero: the current (latest) 10Y-2Y spread — this widget had none
  //. Derived from data already in scope, no
  // new fetch/state.
  const spreads = env?.data.spreads ?? [];
  const latest = spreads[spreads.length - 1];

  useEffect(() => {
    if (!elRef.current) return;
    const chart = createChart(elRef.current, {
      ...chartOptions(),
      width: elRef.current.clientWidth,
      height: elRef.current.clientHeight,
    });
    const a = chart.addLineSeries({ color: theme.accent, lineWidth: 2, title: "10Y-2Y" });
    const b = chart.addLineSeries({ color: theme.chart[0], lineWidth: 2, title: "10Y-3M" });
    // zero baseline — anything below is an inversion
    a.createPriceLine({ price: 0, color: theme.muted, lineWidth: 1, lineStyle: LineStyle.Dashed, axisLabelVisible: true, title: "" });
    chartRef.current = chart;
    aRef.current = a;
    bRef.current = b;
    const ro = new ResizeObserver(() => {
      if (elRef.current)
        chart.applyOptions({ width: elRef.current.clientWidth, height: elRef.current.clientHeight });
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
    if (!ready || !aRef.current || !bRef.current) return;
    const sp = env?.data.spreads ?? [];
    aRef.current.setData(
      sp.filter((p) => p.t10y_2y !== null).map((p) => ({ time: p.date as never, value: Number(p.t10y_2y) })),
    );
    bRef.current.setData(
      sp.filter((p) => p.t10y_3m !== null).map((p) => ({ time: p.date as never, value: Number(p.t10y_3m) })),
    );
    chartRef.current?.timeScale().fitContent();
  }, [env, ready]);

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        figure={latest ? bps(latest.t10y_2y) : undefined}
        figureColor={(latest?.t10y_2y ?? 0) < 0 ? theme.down : theme.up}
        deck={deckLine("rate-spreads")}
        agate={
          <>
            <span style={{ color: theme.accent }}>● 10Y-2Y</span>
            <span style={{ color: theme.chart[0] }}>● 10Y-3M</span>
            {env && <Provenance source={env.source} cacheHit={env.cache_hit} stale={env.stale} warnings={env.warnings} fetchedAt={env.fetched_at} />}
          </>
        }
      />
      <div ref={elRef} style={{ flex: 1, minHeight: 0 }} />
      {errorCode === "SIDECAR_NOT_ENABLED" && <PanelState kind="disabled" profile="macro" />}
      {error && errorCode !== "SIDECAR_NOT_ENABLED" && <PanelState kind="error" message={error} />}
      {loading && !env && <PanelState kind="loading" />}
    </div>
  );
}
