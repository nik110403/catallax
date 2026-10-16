// Sentiment & Positioning (Phase 10a v1.1). Three stacked sections:
// 1) Fear & Greed gauge: big score + rating, horizontal 0-100 bar,
//    prev-period context line, and a thin recharts sparkline of 6-month history.
// 2) F&G component drivers: seven labeled 0-100 mini-bars.
// 3) COT positioning table: net, %OI, z-score ZBar, and percentile per future.
import { Line, LineChart, ResponsiveContainer } from "recharts";
import { useState } from "react";

import { api } from "../api/client";
import type { CotRow, SentimentComponent } from "../api/types";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import { Provenance } from "./Provenance";
import { useAsync } from "./useAsync";

// ── helpers ──────────────────────────────────────────────────────────────────

function fmtNet(v: number | null): string {
  if (v === null) return "—";
  const abs = Math.abs(v);
  if (abs >= 1_000_000) return `${(v / 1_000_000).toFixed(1)}M`;
  if (abs >= 1_000) return `${Math.round(v / 1_000)}k`;
  return `${v}`;
}

function fmtPct(v: number | null, dp = 1): string {
  return v === null ? "—" : `${v.toFixed(dp)}%`;
}

/** F&G score → accent color. ≤45 = fear, 45-55 = neutral, >55 = greed. */
function fgScoreColor(score: number | null): string {
  if (score === null) return theme.muted;
  if (score <= 45) return theme.down;
  if (score <= 55) return theme.muted;
  return theme.up;
}

/** F&G component mini-bar fill: low=down, mid=accent, high=up. */
function componentFillColor(score: number | null): string {
  if (score === null) return theme.grid;
  if (score <= 33) return theme.down;
  if (score <= 67) return theme.accent;
  return theme.up;
}

// ── centred z-score bar (same logic as MarketConditions ZBar). For COT,
// higherIsRiskOff=false means positive z = bullish positioning → green. ──────
function ZBar({ zscore, higherIsRiskOff = false }: { zscore: number | null; higherIsRiskOff?: boolean }) {
  if (zscore === null) {
    return <div style={{ height: 7, background: theme.grid, borderRadius: 2 }} />;
  }
  const clamped = Math.max(-3, Math.min(3, zscore));
  const pct = Math.abs(clamped) / 3; // 0..1
  const isPos = clamped >= 0;
  // Risk direction: (isPos === higherIsRiskOff) → red; otherwise green.
  const color = isPos === higherIsRiskOff ? theme.down : theme.up;
  return (
    <div
      style={{
        height: 7,
        background: theme.grid,
        borderRadius: 2,
        position: "relative",
        overflow: "hidden",
      }}
    >
      <div
        style={{
          position: "absolute",
          top: 0,
          bottom: 0,
          width: `${pct * 50}%`,
          ...(isPos ? { left: "50%" } : { right: "50%" }),
          background: color,
          borderRadius: 2,
        }}
      />
      {/* centre tick */}
      <div
        style={{
          position: "absolute",
          top: 0,
          bottom: 0,
          left: "50%",
          width: 1,
          background: theme.border,
        }}
      />
    </div>
  );
}

// ── sub-components ────────────────────────────────────────────────────────────

function FgComponentBar({ c }: { c: SentimentComponent }) {
  const w = c.score !== null ? Math.max(0, Math.min(100, c.score)) : 0;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6, minWidth: 0 }}>
      <div
        style={{
          fontSize: theme.fontSize.micro,
          color: theme.muted,
          width: 110,
          flexShrink: 0,
          whiteSpace: "nowrap",
          overflow: "hidden",
          textOverflow: "ellipsis",
        }}
      >
        {c.label}
      </div>
      <div
        style={{
          flex: 1,
          height: 5,
          background: theme.grid,
          borderRadius: 2,
          overflow: "hidden",
        }}
      >
        {c.score !== null && (
          <div
            style={{
              width: `${w}%`,
              height: "100%",
              background: componentFillColor(c.score),
              borderRadius: 2,
            }}
          />
        )}
      </div>
      <span
        className="num"
        style={{ fontSize: theme.fontSize.micro, color: theme.muted, width: 26, textAlign: "right", flexShrink: 0 }}
      >
        {c.score !== null ? c.score.toFixed(0) : "—"}
      </span>
    </div>
  );
}

