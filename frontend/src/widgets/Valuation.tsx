// Valuation (Phase 10b). Name + price + market cap header, then a tile grid of
// valuation ratios (P/E, P/B, P/S, EV/EBITDA, ROE, …). Symbol configurable.
import { useEffect, useState } from "react";

import { api } from "../api/client";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import { Provenance } from "./Provenance";
import type { WidgetProps } from "./registry";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";

interface Config {
  symbol: string;
}

const symInputStyle = {
  background: theme.bg,
  color: theme.text,
  border: `1px solid ${theme.border}`,
  borderRadius: 4,
  padding: "2px 5px",
  font: "inherit",
  fontSize: theme.fontSize.micro,
};

function fmtCompact(v: number | null): string {
  if (v === null) return "—";
  const abs = Math.abs(v);
  if (abs >= 1e12) return `${(v / 1e12).toFixed(1)}T`;
  if (abs >= 1e9) return `${(v / 1e9).toFixed(1)}B`;
  if (abs >= 1e6) return `${(v / 1e6).toFixed(1)}M`;
  if (abs >= 1e3) return `${(v / 1e3).toFixed(0)}k`;
  return v.toFixed(2);
}

function fmtRatioVal(v: number | null, unit: string): string {
  if (v === null) return "—";
  if (unit === "%") return `${v.toFixed(2)}%`;
  return `${v.toFixed(2)}${unit === "x" ? "x" : ""}`;
}

/** Return-oriented keys are coloured by sign; plain multiples stay neutral. */
function ratioValueColor(key: string, v: number | null): string {
  if (v === null) return theme.muted;
  if (/roe|roa|fcf|div/.test(key.toLowerCase())) return v >= 0 ? theme.up : theme.down;
  return theme.text;
}

function Valuation({ config, onConfigChange }: WidgetProps<Config>) {
  const [symInput, setSymInput] = useState(config.symbol);
  useEffect(() => setSymInput(config.symbol), [config.symbol]);

  // Bumping retryKey re-runs useAsync's fetch — it's already part of the
  // hook's public contract (any deps change re-triggers the effect), so
  // Retry needs no change to useAsync.ts itself.
  const [retryKey, setRetryKey] = useState(0);
  const { data: env, loading, error } = useAsync(
    () => api.researchValuation(config.symbol),
    [config.symbol, retryKey],
  );

  const d = env?.data;

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        figure={d?.price != null ? d.price.toFixed(2) : undefined}
        unit={d?.currency ?? undefined}
        deck={deckLine("research-valuation")}
        agate={
          (d || env) && (
            <>
              {d && <span style={{ color: theme.text, fontWeight: 600 }}>{d.name}</span>}
              {d?.market_cap !== null && d?.market_cap !== undefined && (
                <span>
                  Mkt Cap <span className="num">{fmtCompact(d.market_cap)}</span>
                </span>
              )}
              {env && (
                <Provenance
                  source={env.source}
                  cacheHit={env.cache_hit}
                  stale={env.stale}
                  warnings={env.warnings}
                  fetchedAt={env.fetched_at}
                />
              )}
            </>
          )
        }
      >
        <form
          style={{ display: "flex", alignItems: "center" }}
          onSubmit={(e) => {
            e.preventDefault();
            const sym = symInput.trim().toUpperCase();
            if (sym) onConfigChange({ ...config, symbol: sym });
          }}
        >
          <input
            value={symInput}
            onChange={(e) => setSymInput(e.target.value)}
            style={{ ...symInputStyle, width: 70 }}
            aria-label="symbol"
          />
        </form>
      </PanelHead>

      {loading && !d && <PanelState kind="loading" />}
      {error && <PanelState kind="error" message={error} onRetry={() => setRetryKey((k) => k + 1)} />}
      {d && (
        <div
          tabIndex={0}
          style={{
            flex: 1,
            minHeight: 0,
            overflowY: "auto",
            padding: "6px 8px",
          }}
        >
          {/* ratio tile grid */}
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fill, minmax(96px, 1fr))",
              gap: 5,
            }}
          >
            {d.ratios.map((r) => (
              <div
                key={r.key}
                style={{
                  background: theme.grid,
                  borderRadius: 4,
                  padding: "5px 7px",
                }}
              >
                <div
                  style={{
                    fontSize: theme.fontSize.micro,
                    color: theme.muted,
                    marginBottom: 3,
                    whiteSpace: "nowrap",
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                  }}
                >
                  {r.label}
                </div>
                <div
                  className="num"
                  style={{
                    fontSize: theme.fontSize.base,
                    color: ratioValueColor(r.key, r.value),
                    fontWeight: 600,
                  }}
                >
                  {fmtRatioVal(r.value, r.unit)}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

registerWidget({
  id: "research-valuation",
  title: "Stock Valuation",
  component: Valuation,
  defaultSize: { w: 5, h: 8 },
  minSize: { w: 3, h: 5 },
  defaultConfig: { symbol: "AAPL" },
  category: "Equity Research",
  description:
    "Live price, market cap, and key valuation multiples (P/E, P/B, P/S, ROE, …) for one equity",
});
