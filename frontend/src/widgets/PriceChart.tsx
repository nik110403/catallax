import {
  createChart,
  type IChartApi,
  type ISeriesApi,
  type UTCTimestamp,
} from "lightweight-charts";
import { useEffect, useRef, useState } from "react";

import { api } from "../api/client";
import { ApiError } from "../api/client";
import type { Candle } from "../api/types";
import { LOCALE } from "../format";
import { theme } from "../theme";
import { chartOptions, controlStyle } from "./chartTheme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import type { WidgetProps } from "./registry";

interface ChartConfig {
  symbol: string;
  source?: string;
  interval: string;
  range?: string; // 1M 3M 6M 1Y 2Y 5Y MAX — omitted keeps the backend's own default (30d)
}

// Lookback window per range token, in days. MAX is capped at the backend's own
// HISTORY_MAX_RANGE_DAYS ceiling (currently 3650) rather than left unbounded.
const RANGE_DAYS: Record<string, number> = {
  "1M": 30,
  "3M": 90,
  "6M": 182,
  "1Y": 365,
  "2Y": 730,
  "5Y": 1825,
  MAX: 3650,
};

function toSeries(candles: Candle[]) {
  const rows = candles
    .map((c) => ({
      time: Math.floor(Date.parse(c.ts) / 1000) as UTCTimestamp,
      open: Number(c.open),
      high: Number(c.high),
      low: Number(c.low),
      close: Number(c.close),
    }))
    .filter((r) => Number.isFinite(r.time));
  rows.sort((a, b) => a.time - b.time);
  // de-dup identical timestamps (lightweight-charts requires unique, ascending)
  return rows.filter((r, i) => i === 0 || r.time !== rows[i - 1].time);
}

// Registered lazily in widgets/index.ts (registerLazyWidget) so lightweight-charts
// only loads when a price-chart panel mounts.
export function PriceChart({ config, onConfigChange }: WidgetProps<ChartConfig>) {
  const elRef = useRef<HTMLDivElement>(null);
  const chartRef = useRef<IChartApi | null>(null);
  const seriesRef = useRef<ISeriesApi<"Candlestick"> | null>(null);
  const [ready, setReady] = useState(false);
  const [symInput, setSymInput] = useState(config.symbol);
  const [status, setStatus] = useState("");
  // Canonical hero: last close (design review P1, 2026-08-15 — this widget
  // had none). The candle fetch below is imperative-only (feeds the chart
  // lib directly, nothing is kept in React state), so there is no existing
  // render-scope value to derive a hero from with a one-line expression. A
  // ref avoids adding a second state hook: it's written in the same already-
  // existing fetch below, and the setStatus call in that same block already
  // triggers the re-render that reads it — no new fetch, no new effect.
  const lastPriceRef = useRef<number | null>(null);

  useEffect(() => setSymInput(config.symbol), [config.symbol]);

  useEffect(() => {
    if (!elRef.current) return;
    const chart = createChart(elRef.current, {
      ...chartOptions(),
      width: elRef.current.clientWidth,
      height: elRef.current.clientHeight,
    });
    const series = chart.addCandlestickSeries({
      upColor: theme.up,
      downColor: theme.down,
      borderVisible: false,
      wickUpColor: theme.up,
      wickDownColor: theme.down,
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
    if (!ready) return;
    let alive = true;
    setStatus("loading…");
    // A range token asks the backend for exactly that lookback (via history()'s
    // existing start/end opts) rather than fetching everything and trimming
    // client-side. No range (bare `GO SYM`) omits `start` entirely, so behavior
    // is byte-identical to before this change: the backend's own 30-day default.
    const days = config.range ? RANGE_DAYS[config.range] : undefined;
    const start = days ? new Date(Date.now() - days * 86_400_000).toISOString() : undefined;
    api
      .history(config.symbol, { interval: config.interval, source: config.source, start })
      .then((candles) => {
        if (!alive) return;
        const data = toSeries(candles);
        seriesRef.current?.setData(data);
        chartRef.current?.timeScale().fitContent();
        lastPriceRef.current = data.length ? data[data.length - 1].close : null;
        setStatus(data.length ? "" : "no data");
      })
      .catch((e) => {
        if (!alive) return;
        seriesRef.current?.setData([]);
        lastPriceRef.current = null;
        setStatus(e instanceof ApiError ? `${e.code}: ${e.message}` : (e?.message ?? "error"));
      });
    return () => {
      alive = false;
    };
  }, [config.symbol, config.source, config.interval, config.range, ready]);

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        figure={lastPriceRef.current !== null ? lastPriceRef.current.toLocaleString(LOCALE, { maximumFractionDigits: 2 }) : "—"}
        unit={config.symbol}
        deck={deckLine("price-chart")}
        agate={status || undefined}
      >
        <form
          style={{ display: "flex", gap: 4 }}
          onSubmit={(e) => {
            e.preventDefault();
            const sym = symInput.trim().toUpperCase();
            if (sym) onConfigChange({ ...config, symbol: sym });
          }}
        >
          <input
            value={symInput}
            onChange={(e) => setSymInput(e.target.value)}
            style={{ ...controlStyle, width: 70 }}
            aria-label="symbol"
          />
          <select
            value={config.source ?? ""}
            onChange={(e) => onConfigChange({ ...config, source: e.target.value || undefined })}
            style={controlStyle}
            aria-label="price data source"
          >
            <option value="">yfinance</option>
            <option value="coingecko">coingecko</option>
          </select>
          <select
            value={config.interval}
            onChange={(e) => onConfigChange({ ...config, interval: e.target.value })}
            style={controlStyle}
            aria-label="chart interval"
          >
            <option value="1d">1d</option>
            <option value="1h">1h</option>
            <option value="1wk">1wk</option>
          </select>
        </form>
      </PanelHead>
      <div ref={elRef} style={{ flex: 1, minHeight: 0 }} />
    </div>
  );
}
