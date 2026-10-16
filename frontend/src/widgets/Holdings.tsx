import { useState } from "react";

import { api } from "../api/client";
import type { WfHoldingAccount, WfHoldingAsset } from "../api/types";
import { LOCALE } from "../format";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import { controlStyle } from "./chartTheme";
import type { WidgetProps } from "./registry";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";

interface HoldingsConfig {
  view: "asset" | "account";
}

const fmt = (v: string | number, dp = 2): string => {
  const n = Number(v);
  return Number.isFinite(n) ? n.toLocaleString(LOCALE, { maximumFractionDigits: dp }) : String(v);
};

const thL: React.CSSProperties = { textAlign: "left", padding: "3px 8px", fontWeight: 400 };
const thR: React.CSSProperties = { textAlign: "right", padding: "3px 8px", fontWeight: 400 };
const tdR: React.CSSProperties = { textAlign: "right", padding: "2px 8px", whiteSpace: "nowrap" };

// Holdings come from the imported portfolio. Switchable views:
//   asset   — per coin/stock (quantity + value)
//   account — per exporter account (investments + cash split)
function Holdings({ config, onConfigChange }: WidgetProps<HoldingsConfig>) {
  const view = config.view ?? "asset";
  // Bumping retryKey re-runs useAsync's fetch — it's already part of the
  // hook's public contract (any deps change re-triggers the effect), so
  // Retry needs no change to useAsync.ts itself.
  const [retryKey, setRetryKey] = useState(0);
  const { data, loading, error } = useAsync(() => api.wealthfolioHoldings(), [retryKey], 60_000);
  const cur = data?.base ?? "EUR";

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        figure={data ? fmt(data.total, 0) : undefined}
        unit={cur}
        deck={deckLine("holdings")}
        agate={
          data && (
            <span className="num">
              inv {fmt(data.investments, 0)} · cash {fmt(data.cash, 0)}
            </span>
          )
        }
      >
        <select
          value={view}
          onChange={(e) =>
            onConfigChange({ ...config, view: e.target.value as HoldingsConfig["view"] })
          }
          style={controlStyle}
          aria-label="holdings grouping"
        >
          <option value="asset">By asset</option>
          <option value="account">By account</option>
        </select>
      </PanelHead>
      {/* PanelHead now renders unconditionally above so it doesn't appear and
          disappear as data lands — loading/
          error occupy only the body area, fill-centered like the empty
          states already do elsewhere. This is the reference fix for the
          pattern; CurrencyBreakdown and Allocation repeat it. */}
      {loading && !data ? (
        <PanelState kind="loading" fill />
      ) : error ? (
        <PanelState kind="error" message={error} onRetry={() => setRetryKey((k) => k + 1)} fill />
      ) : (
      <div tabIndex={0} style={{ flex: 1, minHeight: 0, overflowY: "auto" }}>
        {view === "asset" ? (
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ color: theme.muted, fontSize: theme.fontSize.micro }}>
                <th style={thL}>ASSET</th>
                <th style={thR}>QTY</th>
                <th style={thR}>VALUE</th>
              </tr>
            </thead>
            <tbody>
              {(data?.by_asset ?? []).map((r: WfHoldingAsset) => (
                <tr key={r.symbol} className="kv">
                  <td style={{ padding: "2px 8px" }}>
                    <span style={{ fontWeight: 600 }}>{r.symbol}</span>{" "}
                    <span style={{ color: theme.muted, fontSize: theme.fontSize.micro }}>
                      {r.asset_class.toLowerCase()}
                    </span>
                  </td>
                  <td className="num" style={tdR}>
                    {fmt(r.quantity, 4)}
                  </td>
                  <td className="num" style={tdR}>
                    {fmt(r.value, 0)} <span style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>{cur}</span>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <table style={{ width: "100%", borderCollapse: "collapse" }}>
            <thead>
              <tr style={{ color: theme.muted, fontSize: theme.fontSize.micro }}>
                <th style={thL}>ACCOUNT</th>
                <th style={thR}>INV</th>
                <th style={thR}>CASH</th>
                <th style={thR}>VALUE</th>
              </tr>
            </thead>
            <tbody>
              {(data?.by_account ?? []).map((r: WfHoldingAccount) => (
                <tr key={r.account} className="kv">
                  <td style={{ padding: "2px 8px", fontWeight: 600 }}>{r.account}</td>
                  <td className="num" style={tdR}>
                    {fmt(r.investments, 0)}
                  </td>
                  <td className="num" style={tdR}>
                    {fmt(r.cash, 0)}
                  </td>
                  <td className="num" style={tdR}>
                    {fmt(r.value, 0)}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
      )}
    </div>
  );
}

registerWidget<HoldingsConfig>({
  id: "holdings",
  title: "Holdings",
  component: Holdings,
  defaultSize: { w: 7, h: 7 },
  minSize: { w: 4, h: 4 },
  defaultConfig: { view: "asset" },
});
