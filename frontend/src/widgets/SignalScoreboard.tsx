// Signal scoreboard (Phase 9 — the signal self-scoring core). Leaderboard of
// logged signal families graded against realised forward returns COMPUTED from the
// F0 daily-history backbone. Primary sort = ICIR (the information ratio of the
// cross-sectional Rank-IC series). Honest by construction: only matured + settled
// forward windows are graded, significance (★) is withheld on thin panels, and the
// survivorship / lumpy-universe caveats ride the provenance dot.
import { useState } from "react";

import { api } from "../api/client";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import { Provenance } from "./Provenance";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";

const num = (v: number | null | undefined, dp = 2) =>
  v === null || v === undefined ? "—" : v.toFixed(dp);
const signed = (v: number | null | undefined, dp = 1) =>
  v === null || v === undefined ? "—" : `${v > 0 ? "+" : ""}${v.toFixed(dp)}%`;
const col = (v: number | null | undefined) =>
  v === null || v === undefined ? theme.muted : v > 0 ? theme.up : theme.down;

function Th({ children, w }: { children: React.ReactNode; w?: number }) {
  return (
    <th style={{ textAlign: "right", fontWeight: 400, color: theme.muted, fontSize: theme.fontSize.micro, padding: "2px 6px", letterSpacing: "0.04em", width: w }}>
      {children}
    </th>
  );
}

