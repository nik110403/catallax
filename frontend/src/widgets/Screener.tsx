// Stock Screener (Phase 10b). Compact filter bar + results table sorted by
// F-Score descending. F/Z/M cells coloured by threshold. No persistent config —
// all filter state is session-local.
import { useState } from "react";

import { api } from "../api/client";
import type { ScreenFilters } from "../api/types";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import { Provenance } from "./Provenance";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";

const inputStyle = {
  background: theme.bg,
  color: theme.text,
  border: `1px solid ${theme.border}`,
  borderRadius: 4,
  padding: "2px 4px",
  font: "inherit",
  fontSize: theme.fontSize.micro,
  width: 52,
};

function parseF(s: string): number | undefined {
  const n = parseFloat(s);
  return Number.isFinite(n) ? n : undefined;
}

function fScoreColor(v: number | null): string {
  if (v === null) return theme.muted;
  if (v >= 7) return theme.up;
  if (v <= 2) return theme.down;
  return theme.muted;
}

function zScoreColor(v: number | null): string {
  if (v === null) return theme.muted;
  if (v > 2.99) return theme.up;
  if (v < 1.81) return theme.down;
  return theme.muted;
}

function mScoreColor(v: number | null): string {
  if (v === null) return theme.muted;
  return v > -2.22 ? theme.accent : theme.muted;
}

function fmtOpt(v: number | null, dp = 1): string {
  return v === null ? "—" : v.toFixed(dp);
}

