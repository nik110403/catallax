// Forensic Score Cards (Phase 10b). Piotroski F-Score, Altman Z-Score, Beneish
// M-Score — each as a stacked card: big score + verdict badge + scale hint +
// component checklist. Symbol configurable.
import { useEffect, useState } from "react";

import { api } from "../api/client";
import type { ScoreCard as ScoreCardData } from "../api/types";
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

/** Map a verdict string to its badge colour. */
function verdictColor(verdict: string | null): string {
  if (!verdict) return theme.muted;
  const v = verdict.toLowerCase();
  if (v.includes("strong") || v.includes("safe") || v.includes("low manipulation"))
    return theme.up;
  if (v.includes("distress") || v.includes("elevated manipulation")) return theme.down;
  return theme.accent; // grey zone / moderate / incomplete data
}

function Card({ card }: { card: ScoreCardData }) {
  const badgeColor = verdictColor(card.verdict);
  return (
    <div
      style={{
        borderTop: `1px solid ${theme.border}`,
        paddingTop: 8,
        paddingBottom: 4,
      }}
    >
      {/* score row */}
      <div
        style={{
          display: "flex",
          alignItems: "baseline",
          gap: 8,
          marginBottom: 2,
        }}
      >
        <span
          className="num"
          style={{ fontSize: theme.fontSize.xl, fontWeight: 700, color: badgeColor, lineHeight: 1 }}
        >
          {card.score !== null ? card.score.toFixed(card.key === "altman" ? 2 : 0) : "—"}
        </span>
        <span
          style={{
            fontSize: theme.fontSize.micro,
            color: theme.text,
            fontWeight: 600,
            letterSpacing: "0.05em",
          }}
        >
          {card.label.toUpperCase()}
        </span>
        {card.verdict && (
          <span
            style={{
              fontSize: theme.fontSize.micro,
              color: badgeColor,
              border: `1px solid ${badgeColor}`,
              borderRadius: 3,
              padding: "1px 5px",
              letterSpacing: "0.04em",
            }}
          >
            {card.verdict}
          </span>
        )}
      </div>
      {/* scale hint */}
      <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, marginBottom: 6 }}>{card.scale}</div>
      {/* component rows */}
      {card.components.map((c) => {
        const check =
          c.passed === true ? "✓" : c.passed === false ? "✗" : "–";
        const checkColor =
          c.passed === true ? theme.up : c.passed === false ? theme.down : theme.muted;
        return (
          <div
            key={c.key}
            style={{
              display: "flex",
              alignItems: "flex-start",
              gap: 5,
              marginBottom: 3,
            }}
          >
            <span
              className="num"
              style={{ fontSize: theme.fontSize.micro, color: checkColor, flexShrink: 0, width: 10 }}
            >
              {check}
            </span>
            <span style={{ fontSize: theme.fontSize.micro, color: theme.text, flex: 1 }}>{c.label}</span>
            <span
              style={{
                fontSize: theme.fontSize.micro,
                color: theme.muted,
                textAlign: "right",
                flexShrink: 0,
                maxWidth: 120,
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
              }}
              title={c.detail}
            >
              {c.detail}
            </span>
          </div>
        );
      })}
    </div>
  );
}

function ScoreCard({ config, onConfigChange }: WidgetProps<Config>) {
  const [symInput, setSymInput] = useState(config.symbol);
  useEffect(() => setSymInput(config.symbol), [config.symbol]);

  // Bumping retryKey re-runs useAsync's fetch — it's already part of the
  // hook's public contract (any deps change re-triggers the effect), so
  // Retry needs no change to useAsync.ts itself.
  const [retryKey, setRetryKey] = useState(0);
  const { data: env, loading, error } = useAsync(
    () => api.researchScores(config.symbol),
    [config.symbol, retryKey],
  );

  const d = env?.data;

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        deck={deckLine("research-scorecard")}
        agate={
          (d || env) && (
            <>
              {d && <span style={{ color: theme.text, fontWeight: 600 }}>{d.name}</span>}
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
          {d.cards.map((card) => (
            <Card key={card.key} card={card} />
          ))}
          {d.warnings.length > 0 && (
            <div
              style={{
                marginTop: 8,
                fontSize: theme.fontSize.micro,
                color: theme.accent,
                borderTop: `1px solid ${theme.border}`,
                paddingTop: 6,
              }}
            >
              {d.warnings.map((w, i) => (
                <div key={i}>{w}</div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

registerWidget({
  id: "research-scorecard",
  title: "Forensic Scores",
  component: ScoreCard,
  defaultSize: { w: 5, h: 16 },
  minSize: { w: 4, h: 8 },
  defaultConfig: { symbol: "AAPL" },
  category: "Equity Research",
  description:
    "Piotroski F-Score, Altman Z-Score, and Beneish M-Score with component-level detail",
});
