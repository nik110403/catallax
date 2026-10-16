// Cross-Asset Matrix (Phase 10a). Correlation heatmap (symbols × symbols) on a
// diverging blue→neutral→red scale, a compact snapshot strip (per-symbol returns),
// and the key ratios. Shows avg_pairwise_corr and a history sparkline.
import {
  CartesianGrid,
  Line,
  LineChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { useState } from "react";

import { api } from "../api/client";
import type { CrossAssetSnapshot, RatioItem } from "../api/types";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import { Provenance } from "./Provenance";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";

const fmtPct = (v: number | null, dp = 1): string =>
  v === null ? "—" : `${v >= 0 ? "+" : ""}${v.toFixed(dp)}%`;
const fmtNum = (v: number | null, dp = 2): string => (v === null ? "—" : v.toFixed(dp));

/** Map a correlation value −1..1 to a CSS color on a diverging blue→grey→red scale. */
function corrColor(v: number | null): string {
  if (v === null) return theme.grid;
  // clamp
  const c = Math.max(-1, Math.min(1, v));
  if (c >= 0) {
    // 0..+1: neutral → red
    const t = c;
    const r = Math.round(80 + t * (248 - 80));
    const g = Math.round(90 + t * (81 - 90));
    const b = Math.round(90 + t * (73 - 90));
    return `rgb(${r},${g},${b})`;
  } else {
    // −1..0: blue → neutral
    const t = -c; // 0..1 as we go more negative
    const r = Math.round(80 + t * (88 - 80));
    const g = Math.round(90 + t * (166 - 90));
    const b = Math.round(90 + t * (255 - 90));
    return `rgb(${r},${g},${b})`;
  }
}

function pctColor(v: number | null): string {
  if (v === null) return theme.muted;
  return v >= 0 ? theme.up : theme.down;
}

function SnapshotRow({ s }: { s: CrossAssetSnapshot }) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 6,
        fontSize: theme.fontSize.micro,
        padding: "2px 0",
        borderBottom: `1px solid ${theme.grid}`,
      }}
    >
      <span style={{ width: 72, color: theme.muted, fontSize: theme.fontSize.micro, whiteSpace: "nowrap", overflow: "hidden", textOverflow: "ellipsis" }}>
        {s.label}
      </span>
      <span className="num" style={{ width: 44, textAlign: "right", color: pctColor(s.chg_1d_pct) }}>
        {fmtPct(s.chg_1d_pct)}
      </span>
      <span className="num" style={{ width: 44, textAlign: "right", color: pctColor(s.chg_1w_pct) }}>
        {fmtPct(s.chg_1w_pct)}
      </span>
      <span className="num" style={{ width: 44, textAlign: "right", color: pctColor(s.chg_1m_pct) }}>
        {fmtPct(s.chg_1m_pct)}
      </span>
      <span className="num" style={{ width: 44, textAlign: "right", color: pctColor(s.chg_ytd_pct) }}>
        {fmtPct(s.chg_ytd_pct)}
      </span>
    </div>
  );
}

function RatioRow({ r }: { r: RatioItem }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 6, fontSize: theme.fontSize.micro, padding: "1px 0" }}>
      <span style={{ flex: 1, color: theme.muted, fontSize: theme.fontSize.micro }}>{r.label}</span>
      <span className="num" style={{ color: theme.text }}>{fmtNum(r.value)}</span>
      <span className="num" style={{ width: 50, textAlign: "right", color: pctColor(r.chg_1m_pct) }}>
        {fmtPct(r.chg_1m_pct)}
      </span>
    </div>
  );
}