// No widget props — filter state is session-local; config persists nothing.
function Screener() {
  const [minFInput, setMinFInput] = useState("");
  const [maxPEInput, setMaxPEInput] = useState("");
  const [minDivInput, setMinDivInput] = useState("");
  const [minZInput, setMinZInput] = useState("");
  const [filters, setFilters] = useState<ScreenFilters>({});

  const apply = () => {
    const f: ScreenFilters = {};
    const mf = parseF(minFInput);
    if (mf !== undefined) f.min_f_score = mf;
    const mp = parseF(maxPEInput);
    if (mp !== undefined) f.max_pe = mp;
    const md = parseF(minDivInput);
    if (md !== undefined) f.min_div_yield = md;
    const mz = parseF(minZInput);
    if (mz !== undefined) f.min_z_score = mz;
    setFilters(f);
  };

  // Bumping retryKey re-runs useAsync's fetch — it's already part of the
  // hook's public contract (any deps change re-triggers the effect), so
  // Retry needs no change to useAsync.ts itself.
  const [retryKey, setRetryKey] = useState(0);
  const { data: env, loading, error } = useAsync(
    () => api.researchScreen(filters),
    [filters, retryKey],
  );

  const d = env?.data;
  const sorted = d ? [...d.rows].sort((a, b) => (b.f_score ?? -1) - (a.f_score ?? -1)) : [];

  const thStyle = {
    fontSize: theme.fontSize.micro,
    color: theme.muted,
    fontWeight: 400,
    textAlign: "right" as const,
    paddingRight: 6,
    paddingBottom: 4,
    whiteSpace: "nowrap" as const,
    position: "sticky" as const,
    top: 0,
    background: theme.panelHeader,
    zIndex: 1,
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        deck={deckLine("research-screen")}
        agate={
          (d || env) && (
            <>
              {d && (
                <span style={{ color: theme.accent, letterSpacing: "0.06em" }}>
                  {d.filtered}/{d.universe_size} MATCHED
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
      />

      {/* filter bar */}
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: 6,
          padding: "5px 8px",
          borderBottom: `1px solid ${theme.border}`,
          alignItems: "center",
        }}
      >
        <label style={{ fontSize: theme.fontSize.micro, color: theme.muted, display: "flex", alignItems: "center", gap: 3 }}>
          Min F
          <input
            value={minFInput}
            onChange={(e) => setMinFInput(e.target.value)}
            placeholder="—"
            style={inputStyle}
          />
        </label>
        <label style={{ fontSize: theme.fontSize.micro, color: theme.muted, display: "flex", alignItems: "center", gap: 3 }}>
          Max P/E
          <input
            value={maxPEInput}
            onChange={(e) => setMaxPEInput(e.target.value)}
            placeholder="—"
            style={inputStyle}
          />
        </label>
        <label style={{ fontSize: theme.fontSize.micro, color: theme.muted, display: "flex", alignItems: "center", gap: 3 }}>
          Min Div%
          <input
            value={minDivInput}
            onChange={(e) => setMinDivInput(e.target.value)}
            placeholder="—"
            style={inputStyle}
          />
        </label>
        <label style={{ fontSize: theme.fontSize.micro, color: theme.muted, display: "flex", alignItems: "center", gap: 3 }}>
          Min Z
          <input
            value={minZInput}
            onChange={(e) => setMinZInput(e.target.value)}
            placeholder="—"
            style={inputStyle}
          />
        </label>
        <button
          onClick={apply}
          style={{
            background: theme.accent,
            color: theme.bg,
            border: "none",
            borderRadius: 4,
            padding: "2px 8px",
            fontSize: theme.fontSize.micro,
            fontWeight: 700,
            cursor: "pointer",
            letterSpacing: "0.04em",
          }}
        >
          APPLY
        </button>
      </div>

      {loading && !d && <PanelState kind="loading" />}
      {error && <PanelState kind="error" message={error} onRetry={() => setRetryKey((k) => k + 1)} />}

      {/* results table */}
      <div tabIndex={0} style={{ flex: 1, minHeight: 0, overflowY: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr>
              <th
                style={{
                  ...thStyle,
                  textAlign: "left",
                  paddingLeft: 8,
                  position: "sticky",
                  left: 0,
                  zIndex: 2,
                }}
              >
                SYMBOL
              </th>
              <th style={thStyle}>P/E</th>
              <th style={thStyle}>DIV%</th>
              <th style={thStyle}>F</th>
              <th style={thStyle}>Z</th>
              <th style={{ ...thStyle, paddingRight: 8 }}>M</th>
            </tr>
          </thead>
          <tbody>
            {sorted.map((row) => (
              <tr key={row.symbol}>
                <td
                  style={{
                    fontSize: theme.fontSize.micro,
                    color: theme.text,
                    paddingLeft: 8,
                    paddingRight: 6,
                    paddingBottom: 3,
                    whiteSpace: "nowrap",
                    position: "sticky",
                    left: 0,
                    background: theme.panel,
                  }}
                  title={row.name + (row.sector ? ` · ${row.sector}` : "")}
                >
                  {row.symbol}
                </td>
                <td
                  className="num"
                  style={{
                    fontSize: theme.fontSize.micro,
                    color: theme.muted,
                    textAlign: "right",
                    paddingRight: 6,
                    paddingBottom: 3,
                  }}
                >
                  {fmtOpt(row.pe)}
                </td>
                <td
                  className="num"
                  style={{
                    fontSize: theme.fontSize.micro,
                    color: row.div_yield !== null && row.div_yield > 0 ? theme.up : theme.muted,
                    textAlign: "right",
                    paddingRight: 6,
                    paddingBottom: 3,
                  }}
                >
                  {row.div_yield !== null ? `${row.div_yield.toFixed(2)}%` : "—"}
                </td>
                <td
                  className="num"
                  style={{
                    fontSize: theme.fontSize.micro,
                    color: fScoreColor(row.f_score),
                    textAlign: "right",
                    paddingRight: 6,
                    paddingBottom: 3,
                    fontWeight: row.f_score !== null && (row.f_score >= 7 || row.f_score <= 2) ? 700 : 400,
                  }}
                >
                  {row.f_score !== null ? row.f_score.toFixed(0) : "—"}
                </td>
                <td
                  className="num"
                  style={{
                    fontSize: theme.fontSize.micro,
                    color: zScoreColor(row.z_score),
                    textAlign: "right",
                    paddingRight: 6,
                    paddingBottom: 3,
                  }}
                >
                  {fmtOpt(row.z_score)}
                </td>
                <td
                  className="num"
                  style={{
                    fontSize: theme.fontSize.micro,
                    color: mScoreColor(row.m_score),
                    textAlign: "right",
                    paddingRight: 8,
                    paddingBottom: 3,
                  }}
                >
                  {fmtOpt(row.m_score, 2)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

registerWidget({
  id: "research-screen",
  title: "Stock Screener",
  component: Screener,
  defaultSize: { w: 5, h: 16 },
  minSize: { w: 4, h: 8 },
  defaultConfig: {},
  category: "Equity Research",
  description:
    "Screen the F0 universe by F-Score, P/E, dividend yield, and Z-Score; sorted by F-Score",
});
