import type { ReactNode } from "react";
import { useState } from "react";

import { api } from "../api/client";
import type { AccountNode } from "../api/types";
import { LOCALE } from "../format";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import type { WidgetProps } from "./registry";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";

function fmtNum(v: string): string {
  const n = Number(v);
  if (!Number.isFinite(n)) return v;
  return n.toLocaleString(LOCALE, { maximumFractionDigits: 2 });
}

function balanceCells(balances: Record<string, string>): ReactNode {
  const entries = Object.entries(balances).slice(0, 3);
  if (!entries.length) return null;
  return entries.map(([ccy, amt]) => (
    <span key={ccy}>
      {fmtNum(amt)} <span style={{ opacity: 0.65 }}>{ccy}</span>
    </span>
  ));
}

function renderNode(node: AccountNode, depth: number, out: ReactNode[]): void {
  out.push(
    <div
      className={`acct-row${depth === 0 ? " root" : ""}`}
      key={node.name}
      style={{ paddingLeft: 8 + depth * 12 }}
    >
      <span className="acct-name">{depth === 0 ? node.name : node.name.split(":").pop()}</span>
      <span className="acct-bal">{balanceCells(node.balances)}</span>
    </div>,
  );
  for (const child of node.children) renderNode(child, depth + 1, out);
}

function AccountTree(_: WidgetProps<Record<string, never>>) {
  // Bumping retryKey re-runs useAsync's fetch — it's already part of the
  // hook's public contract (any deps change re-triggers the effect), so
  // Retry needs no change to useAsync.ts itself.
  const [retryKey, setRetryKey] = useState(0);
  const { data, loading, error } = useAsync(() => api.accounts(), [retryKey], 60_000);
  const roots = data ?? [];
  const out: ReactNode[] = [];
  for (const root of roots) renderNode(root, 0, out);
  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      {/* PanelHead renders unconditionally so it doesn't appear and disappear
          as data lands;
          loading/error/empty occupy only the body area below it. */}
      <PanelHead deck={deckLine("account-tree")} />
      {loading && !data ? (
        <PanelState kind="loading" fill />
      ) : error ? (
        <PanelState kind="error" message={error} onRetry={() => setRetryKey((k) => k + 1)} fill />
      ) : !roots.length ? (
        <PanelState kind="empty" message="no accounts" fill />
      ) : (
        <div tabIndex={0} style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "4px 0" }}>{out}</div>
      )}
    </div>
  );
}

registerWidget<Record<string, never>>({
  id: "account-tree",
  title: "Accounts",
  component: AccountTree,
  defaultSize: { w: 5, h: 9 },
  minSize: { w: 3, h: 4 },
  defaultConfig: {},
});
