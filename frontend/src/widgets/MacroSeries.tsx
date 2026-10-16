// Macro time series (Phase 8, Macro). A single indicator line via lightweight-charts.
// `series` of the form `ROOT.country` (e.g. CPI.united_states) is served keyless by
// econdb; a bare id (e.g. DGS10, UNRATE) is a FRED series and needs OPENBB_FRED_API_KEY.
// Registered lazily (charts are code-split) — see widgets/index.ts.
import { createChart, type IChartApi, type ISeriesApi } from "lightweight-charts";
import { useEffect, useRef, useState } from "react";

import { api } from "../api/client";
import { LOCALE } from "../format";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import { chartOptions, controlStyle } from "./chartTheme";
import { Provenance } from "./Provenance";
import type { WidgetProps } from "./registry";
import { useAsync } from "./useAsync";

interface SeriesConfig {
  series: string; // "CPI.united_states" (econdb) | "DGS10" (FRED)
  transform: string; // "toya" | "level"
}

export function MacroSeriesWidget({ config, onConfigChange }: WidgetProps<SeriesConfig>) {
  const series = config.series ?? "CPI.united_states";
  const transform = config.transform ?? "toya";
  const [draft, setDraft] = useState(series);
  useEffect(() => setDraft(series), [series]);

  const elRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Line"> | null>(null);
  const [ready, setReady] = useState(false);

  const { data: env, loading, error, errorCode } = useAsync(
    () => api.macroSeries(series, transform),
    [series, transform],
    3_600_000,
  );

  useEffect(() => {
    if (!elRef.current) return;
    const chart = createChart(elRef.current, {
      ...chartOptions(),
      width: elRef.current.clientWidth,
      height: elRef.current.clientHeight,
    });
    const line = chart.addLineSeries({ color: theme.accent, lineWidth: 2 });
    chartRef.current = chart;
    seriesRef.current = line;
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
    if (!ready || !seriesRef.current) return;
    const pts = env?.data.points ?? [];
    seriesRef.current.setData(pts.map((p) => ({ time: p.date as never, value: Number(p.value) })));
    chartRef.current?.timeScale().fitContent();
  }, [env, ready]);

  const d = env?.data;
  const hasData = (d?.points.length ?? 0) > 0;
  const latest = hasData ? d!.points[d!.points.length - 1].value : null;
  // econdb values are fractions; FRED levels are absolute.
  const isPct = d?.unit?.includes("%");
  const latestStr =
    latest === null || latest === undefined
      ? "—"
      : isPct
        ? `${(latest * 100).toFixed(2)}%`
        : latest.toLocaleString(LOCALE, { maximumFractionDigits: 2 });

  const commit = () => {
    const v = draft.trim();
    if (v && v !== series) onConfigChange({ ...config, series: v });
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        figure={hasData ? latestStr : undefined}
        deck={deckLine("macro-series")}
        agate={
          <>
            <span title={d?.title}>{d?.title ?? series}</span>
            {env && <Provenance source={env.source} cacheHit={env.cache_hit} stale={env.stale} warnings={env.warnings} fetchedAt={env.fetched_at} />}
          </>
        }
      >
        <input
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => e.key === "Enter" && commit()}
          placeholder="CPI.united_states or DGS10"
          spellCheck={false}
          // `flex: 1` was inert here: PanelHead's `children` wrapper is
          // flexShrink:0 with no width of its own, so there was never any
          // extra space to grow into and the input fell back to its
          // intrinsic ~120px, clipping the placeholder — the only place
          // this input's format is documented.
          // Fixed locally with an explicit width rather than in PanelHead:
          // most other widgets' `children` are fixed-width selects, and
          // PanelHead is shared with widgets outside this fix's scope —
          // widening its wrapper generally risks misaligning controls I
          // can't verify here. 210px comfortably fits the placeholder
          // ("CPI.united_states or DGS10", ~159px at 11px) with room to type.
          style={{ ...controlStyle, width: 210, flexShrink: 0 }}
          title="econdb ROOT.country (keyless) or a FRED series id (needs key)"
        />
        <select
          value={transform}
          onChange={(e) => onConfigChange({ ...config, transform: e.target.value })}
          style={controlStyle}
          title="econdb transform"
          aria-label="series transform"
        >
          <option value="toya">YoY</option>
          <option value="level">Level</option>
        </select>
      </PanelHead>
      {/* keep the chart mounted (the create effect needs the node) but hide it
          whenever there's no data to show — including the disabled-service
          case, where `!env` was true (the fetch never resolved a body) and
          the chart's blank frame rendered above the disabled message instead
          of the message owning the body via `fill`. */}
      <div ref={elRef} style={{ flex: 1, minHeight: 0, display: !error && (!env || hasData) ? "block" : "none" }} />
      {/* Short form, not the full disabled explainer — the Macro tab renders
          this panel alongside Indicator Heatmap and Recession Signals, and
          all three are gated by the same "macro" profile. Indicator Heatmap
          (top-left, the tab's largest panel) carries the full instruction;
          repeating it here made three identical panels over an empty
          viewport read as unfinished rather than as one situation
         . Local + hardcoded rather than a
          shared "which widget explains" flag — one profile, one page, not
          worth a coordination layer. */}
      {errorCode === "SIDECAR_NOT_ENABLED" && (
        <div
          className="panel-state"
          role="status"
          style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", textAlign: "center", padding: "0 12px" }}
        >
          {/* One child, not bare text + a span: a flex container wraps
              multiple direct text/element children in separate anonymous
              flex items and trims the whitespace between them — "Part of "
              + "Macro" + " (off)" rendered as "Part ofMacro(off)" until this
              was wrapped in a single span (verified in the built page). */}
          <span>
            Part of <span style={{ fontFamily: theme.fontMono }}>Macro</span> (off) — see Indicator Heatmap.
          </span>
        </div>
      )}
      {error && errorCode !== "SIDECAR_NOT_ENABLED" && <PanelState kind="error" message={error} fill />}
      {!error && env && !hasData && (
        <PanelState kind="empty" message={env.warnings?.[0] ?? "no data for this series"} fill />
      )}
    </div>
  );
}
