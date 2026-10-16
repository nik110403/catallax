// Economic release calendar (Phase 8, Macro). FRED-sourced (the only free
// calendar provider in our OpenBB extension set) → needs the free
// OPENBB_FRED_API_KEY. Without it the widget renders a clear enablement note
// rather than an error. Polls like the news panel.
import { useState } from "react";

import { api } from "../api/client";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import { Provenance } from "./Provenance";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";

function EconCalendar() {
  // Bumping retryKey re-runs useAsync's fetch — it's already part of the
  // hook's public contract (any deps change re-triggers the effect), so
  // Retry needs no change to useAsync.ts itself.
  const [retryKey, setRetryKey] = useState(0);
  const { data: env, loading, error } = useAsync(() => api.macroCalendar(), [retryKey], 1_800_000);
  if (loading && !env) return <PanelState kind="loading" skeleton="table" />;
  if (error) return <PanelState kind="error" message={error} onRetry={() => setRetryKey((k) => k + 1)} />;

  const cal = env?.data;
  const events = cal?.events ?? [];

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        deck={deckLine("econ-calendar")}
        agate={
          // "needs key" is a neutral not-configured state, not a data failure —
          // suppress the (red/stale) provenance dot so it doesn't read as broken.
          // The "ECON CALENDAR" label that used to live here repeated the deck
          // ("Upcoming US economic releases…") and the panel header ("Econ
          // Calendar") — dropped.
          env && !cal?.needs_key && (
            <Provenance source={env.source} cacheHit={env.cache_hit} stale={env.stale} warnings={env.warnings} fetchedAt={env.fetched_at} />
          )
        }
      />
      <div tabIndex={0} style={{ flex: 1, minHeight: 0, overflowY: "auto" }}>
        {cal?.needs_key ? (
          <PanelState kind="empty" message="Economic calendar needs a free FRED API key." guidance={deckLine("econ-calendar")} fill>
            <span style={{ color: theme.muted, fontSize: theme.fontSize.micro, lineHeight: 1.6, marginTop: 8, display: "inline-block" }}>
              Set <code style={{ color: theme.accent }}>OPENBB_FRED_API_KEY</code> from{" "}
              <a
                href="https://fredaccount.stlouisfed.org/apikeys"
                target="_blank"
                rel="noreferrer"
                style={{ color: theme.accent }}
              >
                fredaccount.stlouisfed.org
              </a>{" "}
              to enable.
            </span>
          </PanelState>
        ) : !events.length ? (
          <PanelState kind="empty" message="no upcoming releases" guidance={deckLine("econ-calendar")} />
        ) : (
          <table style={{ width: "100%", borderCollapse: "collapse", fontSize: theme.fontSize.micro }}>
            <tbody>
              {events.map((e, i) => (
                <tr key={i} style={{ borderBottom: `1px solid ${theme.grid}` }}>
                  <td style={{ padding: "3px 6px", color: theme.muted, whiteSpace: "nowrap" }}>
                    {e.date?.slice(0, 10) ?? "—"}
                  </td>
                  <td style={{ padding: "3px 6px" }}>
                    {e.event}
                    {e.country ? <span style={{ color: theme.muted }}> · {e.country}</span> : null}
                  </td>
                  <td className="num" style={{ padding: "3px 6px", textAlign: "right", whiteSpace: "nowrap" }}>
                    {e.actual ?? e.consensus ?? "—"}
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
  id: "econ-calendar",
  title: "Econ Calendar",
  component: EconCalendar,
  defaultSize: { w: 4, h: 7 },
  minSize: { w: 3, h: 4 },
  defaultConfig: {},
  category: "Macro",
  description: "Economic release calendar (FRED — needs free key)",
});