function SignalScoreboard() {
  // Bumping retryKey re-runs useAsync's fetch — it's already part of the
  // hook's public contract (any deps change re-triggers the effect), so
  // Retry needs no change to useAsync.ts itself.
  const [retryKey, setRetryKey] = useState(0);
  const { data: env, loading, error } = useAsync(() => api.signalScoreboard(), [retryKey], 3_600_000);
  const [seeding, setSeeding] = useState(false);
  const [seedMsg, setSeedMsg] = useState<string | null>(null);

  const seed = async () => {
    setSeeding(true);
    setSeedMsg(null);
    try {
      const r = await api.signalSeed();
      setSeedMsg(`seeded ${r.data.n_written} rows — reloading…`);
      // grades recompute lazily; a refetch picks them up.
      setTimeout(() => window.location.reload(), 900);
    } catch (e) {
      setSeedMsg(e instanceof Error ? e.message : "seed failed");
      setSeeding(false);
    }
  };

  if (loading && !env) return <PanelState kind="loading" message="grading signals…" skeleton="table" />;
  if (error) return <PanelState kind="error" message={error} onRetry={() => setRetryKey((k) => k + 1)} />;
  const board = env?.data;
  const rows = board?.rows ?? [];
  // Canonical hero: the top-ranked signal's ICIR — the leaderboard's own
  // primary sort key.
  // figureColor reuses the same up/down quality law the table already
  // applies to every ICIR cell (col()), so hero and table agree.
  const top = rows[0];

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        figure={top ? num(top.icir) : undefined}
        figureColor={top ? col(top.icir) : undefined}
        unit={top?.signal_id}
        deck={deckLine("signal-scoreboard")}
        agate={
          <>
            <span style={{ letterSpacing: "0.04em" }}>
              {board?.horizon_bars ?? 21}-BAR FWD{board ? ` · Q${board.quantiles}` : ""}
            </span>
            {env && <Provenance source={env.source} stale={env.stale} warnings={env.warnings} fetchedAt={env.fetched_at} />}
          </>
        }
      />

      <div tabIndex={0} style={{ flex: 1, minHeight: 0, overflowY: "auto" }}>
        {rows.length === 0 ? (
          <PanelState kind="empty" message="No signals logged yet." guidance={deckLine("signal-scoreboard")} fill>
            <div style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 10, marginTop: 10 }}>
              <button
                onClick={seed}
                disabled={seeding}
                style={{ padding: "5px 12px", background: theme.panelHeader, color: theme.accent, border: `1px solid ${theme.accent}`, borderRadius: 4, cursor: seeding ? "default" : "pointer", fontSize: theme.fontSize.micro }}
              >
                {seeding ? "seeding…" : "Seed reference factors"}
              </button>
              <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, maxWidth: 280, lineHeight: 1.4 }}>
                Computes 12-1 momentum + a low-vol factor over your portfolio, then grades them vs realised forward returns.
              </div>
              {seedMsg && <div style={{ fontSize: theme.fontSize.micro, color: theme.text }}>{seedMsg}</div>}
            </div>
          </PanelState>
        ) : (
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: theme.fontSize.micro }}>
            <thead>
              <tr style={{ borderBottom: `1px solid ${theme.grid}` }}>
                <th style={{ textAlign: "left", fontWeight: 400, color: theme.muted, fontSize: theme.fontSize.micro, padding: "2px 6px", letterSpacing: "0.04em" }}>SIGNAL</th>
                <Th>ICIR</Th>
                <Th>RANK-IC</Th>
                <Th>t</Th>
                <Th>L/S CUM</Th>
                <Th>HIT</Th>
                <Th>N</Th>
              </tr>
            </thead>
            <tbody>
              {rows.map((r) => (
                <tr key={r.signal_id} style={{ borderBottom: `1px solid ${theme.grid}` }}>
                  <td style={{ padding: "3px 6px", color: theme.text }}>
                    {r.signal_id}
                    {r.low_confidence && (
                      <span title={`only ${r.n_dates_graded} graded cross-sections`} style={{ marginLeft: 5, fontSize: theme.fontSize.micro, color: theme.warn, background: "rgba(210,153,34,0.12)", border: `1px solid ${theme.warn}`, borderRadius: 3, padding: "0 3px" }}>
                        low n
                      </span>
                    )}
                  </td>
                  <td className="num" style={{ textAlign: "right", padding: "3px 6px", color: col(r.icir), fontWeight: 700 }}>{num(r.icir)}</td>
                  <td className="num" style={{ textAlign: "right", padding: "3px 6px", color: theme.muted }}>{num(r.mean_rank_ic, 3)}</td>
                  <td className="num" style={{ textAlign: "right", padding: "3px 6px", color: theme.text }}>
                    {num(r.t_stat)}
                    {r.significant && <span title="|t| ≥ 2 over a sufficient panel" style={{ color: theme.up, marginLeft: 3, fontSize: theme.fontSize.base }}>★</span>}
                  </td>
                  {/* L/S cum is neutral: its sign is factor-type-dependent, not a quality verdict (ICIR carries quality). */}
                  <td className="num" style={{ textAlign: "right", padding: "3px 6px", color: theme.text }}>{signed(r.ls_cumulative_pct)}</td>
                  <td className="num" style={{ textAlign: "right", padding: "3px 6px", color: theme.muted }}>
                    {r.hit_rate === null ? "—" : `${(r.hit_rate * 100).toFixed(0)}%`}
                  </td>
                  <td className="num" style={{ textAlign: "right", padding: "3px 6px", color: theme.muted }}>{r.n_pairs_matured}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>

      {rows.length > 0 && (
        <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, padding: "5px 8px", borderTop: `1px solid ${theme.border}`, lineHeight: 1.4 }}>
          Cross-sectional Rank-IC graded on matured, settled forward windows (entry = next bar after T). ★ = |t| ≥ 2.
          {board && board.warnings.length > 0 && ` · ${board.warnings.length} caveat${board.warnings.length > 1 ? "s" : ""} (hover the dot)`}
        </div>
      )}
    </div>
  );
}

registerWidget({
  id: "signal-scoreboard",
  title: "Signal Scoreboard",
  component: SignalScoreboard,
  defaultSize: { w: 7, h: 9 },
  minSize: { w: 4, h: 6 },
  defaultConfig: {},
  category: "Signals",
  description: "Leaderboard of logged signals graded by Rank-IC / ICIR vs realised forward returns",
});