function CrossAssetMatrix() {
  // Bumping retryKey re-runs useAsync's fetch — it's already part of the
  // hook's public contract (any deps change re-triggers the effect), so
  // Retry needs no change to useAsync.ts itself.
  const [retryKey, setRetryKey] = useState(0);
  const { data: env, loading, error } = useAsync(() => api.marketCrossAsset(), [retryKey], 3_600_000);
  if (loading && !env) return <PanelState kind="loading" />;
  if (error) return <PanelState kind="error" message={error} onRetry={() => setRetryKey((k) => k + 1)} />;
  const d = env?.data;
  if (!d) return <PanelState kind="empty" message="no data" />;

  const syms = d.symbols;
  const n = syms.length;

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        figure={d.avg_pairwise_corr !== null ? fmtNum(d.avg_pairwise_corr) : undefined}
        deck={deckLine("market-cross-asset")}
        agate={
          <>
            <span style={{ letterSpacing: "0.06em" }}>{d.window_days}D WINDOW</span>
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

      <div tabIndex={0} style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "6px 8px", display: "flex", flexDirection: "column" }}>
        {/* snapshot strip (least critical → flex-ordered last so the heatmap leads) */}
        <div style={{ order: 4 }}>
        {/* snapshot strip header */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 6,
            fontSize: theme.fontSize.micro,
            color: theme.muted,
            padding: "0 0 2px",
          }}
        >
          <span style={{ width: 52 }} />
          <span style={{ width: 44, textAlign: "right" }}>1D</span>
          <span style={{ width: 44, textAlign: "right" }}>1W</span>
          <span style={{ width: 44, textAlign: "right" }}>1M</span>
          <span style={{ width: 44, textAlign: "right" }}>YTD</span>
        </div>
        {d.snapshot.map((s) => (
          <SnapshotRow key={s.symbol} s={s} />
        ))}
        </div>

        {/* avg corr summary */}
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 8,
            fontSize: theme.fontSize.micro,
            color: theme.muted,
            margin: "6px 0 4px",
            order: 2,
          }}
        >
          <span>Avg pairwise corr</span>
          {d.avg_corr_history.length > 1 && (
            <div style={{ flex: 1, height: 28 }}>
              <ResponsiveContainer width="100%" height="100%">
                <LineChart
                  data={d.avg_corr_history}
                  margin={{ top: 2, right: 2, bottom: 2, left: 0 }}
                >
                  <XAxis dataKey="date" hide />
                  <YAxis domain={[-1, 1]} hide />
                  <CartesianGrid stroke={theme.grid} strokeWidth={0.5} vertical={false} />
                  <Tooltip
                    content={({ active, payload }) => {
                      if (!active || !payload?.length) return null;
                      const p = payload[0].payload as { date: string; value: number | null };
                      return (
                        <div
                          style={{
                            background: theme.panel,
                            border: `1px solid ${theme.border}`,
                            borderRadius: 3,
                            padding: "2px 6px",
                            fontSize: theme.fontSize.micro,
                          }}
                        >
                          <span style={{ color: theme.muted }}>{p.date}</span>{" "}
                          <span className="num" style={{ color: theme.accent }}>
                            {fmtNum(p.value)}
                          </span>
                        </div>
                      );
                    }}
                    wrapperStyle={{ outline: "none" }}
                  />
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
        </div>

        {/* correlation heatmap */}
        {n > 0 && (
          <div
            tabIndex={0}
            style={{
              paddingTop: 2,
              marginBottom: 6,
              overflowX: "auto",
              order: 1,
            }}
          >
            <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.05em", marginBottom: 3 }}>
              CORRELATION MATRIX
            </div>
            <table
              style={{
                borderCollapse: "collapse",
                fontSize: theme.fontSize.micro,
                tableLayout: "fixed",
              }}
            >
              <thead>
                <tr>
                  <th style={{ width: 40, color: theme.muted, fontWeight: 400 }} />
                  {syms.map((s) => (
                    <th
                      key={s}
                      style={{
                        width: 36,
                        padding: "1px 2px",
                        color: theme.muted,
                        fontWeight: 400,
                        textAlign: "center",
                        whiteSpace: "nowrap",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                      }}
                      title={s}
                    >
                      {s.length > 5 ? s.slice(0, 5) : s}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {d.matrix.map((row, ri) => (
                  <tr key={syms[ri] ?? ri}>
                    <td
                      style={{
                        padding: "1px 3px",
                        color: theme.muted,
                        whiteSpace: "nowrap",
                        overflow: "hidden",
                        textOverflow: "ellipsis",
                      }}
                      title={syms[ri]}
                    >
                      {syms[ri]?.length > 5 ? syms[ri].slice(0, 5) : syms[ri]}
                    </td>
                    {row.map((val, ci) => (
                      <td
                        key={ci}
                        className="num"
                        title={`${syms[ri]} / ${syms[ci]}: ${fmtNum(val)}`}
                        style={{
                          padding: "2px 1px",
                          textAlign: "center",
                          background: corrColor(val),
                          color: val === null ? theme.muted : theme.text,
                          fontSize: theme.fontSize.micro,
                        }}
                      >
                        {val === null ? "—" : val.toFixed(2)}
                      </td>
                    ))}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {/* ratios */}
        {d.ratios.length > 0 && (
          <div style={{ borderTop: `1px solid ${theme.grid}`, paddingTop: 5, order: 3 }}>
            <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.05em", marginBottom: 3 }}>
              RATIOS
            </div>
            {d.ratios.map((r) => (
              <RatioRow key={r.key} r={r} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

registerWidget({
  id: "market-cross-asset",
  title: "Cross-Asset",
  component: CrossAssetMatrix,
  defaultSize: { w: 7, h: 10 },
  minSize: { w: 4, h: 7 },
  defaultConfig: {},
  category: "Market Research",
  description: "Correlation heatmap + snapshot strip + key ratios across asset classes",
});
