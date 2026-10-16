// Orders Blotter (Phase 13, Algo Hub). Live order + fill history against the
// paper OMS. Subscribes to the algo pub/sub SSE stream so fills/status changes
// refresh immediately; a 15s poll is the fallback if the stream drops. Lazy
// (list-heavy, mirrors AINews' registerLazyWidget pattern) — registered in
// widgets/index.ts.
import { useEffect, useRef, useState } from "react";

import { ApiError, algoStreamUrl, api } from "../api/client";
import type { AlgoFill, AlgoOrder } from "../api/types";
import { LOCALE } from "../format";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";

const STATUS_COLOR: Record<string, string> = {
  filled: theme.up,
  rejected: theme.down,
  cancelled: theme.muted,
  canceled: theme.muted,
  accepted: theme.accent,
  new: theme.accent,
};

// Fixed min+max fraction digits (not just max) so numeric columns don't ragged-edge
// on decimals — aids column scanning when right-aligned.
const fmt = (v: number, dp = 2): string =>
  Number.isFinite(v) ? v.toLocaleString(LOCALE, { minimumFractionDigits: dp, maximumFractionDigits: dp }) : "—";

const thL: React.CSSProperties = { textAlign: "left", padding: "3px 8px", fontWeight: 400 };
const thR: React.CSSProperties = { textAlign: "right", padding: "3px 8px", fontWeight: 400 };
// Row separator lives on the cells (not the <tr>) so native table layout — not
// the .kv flex-row class — drives column alignment; .kv on a <tr> collapses
// its geometry to a flex row and desyncs it from the header's table cells.
const tdL: React.CSSProperties = { padding: "2px 8px", whiteSpace: "nowrap", borderBottom: `1px solid ${theme.border}` };
const tdR: React.CSSProperties = {
  textAlign: "right",
  padding: "2px 8px",
  whiteSpace: "nowrap",
  borderBottom: `1px solid ${theme.border}`,
};

export function OrdersBlotter() {
  const [orders, setOrders] = useState<AlgoOrder[]>([]);
  const [fills, setFills] = useState<AlgoFill[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [cancelling, setCancelling] = useState<number | null>(null);

  // Tracks component liveness so refresh() (called from the effect's stream
  // handler/poll and from cancel()) never sets state after unmount.
  const aliveRef = useRef(true);

  const refresh = () => {
    Promise.all([api.algoOrders(), api.algoFills()])
      .then(([o, f]) => {
        if (!aliveRef.current) return;
        setOrders(o.orders);
        setFills(f.fills);
        setErr(null);
      })
      .catch((e) => aliveRef.current && setErr(e instanceof ApiError ? e.message : "load failed"));
  };

  useEffect(() => {
    aliveRef.current = true;
    refresh();
    const es = new EventSource(algoStreamUrl());
    // Debounce: a burst of stream messages (e.g. several fills in quick
    // succession) collapses into a single trailing refresh instead of one
    // refetch per message.
    let debounce: ReturnType<typeof setTimeout> | null = null;
    es.onmessage = () => {
      if (debounce) clearTimeout(debounce);
      debounce = setTimeout(refresh, 250);
    };
    // EventSource auto-reconnects on transient errors; the 15s poll below
    // covers any gap while it's down.
    const poll = setInterval(refresh, 15_000);
    return () => {
      aliveRef.current = false;
      if (debounce) clearTimeout(debounce);
      es.close();
      clearInterval(poll);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const cancel = async (id: number) => {
    setCancelling(id);
    try {
      await api.algoCancelOrder(id);
      refresh();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "cancel failed");
    } finally {
      setCancelling(null);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead deck={deckLine("algo-blotter")} />
      {err && <div style={{ fontSize: theme.fontSize.micro, color: theme.down, padding: "4px 8px" }}>{err}</div>}
      <div tabIndex={0} style={{ flex: 1, minHeight: 0, overflowY: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ color: theme.muted, fontSize: theme.fontSize.micro }}>
              <th style={thL}>SYMBOL</th>
              <th style={thL}>SIDE</th>
              <th style={thR}>QTY</th>
              <th style={thL}>STATUS</th>
              <th style={thL}>REASON</th>
              <th style={thR} />
            </tr>
          </thead>
          <tbody>
            {orders.map((o) => (
              <tr key={o.id}>
                <td style={{ ...tdL, fontWeight: 600 }}>{o.symbol}</td>
                <td style={tdL}>{o.side}</td>
                <td className="num" style={tdR}>
                  {fmt(o.qty, 4)}
                </td>
                <td style={{ ...tdL, color: STATUS_COLOR[o.status] ?? theme.text }}>{o.status}</td>
                <td style={{ ...tdL, color: theme.muted, fontSize: theme.fontSize.micro }}>{o.reason ?? ""}</td>
                <td style={tdR}>
                  {(o.status === "new" || o.status === "accepted") && (
                    <button
                      onClick={() => cancel(o.id)}
                      disabled={cancelling === o.id}
                      style={{
                        background: "transparent",
                        border: `1px solid ${theme.border}`,
                        color: theme.down,
                        borderRadius: 3,
                        padding: "1px 6px",
                        fontSize: theme.fontSize.micro,
                        cursor: "pointer",
                      }}
                    >
                      {cancelling === o.id ? "…" : "CANCEL"}
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {!orders.length && (
              <tr>
                <td colSpan={6} style={{ padding: 10, color: theme.muted, fontSize: theme.fontSize.micro }}>
                  No orders yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
      <div tabIndex={0} style={{ borderTop: `1px solid ${theme.border}`, maxHeight: "40%", overflowY: "auto" }}>
        <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.05em", padding: "4px 8px" }}>FILLS</div>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <thead>
            <tr style={{ color: theme.muted, fontSize: theme.fontSize.micro }}>
              <th style={thL}>SYMBOL</th>
              <th style={thL}>SIDE</th>
              <th style={thR}>QTY</th>
              <th style={thR}>PRICE</th>
              <th style={thR}>COMMISSION</th>
            </tr>
          </thead>
          <tbody>
            {fills.map((f) => (
              <tr key={f.id}>
                <td style={tdL}>{f.symbol}</td>
                <td style={tdL}>{f.side}</td>
                <td className="num" style={tdR}>
                  {fmt(f.qty, 4)}
                </td>
                <td className="num" style={tdR}>
                  {fmt(f.price)}
                </td>
                <td className="num" style={tdR}>
                  {fmt(f.commission)}
                </td>
              </tr>
            ))}
            {!fills.length && (
              <tr>
                <td colSpan={5} style={{ padding: 8, color: theme.muted, fontSize: theme.fontSize.micro }}>
                  No fills yet.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}