function CotTableRow({ row }: { row: CotRow }) {
  // For inverse-to-risk underlyings (e.g. VIX) net-long spec = risk-off, so flip:
  // green = risk-on/bullish-for-markets, red = risk-off/bearish.
  const bullish = row.net === null ? null : row.inverse ? row.net < 0 : row.net >= 0;
  const netColor = bullish === null ? theme.muted : bullish ? theme.up : theme.down;
  return (
    <tr>
      <td
        style={{
          fontSize: theme.fontSize.micro,
          color: theme.text,
          paddingRight: 6,
          paddingBottom: 3,
          whiteSpace: "nowrap",
        }}
      >
        {row.label}
      </td>
      <td
        className="num"
        style={{
          fontSize: theme.fontSize.micro,
          color: netColor,
          textAlign: "right",
          paddingRight: 6,
          paddingBottom: 3,
        }}
      >
        {fmtNet(row.net)}
      </td>
      <td
        style={{
          fontSize: theme.fontSize.micro,
          color: theme.muted,
          textAlign: "right",
          paddingRight: 6,
          paddingBottom: 3,
        }}
      >
        {fmtPct(row.pct_oi)}
      </td>
      <td style={{ width: 96, paddingRight: 6, paddingBottom: 3 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 5 }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <ZBar zscore={row.z_1y} higherIsRiskOff={row.inverse} />
          </div>
          <span
            className="num"
            style={{ fontSize: theme.fontSize.micro, color: theme.muted, width: 24, textAlign: "right", flexShrink: 0 }}
          >
            {row.z_1y !== null ? row.z_1y.toFixed(1) : "—"}
          </span>
        </div>
      </td>
      <td
        style={{
          fontSize: theme.fontSize.micro,
          color: theme.muted,
          textAlign: "right",
          paddingBottom: 3,
        }}
      >
        {row.percentile !== null ? `${row.percentile.toFixed(0)}p` : "—"}
      </td>
    </tr>
  );
}

// ── main widget ───────────────────────────────────────────────────────────────

export function SentimentPositioning() {
  // Bumping retryKey re-runs useAsync's fetch — it's already part of the
  // hook's public contract (any deps change re-triggers the effect), so
  // Retry needs no change to useAsync.ts itself.
  const [retryKey, setRetryKey] = useState(0);
  const { data: env, loading, error } = useAsync(() => api.marketSentiment(), [retryKey], 3_600_000);
  if (loading && !env) return <PanelState kind="loading" />;
  if (error) return <PanelState kind="error" message={error} onRetry={() => setRetryKey((k) => k + 1)} />;
  const d = env?.data;
  if (!d) return <PanelState kind="empty" message="no data" />;

  const scoreColor = fgScoreColor(d.fg_score);
  const sparkData = d.fg_history.slice(-120);

  // prev-period context chips
  const prevChips = [
    d.fg_prev_close !== null ? `1D ${d.fg_prev_close.toFixed(1)}` : null,
    d.fg_prev_week !== null ? `1W ${d.fg_prev_week.toFixed(1)}` : null,
    d.fg_prev_month !== null ? `1M ${d.fg_prev_month.toFixed(1)}` : null,
    d.fg_prev_year !== null ? `1Y ${d.fg_prev_year.toFixed(1)}` : null,
  ].filter((x): x is string => x !== null);

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        figure={d.fg_score !== null ? d.fg_score.toFixed(1) : undefined}
        deck={deckLine("market-sentiment")}
        agate={
          <>
            {d.fg_rating && (
              <span style={{ color: scoreColor, textTransform: "capitalize" }}>{d.fg_rating}</span>
            )}
            <span className="num">F&G as of {d.as_of.slice(0, 10)}</span>
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
        {/* ── Section 1: Fear & Greed gauge ── */}
        <div>
          {/* horizontal 0-100 bar with gradient fill + score marker */}
          <div
            style={{
              height: 8,
              background: theme.grid,
              borderRadius: 4,
              position: "relative",
              overflow: "hidden",
              marginBottom: 4,
            }}
          >
            {/* full-width gradient track */}
            <div
              style={{
                position: "absolute",
                inset: 0,
                background: `linear-gradient(to right, ${theme.down}, ${theme.muted}, ${theme.up})`,
                opacity: 0.7,
              }}
            />
            {/* score marker (clamped so a 2px tick never clips at 0/100 under overflow:hidden) */}
            {d.fg_score !== null && (
              <div
                style={{
                  position: "absolute",
                  top: 0,
                  bottom: 0,
                  left: `clamp(0px, calc(${Math.max(0, Math.min(100, d.fg_score))}% - 1px), calc(100% - 2px))`,
                  width: 2,
                  background: theme.text,
                }}
              />
            )}
          </div>

          {/* prev-period context chips */}
          {prevChips.length > 0 && (
            <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, marginBottom: 4 }}>
              {prevChips.join(" · ")}
            </div>
          )}

          {/* sparkline */}
          {sparkData.length > 1 && (
            <div style={{ height: 44, marginBottom: 2 }}>
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={sparkData} margin={{ top: 2, right: 2, bottom: 2, left: 2 }}>
                  <Line
                    type="monotone"
                    dataKey="score"
                    stroke={theme.accent}
                    strokeWidth={1}
                    dot={false}
                    isAnimationActive={false}
                  />
                </LineChart>
              </ResponsiveContainer>
            </div>
          )}
        </div>

        {/* ── Section 2: F&G component drivers ── */}
        {d.fg_components.length > 0 && (
          <div style={{ borderTop: `1px solid ${theme.border}`, paddingTop: 8 }}>
            <div
              style={{
                fontSize: theme.fontSize.micro,
                color: theme.muted,
                letterSpacing: "0.05em",
                marginBottom: 5,
              }}
            >
              F&G DRIVERS
            </div>
            <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
              {d.fg_components.map((c) => (
                <FgComponentBar key={c.key} c={c} />
              ))}
            </div>
          </div>
        )}

        {/* ── Section 3: COT positioning table ── */}
        {d.cot.length > 0 && (
          <div style={{ borderTop: `1px solid ${theme.border}`, paddingTop: 8 }}>
            <div
              style={{
                fontSize: theme.fontSize.micro,
                color: theme.muted,
                letterSpacing: "0.05em",
                marginBottom: 4,
              }}
            >
              COT POSITIONING{d.cot_as_of ? ` · ${d.cot_as_of.slice(0, 10)}` : ""}
            </div>
            {/* maxWidth so the 5-col table stays tight on a full-width widget span */}
            <table style={{ width: "100%", maxWidth: 560, borderCollapse: "collapse" }}>
              <thead>
                <tr>
                  <th
                    style={{
                      fontSize: theme.fontSize.micro,
                      color: theme.muted,
                      fontWeight: 400,
                      textAlign: "left",
                      paddingBottom: 4,
                    }}
                  >
                    ASSET
                  </th>
                  <th
                    style={{
                      fontSize: theme.fontSize.micro,
                      color: theme.muted,
                      fontWeight: 400,
                      textAlign: "right",
                      paddingRight: 6,
                      paddingBottom: 4,
                    }}
                  >
                    NET
                  </th>
                  <th
                    style={{
                      fontSize: theme.fontSize.micro,
                      color: theme.muted,
                      fontWeight: 400,
                      textAlign: "right",
                      paddingRight: 6,
                      paddingBottom: 4,
                    }}
                  >
                    %OI
                  </th>
                  <th
                    style={{
                      fontSize: theme.fontSize.micro,
                      color: theme.muted,
                      fontWeight: 400,
                      paddingRight: 6,
                      paddingBottom: 4,
                    }}
                  >
                    Z-SCORE
                  </th>
                  <th
                    style={{
                      fontSize: theme.fontSize.micro,
                      color: theme.muted,
                      fontWeight: 400,
                      textAlign: "right",
                      paddingBottom: 4,
                    }}
                  >
                    PCT
                  </th>
                </tr>
              </thead>
              <tbody>
                {d.cot.map((row) => (
                  <CotTableRow key={row.code} row={row} />
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
