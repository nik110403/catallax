// Technical Scanner (Phase 11, Quant/ML Lab). Synchronous pattern scanner:
// toggle pattern chips, optionally emit hits to the signal store, hit SCAN.
// Results table sorted by |score| (server-side). Mirrors Screener's Apply-button
// useAsync pattern.
import { useEffect, useRef, useState } from "react";

import { api } from "../api/client";
import type { Envelope, ScanReport, ScanRow } from "../api/types";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import { Provenance } from "./Provenance";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";

function fmtClose(v: number | null): string {
  if (v === null) return "—";
  if (v >= 1000) return v.toFixed(0);
  if (v >= 10) return v.toFixed(2);
  return v.toFixed(4);
}

// ── Pattern chips for a scan row ────────────────────────────────────────────

function PatternChips({ row }: { row: ScanRow }) {
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 3 }}>
      {row.patterns.map((p) => (
        <span
          key={p.name}
          title={p.detail}
          style={{
            fontSize: theme.fontSize.micro,
            padding: "1px 4px",
            borderRadius: 3,
            border: `1px solid ${p.bias >= 0 ? theme.up : theme.down}`,
            color: p.bias >= 0 ? theme.up : theme.down,
            whiteSpace: "nowrap",
            letterSpacing: "0.03em",
          }}
        >
          {p.label}
        </span>
      ))}
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

