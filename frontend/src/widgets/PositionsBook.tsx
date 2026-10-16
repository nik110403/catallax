// Positions Book (Phase 13, Algo Hub). Live positions off the paper OMS + an
// account strip (cash / equity / day P&L) + an equity sparkline from the daily
// account snapshots — same recharts LineChart sparkline approach as Market
// Breadth's A/D line (recharts is already in the eager bundle via YieldCurve).
import { Line, LineChart, ResponsiveContainer } from "recharts";

import { api } from "../api/client";
import type { AlgoPosition } from "../api/types";
import { LOCALE } from "../format";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";

// Fixed min+max fraction digits (not just max) so numeric columns don't ragged-edge
// on decimals — aids column scanning when right-aligned.
const fmt = (v: number | null, dp = 2): string =>
  v === null || !Number.isFinite(v)
    ? "—"
    : v.toLocaleString(LOCALE, { minimumFractionDigits: dp, maximumFractionDigits: dp });

const pnlColor = (v: number | null): string => (v === null ? theme.muted : v >= 0 ? theme.up : theme.down);

const thL: React.CSSProperties = { textAlign: "left", padding: "3px 8px", fontWeight: 400 };
const thR: React.CSSProperties = { textAlign: "right", padding: "3px 8px", fontWeight: 400 };
// Row separator lives on the cells (not the <tr>) so native table layout — not
// the .kv flex-row class — drives column alignment; .kv on a <tr> collapses
// its geometry to a flex row and desyncs it from the header's table cells.
const tdL: React.CSSProperties = { padding: "2px 8px", fontWeight: 600, borderBottom: `1px solid ${theme.border}` };
const tdR: React.CSSProperties = {
  textAlign: "right",
  padding: "2px 8px",
  whiteSpace: "nowrap",
  borderBottom: `1px solid ${theme.border}`,
};

function PositionsBook() {
  const positions = useAsync(() => api.algoPositions(), [], 15_000);
  const account = useAsync(() => api.algoAccount(), [], 15_000);

  const rows: AlgoPosition[] = positions.data?.positions ?? [];
  const acct = account.data;
  const snapshots = acct?.snapshots ?? [];
  const sparkData = snapshots.map((s) => ({ date: s.date, equity: s.equity }));

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        figure={acct ? fmt(acct.equity, 0) : undefined}
        deck={deckLine("algo-positions")}
        agate={
          acct && (
            <>
              <span>
                CASH{" "}
                <span className="num" style={{ color: theme.text }}>
                  {fmt(acct.cash, 0)}
                </span>
              </span>
              <span>
                DAY P&L{" "}
                <span className="num" style={{ color: pnlColor(acct.day_pnl) }}>
                  {fmt(acct.day_pnl, 0)}
                </span>
              </span>
            </>
          )
        }
      />
      {sparkData.length > 1 && (
        <div style={{ height: 40, padding: "4px 8px 6px", borderBottom: `1px solid ${theme.border}` }}>
          <ResponsiveContainer width="100%" height="100%">
            <LineChart data={sparkData} margin={{ top: 2, right: 2, bottom: 2, left: 2 }}>
              <Line
                type="monotone"
                dataKey="equity"
                stroke={theme.accent}
                strokeWidth={1}
                dot={false}
                isAnimationActive={false}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}
      <div tabIndex={0} style={{ flex: 1, minHeight: 0, overflowY: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ color: theme.muted, fontSize: theme.fontSize.micro }}>
              <th style={thL}>SYMBOL</th>
              <th style={thR}>QTY</th>
              <th style={thR}>AVG COST</th>
              <th style={thR}>MARK</th>
              <th style={thR}>UNREAL P&L</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((p) => (
              <tr key={p.symbol}>
                <td style={tdL}>{p.symbol}</td>
                <td className="num" style={tdR}>
                  {fmt(p.qty, 4)}
                </td>
                <td className="num" style={tdR}>
                  {fmt(p.avg_cost)}
                </td>
                <td className="num" style={tdR}>
                  {fmt(p.mark)}
                </td>
                <td className="num" style={{ ...tdR, color: pnlColor(p.unrealized_pnl) }}>
                  {fmt(p.unrealized_pnl)}
                </td>
              </tr>
            ))}
            {!rows.length && (
              <tr>
                <td colSpan={5} style={{ padding: 10, color: theme.muted, fontSize: theme.fontSize.micro }}>
                  No open positions.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

registerWidget({
  id: "algo-positions",
  title: "Positions Book",
  component: PositionsBook,
  defaultSize: { w: 8, h: 8 },
  minSize: { w: 5, h: 5 },
  defaultConfig: {},
  category: "Algo",
  description: "Open paper-trading positions, account cash/equity, and the equity curve.",
});
