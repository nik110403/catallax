// Company Fundamentals (Phase 10b). Profile header + four statement tables:
// Income / Balance Sheet / Cash Flow / Margins & Returns. Symbol configurable.
import { useEffect, useState } from "react";

import { api } from "../api/client";
import type { StatementLine } from "../api/types";
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
  return v.toFixed(0);
}

function fmtMargin(v: number | null): string {
  return v === null ? "—" : `${v.toFixed(1)}%`;
}

function StmtTable({
  title,
  years,
  rows,
  isMargin,
}: {
  title: string;
  years: string[];
  rows: StatementLine[];
  isMargin?: boolean;
}) {
  const cols = years.slice(0, 5);
  return (
    <div style={{ marginBottom: 6 }}>
      <div
        style={{
          fontSize: theme.fontSize.micro,
          color: theme.muted,
          letterSpacing: "0.05em",
          paddingTop: 6,
          paddingBottom: 4,
          borderTop: `1px solid ${theme.border}`,
        }}
      >
        {title}
      </div>
      <table style={{ width: "100%", borderCollapse: "collapse" }}>
        <thead>
          <tr>
            <th
              style={{
                fontSize: theme.fontSize.micro,
                color: theme.muted,
                fontWeight: 400,
                textAlign: "left",
                paddingBottom: 3,
              }}
            >
              ITEM
            </th>
            {cols.map((y) => (
              <th
                key={y}
                style={{
                  fontSize: theme.fontSize.micro,
                  color: theme.muted,
                  fontWeight: 400,
                  textAlign: "right",
                  paddingRight: 6,
                  paddingBottom: 3,
                  whiteSpace: "nowrap",
                }}
              >
                {y.slice(0, 4)}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={row.key}>
              <td
                style={{
                  fontSize: theme.fontSize.micro,
                  color: theme.muted,
                  paddingRight: 8,
                  paddingBottom: 2,
                  whiteSpace: "nowrap",
                  maxWidth: 130,
                  overflow: "hidden",
                  textOverflow: "ellipsis",
                }}
              >
                {row.label}
              </td>
              {cols.map((y, i) => {
                const v = row.values[i] ?? null;
                const display = isMargin ? fmtMargin(v) : fmtCompact(v);
                const cellColor = isMargin
                  ? v === null
                    ? theme.muted
                    : v >= 0
                      ? theme.up
                      : theme.down
                  : theme.text;
                return (
                  <td
                    key={y}
                    className="num"
                    style={{
                      fontSize: theme.fontSize.micro,
                      color: cellColor,
                      textAlign: "right",
                      paddingRight: 6,
                      paddingBottom: 2,
                      whiteSpace: "nowrap",
                    }}
                  >
                    {display}
                  </td>
                );
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Fundamentals({ config, onConfigChange }: WidgetProps<Config>) {
  const [symInput, setSymInput] = useState(config.symbol);
  useEffect(() => setSymInput(config.symbol), [config.symbol]);

  // Bumping retryKey re-runs useAsync's fetch — it's already part of the
  // hook's public contract (any deps change re-triggers the effect), so
  // Retry needs no change to useAsync.ts itself.
  const [retryKey, setRetryKey] = useState(0);
  const { data: env, loading, error } = useAsync(
    () => api.researchFundamentals(config.symbol),
    [config.symbol, retryKey],
  );

  const d = env?.data;

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        deck={deckLine("research-fundamentals")}
        agate={
          (d || env) && (
            <>
              {d && <span style={{ color: theme.text, fontWeight: 600 }}>{d.name}</span>}
              {d?.sector && <span>{d.sector}</span>}
              {d?.industry && <span>{d.industry}</span>}
              {d?.currency && <span>{d.currency}</span>}
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
          {d.income.length > 0 && (
            <StmtTable title="INCOME STATEMENT" years={d.fiscal_years} rows={d.income} />
          )}
          {d.balance.length > 0 && (
            <StmtTable title="BALANCE SHEET" years={d.fiscal_years} rows={d.balance} />
          )}
          {d.cashflow.length > 0 && (
            <StmtTable title="CASH FLOW" years={d.fiscal_years} rows={d.cashflow} />
          )}
          {d.margins.length > 0 && (
            <StmtTable
              title="MARGINS & RETURNS"
              years={d.fiscal_years}
              rows={d.margins}
              isMargin
            />
          )}
        </div>
      )}
    </div>
  );
}

registerWidget({
  id: "research-fundamentals",
  title: "Fundamentals",
  component: Fundamentals,
  defaultSize: { w: 7, h: 16 },
  minSize: { w: 4, h: 8 },
  defaultConfig: { symbol: "AAPL" },
  category: "Equity Research",
  description: "Annual income, balance sheet, cash flow, and margin tables for one equity symbol",
});
