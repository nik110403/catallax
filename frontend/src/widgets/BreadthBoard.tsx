// Market Breadth (Phase 10a v1.2). Two stacked sections:
// 1) Per-market snapshot table: advances/declines, breadth%, net H−L,
//    up/down volume ratio, TRIN — one row per market, primary emphasised.
// 2) A/D line sparkline + McClellan oscillator/summation (guarded by
//    warming_up flag while < 40 sessions are persisted).
import { Line, LineChart, ResponsiveContainer } from "recharts";
import { useState } from "react";

import { api } from "../api/client";
import type { BreadthMarket } from "../api/types";
import { NOT_IN_SNAPSHOT, useDemoMode } from "../demo";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import { Provenance } from "./Provenance";
import { useAsync } from "./useAsync";

// ── helpers ──────────────────────────────────────────────────────────────────

function fmtPct(v: number | null, dp = 1): string {
  return v === null ? "—" : `${v.toFixed(dp)}%`;
}

function fmtNum(v: number | null, dp = 2): string {
  return v === null ? "—" : v.toFixed(dp);
}

/** breadth_pct >50 = more advancers (green), <50 = more decliners (red). */
function breadthColor(v: number | null): string {
  if (v === null) return theme.muted;
  return v >= 50 ? theme.up : theme.down;
}

/** net_highs: positive green, negative red. */
function signedColor(v: number | null): string {
  if (v === null) return theme.muted;
  return v >= 0 ? theme.up : theme.down;
}

/** up/down volume ratio: >1 = up-volume dominates (green), <1 = down-volume (red). */
function udVolColor(v: number | null): string {
  if (v === null) return theme.muted;
  return v >= 1 ? theme.up : theme.down;
}

/** TRIN (Arms): <1 = buying pressure (green), >1 = selling (red); ~1 neutral band. */
function trinColor(v: number | null): string {
  if (v === null) return theme.muted;
  if (v < 0.9) return theme.up;
  if (v > 1.1) return theme.down;
  return theme.muted;
}

// ── sub-components ────────────────────────────────────────────────────────────

const TABLE_HEADERS = ["MARKET", "A/D", "BREADTH%", "NET H−L", "U/D VOL", "TRIN"];

function BreadthRow({ row, isPrimary }: { row: BreadthMarket; isPrimary: boolean }) {
  const adStr =
    row.advances !== null && row.declines !== null
      ? `${row.advances}/${row.declines}`
      : "—";
  const netHStr =
    row.net_highs !== null
      ? row.net_highs >= 0
        ? `+${row.net_highs}`
        : `${row.net_highs}`
      : "—";
  return (
    <tr>
      <td
        style={{
          fontSize: theme.fontSize.micro,
          color: isPrimary ? theme.text : theme.muted,
          fontWeight: isPrimary ? 600 : 400,
          paddingRight: 6,
          paddingBottom: 3,
          paddingLeft: isPrimary ? 4 : 0,
          borderLeft: isPrimary ? `2px solid ${theme.accent}` : "2px solid transparent",
          whiteSpace: "nowrap",
        }}
      >
        {row.market}
      </td>
      <td
        className="num"
        style={{ fontSize: theme.fontSize.micro, color: theme.muted, textAlign: "right", paddingRight: 6, paddingBottom: 3 }}
      >
        {adStr}
      </td>
      <td
        className="num"
        style={{
          fontSize: theme.fontSize.micro,
          color: breadthColor(row.breadth_pct),
          textAlign: "right",
          paddingRight: 6,
          paddingBottom: 3,
        }}
      >
        {fmtPct(row.breadth_pct)}
      </td>
      <td
        className="num"
        style={{
          fontSize: theme.fontSize.micro,
          color: signedColor(row.net_highs),
          textAlign: "right",
          paddingRight: 6,
          paddingBottom: 3,
        }}
      >
        {netHStr}
      </td>
      <td
        className="num"
        style={{ fontSize: theme.fontSize.micro, color: udVolColor(row.updown_vol_ratio), textAlign: "right", paddingRight: 6, paddingBottom: 3 }}
      >
        {fmtNum(row.updown_vol_ratio)}
      </td>
      <td
        className="num"
        style={{ fontSize: theme.fontSize.micro, color: trinColor(row.trin), textAlign: "right", paddingBottom: 3 }}
      >
        {fmtNum(row.trin)}
      </td>
    </tr>
  );
}

// ── main widget ───────────────────────────────────────────────────────────────