function Scanner() {
  const enginesAsync = useAsync(() => api.labEngines(), []);
  const allPatterns = enginesAsync.data?.scan_patterns ?? [];

  // Initialize selected names once when patterns load
  const [selectedNames, setSelectedNames] = useState<Set<string>>(new Set());
  const initializedRef = useRef(false);
  useEffect(() => {
    if (!initializedRef.current && allPatterns.length > 0) {
      initializedRef.current = true;
      setSelectedNames(new Set(allPatterns.map((p) => p.name)));
    }
  }, [allPatterns]);

  const [emit, setEmit] = useState(false);

  // Applied state drives useAsync re-fetch (new object reference = new fetch).
  // On mount: patterns=undefined → backend defaults to all patterns.
  const [applied, setApplied] = useState<{
    patterns: string[] | undefined;
    emit: boolean;
  }>({ patterns: undefined, emit: false });

  // Bumping retryKey re-runs useAsync's fetch — it's already part of the
  // hook's public contract (any deps change re-triggers the effect), so
  // Retry needs no change to useAsync.ts itself.
  const [retryKey, setRetryKey] = useState(0);
  const { data: env, loading, error } = useAsync(
    () => api.labScan(applied.patterns, undefined, applied.emit || undefined),
    [applied, retryKey],
  );

  const d = env?.data;

  const togglePattern = (name: string) => {
    setSelectedNames((prev) => {
      const next = new Set(prev);
      if (next.has(name)) next.delete(name);
      else next.add(name);
      return next;
    });
  };

  const handleScan = () => {
    const isAll =
      allPatterns.length === 0 || selectedNames.size === allPatterns.length;
    setApplied({
      patterns: isAll ? undefined : [...selectedNames],
      emit,
    });
  };

  const thStyle: React.CSSProperties = {
    fontSize: theme.fontSize.micro,
    color: theme.muted,
    fontWeight: 400,
    textAlign: "right" as const,
    paddingRight: 6,
    paddingBottom: 4,
    whiteSpace: "nowrap",
    position: "sticky",
    top: 0,
    background: theme.panelHeader,
    zIndex: 1,
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        deck={deckLine("lab-scanner")}
        agate={
          <>
            {d && (
              <span className="num" style={{ color: theme.accent }}>
                {d.n_hits}/{d.n_symbols} hits
              </span>
            )}
            {d && d.emitted > 0 && <span style={{ color: theme.up }}>+{d.emitted} emitted</span>}
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

      {/* controls */}
      <div
        style={{
          display: "flex",
          flexWrap: "wrap",
          gap: 5,
          padding: "5px 8px",
          borderBottom: `1px solid ${theme.border}`,
          alignItems: "center",
        }}
      >
        {/* pattern toggle chips */}
        {allPatterns.length === 0 ? (
          <span style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>loading patterns…</span>
        ) : (
          allPatterns.map((p) => {
            const on = selectedNames.has(p.name);
            return (
              <button
                key={p.name}
                onClick={() => togglePattern(p.name)}
                style={{
                  fontSize: theme.fontSize.micro,
                  padding: "1px 5px",
                  borderRadius: 3,
                  border: `1px solid ${on ? theme.accent : theme.border}`,
                  background: on ? "rgba(240,136,62,0.12)" : "transparent",
                  color: on ? theme.accent : theme.muted,
                  cursor: "pointer",
                  letterSpacing: "0.03em",
                  font: "inherit",
                }}
              >
                {p.label}
              </button>
            );
          })
        )}

        <span style={{ flex: 1 }} />

        {/* emit checkbox */}
        <label
          style={{
            display: "inline-flex",
            alignItems: "center",
            gap: 4,
            fontSize: theme.fontSize.micro,
            color: theme.muted,
            cursor: "pointer",
          }}
        >
          <input
            type="checkbox"
            checked={emit}
            onChange={(e) => setEmit(e.target.checked)}
          />
          EMIT
        </label>

        {/* scan button */}
        <button
          onClick={handleScan}
          style={{
            padding: "2px 8px",
            background: theme.panelHeader,
            color: theme.accent,
            border: `1px solid ${theme.accent}`,
            borderRadius: 4,
            cursor: "pointer",
            fontSize: theme.fontSize.micro,
            fontWeight: 700,
            letterSpacing: "0.04em",
          }}
        >
          SCAN
        </button>
      </div>

      {loading && !d && <PanelState kind="loading" message="scanning…" />}
      {error && <PanelState kind="error" message={error} onRetry={() => setRetryKey((k) => k + 1)} />}

      {/* results table */}
      <div tabIndex={0} style={{ flex: 1, minHeight: 0, overflowY: "auto" }}>
        {d && d.rows.length === 0 && (
          <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, padding: "8px 10px" }}>
            no hits — try different patterns or a broader universe.
          </div>
        )}
        {d && d.rows.length > 0 && (
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
                <th style={{ ...thStyle, textAlign: "left", paddingLeft: 0 }}>
                  NAME
                </th>
                <th style={thStyle}>CLOSE</th>
                <th style={thStyle}>SCORE</th>
                <th style={{ ...thStyle, textAlign: "left", paddingRight: 8 }}>
                  PATTERNS
                </th>
              </tr>
            </thead>
            <tbody>
              {d.rows.map((row: ScanRow) => (
                <tr key={row.symbol}>
                  <td
                    style={{
                      fontSize: theme.fontSize.micro,
                      color: theme.text,
                      paddingLeft: 8,
                      paddingRight: 6,
                      paddingBottom: 4,
                      whiteSpace: "nowrap",
                      fontWeight: 600,
                      position: "sticky",
                      left: 0,
                      background: theme.panel,
                    }}
                  >
                    {row.symbol}
                  </td>
                  <td
                    style={{
                      fontSize: theme.fontSize.micro,
                      color: theme.muted,
                      paddingRight: 6,
                      paddingBottom: 4,
                      whiteSpace: "nowrap",
                      maxWidth: 120,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                    }}
                    title={row.label}
                  >
                    {row.label}
                  </td>
                  <td
                    className="num"
                    style={{
                      fontSize: theme.fontSize.micro,
                      color: theme.text,
                      textAlign: "right",
                      paddingRight: 6,
                      paddingBottom: 4,
                      whiteSpace: "nowrap",
                    }}
                  >
                    {fmtClose(row.close)}
                  </td>
                  <td
                    className="num"
                    style={{
                      fontSize: theme.fontSize.micro,
                      color: row.score >= 0 ? theme.up : theme.down,
                      textAlign: "right",
                      paddingRight: 6,
                      paddingBottom: 4,
                      whiteSpace: "nowrap",
                      fontWeight: Math.abs(row.score) >= 2 ? 700 : 400,
                    }}
                  >
                    {row.score >= 0 ? "+" : ""}
                    {row.score.toFixed(1)}
                  </td>
                  <td style={{ paddingRight: 8, paddingBottom: 4 }}>
                    <PatternChips row={row} />
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}

registerWidget({
  id: "lab-scanner",
  title: "Technical Scanner",
  component: Scanner,
  defaultSize: { w: 7, h: 15 },
  minSize: { w: 4, h: 10 },
  defaultConfig: {},
  category: "Quant Lab",
  description:
    "Synchronous technical pattern scanner: toggle patterns, scan the lab universe, optionally emit hits to the signal store",
});
