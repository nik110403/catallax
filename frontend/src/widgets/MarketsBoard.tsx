import { useState } from "react";

import { api } from "../api/client";
import type { MarketGroup, MarketQuote } from "../api/types";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import { controlStyle } from "./chartTheme";
import type { WidgetProps } from "./registry";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";

interface BoardConfig {
  group: string; // group key or "all"
}

// Sticky header so the column labels stay put while the board scrolls.
const thBase: React.CSSProperties = {
  padding: "3px 8px",
  fontWeight: 400,
  position: "sticky",
  top: 0,
  background: theme.panel,
};
const thL: React.CSSProperties = { ...thBase, textAlign: "left" };
const thR: React.CSSProperties = { ...thBase, textAlign: "right" };
const thChg: React.CSSProperties = { ...thR, minWidth: 56 };
const tdR: React.CSSProperties = { textAlign: "right", padding: "2px 8px", whiteSpace: "nowrap" };
const tdChg: React.CSSProperties = { ...tdR, minWidth: 56 };

const pct = (n: number) => `${n > 0 ? "+" : ""}${n.toFixed(2)}%`;
const colorFor = (n: number) => (n > 0 ? theme.up : n < 0 ? theme.down : theme.muted);

function Rows({ items }: { items: MarketQuote[] }) {
  return (
    <>
      {items.map((q) => (
        // Yahoo/CoinGecko symbol on hover (title) instead of an inline sub-label.
        <tr
          key={q.symbol}
          className="kv"
          style={{ display: "table-row", border: "none" }}
          title={q.symbol}
        >
          <td style={{ padding: "2px 8px", fontWeight: 600 }}>{q.label}</td>
          <td className="num" style={tdR}>
            {q.price}
          </td>
          <td className="num" style={{ ...tdChg, color: colorFor(q.change_pct) }}>
            {pct(q.change_pct)}
          </td>
        </tr>
      ))}
    </>
  );
}

// Cross-asset markets board. Shows one instrument group, or every group
// stacked, from the Redis-cached `/markets/overview` blob.
function MarketsBoard({ config, onConfigChange }: WidgetProps<BoardConfig>) {
  const sel = config.group ?? "indices";
  // Bumping retryKey re-runs useAsync's fetch — it's already part of the
  // hook's public contract (any deps change re-triggers the effect), so
  // Retry needs no change to useAsync.ts itself.
  const [retryKey, setRetryKey] = useState(0);
  const { data, loading, error } = useAsync(() => api.marketsOverview(), [retryKey], 60_000);
  if (loading && !data) return <PanelState kind="loading" />;
  if (error) return <PanelState kind="error" message={error} onRetry={() => setRetryKey((k) => k + 1)} />;

  const groups: MarketGroup[] = data?.groups ?? [];
  const shown = sel === "all" ? groups : groups.filter((g) => g.key === sel);

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead deck={deckLine("markets-board")}>
        <select
          value={sel}
          onChange={(e) => onConfigChange({ ...config, group: e.target.value })}
          style={controlStyle}
          aria-label="markets group filter"
        >
          <option value="all">All</option>
          {groups.map((g) => (
            <option key={g.key} value={g.key}>
              {g.label}
            </option>
          ))}
        </select>
      </PanelHead>
      <div tabIndex={0} style={{ flex: 1, minHeight: 0, overflowY: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ color: theme.muted, fontSize: theme.fontSize.micro }}>
              <th style={thL}>NAME</th>
              <th style={thR}>LAST</th>
              <th style={thChg}>CHG%</th>
            </tr>
          </thead>
          {shown.map((g) => (
            <tbody key={g.key}>
              <tr>
                <td
                  colSpan={3}
                  style={{
                    padding: "6px 8px 2px",
                    color: theme.muted,
                    fontSize: theme.fontSize.micro,
                    letterSpacing: "0.06em",
                    borderBottom: `1px solid ${theme.border}`,
                  }}
                >
                  {g.label.toUpperCase()}
                </td>
              </tr>
              <Rows items={g.items} />
            </tbody>
          ))}
        </table>
        {!shown.length && <PanelState kind="empty" message="no data" />}
      </div>
    </div>
  );
}

registerWidget<BoardConfig>({
  id: "markets-board",
  title: "Markets",
  component: MarketsBoard,
  defaultSize: { w: 5, h: 10 },
  minSize: { w: 3, h: 5 },
  defaultConfig: { group: "all" },
});