export function BreadthBoard() {
  const demo = useDemoMode();
  // Bumping retryKey re-runs useAsync's fetch — it's already part of the
  // hook's public contract (any deps change re-triggers the effect), so
  // Retry needs no change to useAsync.ts itself.
  const [retryKey, setRetryKey] = useState(0);
  const { data: env, loading, error } = useAsync(() => api.marketBreadth(), [retryKey], 3_600_000);
  if (loading && !env) return <PanelState kind="loading" />;
  if (error) return <PanelState kind="error" message={error} onRetry={() => setRetryKey((k) => k + 1)} />;
  const d = env?.data;
  if (!d) return <PanelState kind="empty" message="no data" />;

  const shownMarkets = d.markets.filter((row) => !demo || row.issues !== null || row.breadth_pct !== null);
  const adSparkData = d.ad_line.filter((p) => p.value !== null);
  const oscColor =
    d.mcclellan_osc === null ? theme.muted : d.mcclellan_osc >= 0 ? theme.up : theme.down;
  const sumColor =
    d.mcclellan_summation === null
      ? theme.muted
      : d.mcclellan_summation >= 0
        ? theme.up
        : theme.down;

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        deck={deckLine("market-breadth")}
        agate={
          env && (
            <Provenance
              source={env.source}
              cacheHit={env.cache_hit}
              stale={env.stale}
              warnings={env.warnings}
              fetchedAt={env.fetched_at}
            />
          )
        }
      />

      <div
        tabIndex={0}
        style={{
          flex: 1,
          minHeight: 0,
          overflowY: "auto",
          padding: "6px 8px",
          display: "flex",
          flexDirection: "column",
          gap: 10,
        }}
      >
        {/* ── Section 1: per-market breadth table ── */}
        <div>
          <div
            style={{
              fontSize: theme.fontSize.micro,
              color: theme.muted,
              letterSpacing: "0.05em",
              marginBottom: 2,
            }}
          >
            SNAPSHOT{d.session_date ? ` · ${d.session_date}` : ""} · whole-market
          </div>
          {/* maxWidth so the 6-col table stays tight on a full-width widget span */}
          {demo && shownMarkets.length === 0 ? <PanelState kind="empty" message={NOT_IN_SNAPSHOT} fill /> : (
          <table style={{ width: "100%", maxWidth: 560, borderCollapse: "collapse" }}>
            <thead>
              <tr>
                {TABLE_HEADERS.map((h, i) => (
                  <th
                    key={h}
                    style={{
                      fontSize: theme.fontSize.micro,
                      color: theme.muted,
                      fontWeight: 400,
                      textAlign: i === 0 ? "left" : "right",
                      paddingRight: i < TABLE_HEADERS.length - 1 ? 6 : 0,
                      paddingBottom: 4,
                    }}
                  >
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {shownMarkets.map((row) => (
                <BreadthRow
                  key={row.market}
                  row={row}
                  isPrimary={row.market === d.primary_market}
                />
              ))}
            </tbody>
          </table>
)}
        </div>

        {/* ── Section 2: A/D line + McClellan ── */}
        <div style={{ borderTop: `1px solid ${theme.border}`, paddingTop: 8 }}>
          <div
            style={{
              fontSize: theme.fontSize.micro,
              color: theme.muted,
              letterSpacing: "0.05em",
              marginBottom: 5,
            }}
          >
            A/D LINE &amp; MCCLELLAN
          </div>
          {d.warming_up ? (
            <div style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>
              Breadth oscillators warming up —{" "}
              <span className="num" style={{ color: theme.accent }}>
                {d.history_days}/40
              </span>{" "}
              sessions
            </div>
          ) : (
            <>
              {adSparkData.length > 1 && (
                <div style={{ height: 44, marginBottom: 6 }}>
                  <ResponsiveContainer width="100%" height="100%">
                    <LineChart
                      data={adSparkData}
                      margin={{ top: 2, right: 2, bottom: 2, left: 2 }}
                    >
                      <Line
                        type="monotone"
                        dataKey="value"
                        stroke={theme.accent}
                        strokeWidth={1}
                        dot={false}
                        isAnimationActive={false}
                      />
                    </LineChart>
                  </ResponsiveContainer>
                </div>
              )}
              <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
                <div>
                  <span style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>McClellan Osc </span>
                  <span className="num" style={{ fontSize: theme.fontSize.micro, color: oscColor }}>
                    {d.mcclellan_osc !== null ? d.mcclellan_osc.toFixed(1) : "—"}
                  </span>
                </div>
                <div>
                  <span style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>Summation </span>
                  <span className="num" style={{ fontSize: theme.fontSize.micro, color: sumColor }}>
                    {d.mcclellan_summation !== null ? d.mcclellan_summation.toFixed(1) : "—"}
                  </span>
                </div>
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
