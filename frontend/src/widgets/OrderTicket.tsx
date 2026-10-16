// Order Ticket (Phase 13, Algo Hub). Manual order entry against the paper OMS:
// symbol / side / type / qty / limit, a debounced live quote preview, and the
// backend's structured 400 detail surfaced as the error line. A red banner
// shows when the kill switch is engaged (orders would be rejected server-side).
import { useEffect, useState } from "react";

import { ApiError, api } from "../api/client";
import type { AlgoOrder, AlgoOrderSpec } from "../api/types";
import { LOCALE } from "../format";
import { theme } from "../theme";
import { controlStyle } from "./chartTheme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";
import { InlineError } from "./InlineError";

type Side = "buy" | "sell";
type OrderType = "market" | "limit";

const fmt = (v: number, dp = 2): string =>
  Number.isFinite(v) ? v.toLocaleString(LOCALE, { maximumFractionDigits: dp }) : "—";

const fieldStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 2,
  fontSize: theme.fontSize.micro,
  color: theme.muted,
  letterSpacing: "0.04em",
};

function OrderTicket() {
  const [symbol, setSymbol] = useState("AAPL");
  const [side, setSide] = useState<Side>("buy");
  const [type, setType] = useState<OrderType>("market");
  const [qty, setQty] = useState(1);
  const [limitPrice, setLimitPrice] = useState("");

  const [quote, setQuote] = useState<{ price: number; currency: string } | null>(null);
  const [quoteErr, setQuoteErr] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [last, setLast] = useState<AlgoOrder | null>(null);
  const [resuming, setResuming] = useState(false);
  // useAsync has no imperative refetch — bumping this dep restarts its effect,
  // forcing an immediate status refresh right after resume() instead of
  // waiting out the rest of the 10s poll interval.
  const [statusRefreshKey, setStatusRefreshKey] = useState(0);

  const status = useAsync(() => api.algoStatus(), [statusRefreshKey], 10_000);

  const trimmedLimitPrice = limitPrice.trim();
  const limitPriceInvalid = type === "limit" && (trimmedLimitPrice === "" || !Number.isFinite(Number(trimmedLimitPrice)));

  // Debounced quote preview (~500ms after the symbol stops changing) — reuses
  // the same api.quote() PriceChart's history uses, just the latest tick.
  useEffect(() => {
    const sym = symbol.trim().toUpperCase();
    if (!sym) {
      setQuote(null);
      setQuoteErr(null);
      return;
    }
    setQuoteErr(null);
    const t = setTimeout(() => {
      api
        .quote(sym)
        .then((q) => setQuote({ price: Number(q.price), currency: q.currency }))
        .catch((e) => {
          setQuote(null);
          setQuoteErr(e instanceof ApiError ? e.message : "quote failed");
        });
    }, 500);
    return () => clearTimeout(t);
  }, [symbol]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setErr(null);
    setLast(null);
    // Empty string parses to 0 via Number(), so check for blank input first —
    // otherwise a cleared field would submit limit_price: 0 instead of unset.
    const trimmedLimit = limitPrice.trim();
    const parsedLimit = trimmedLimit === "" ? NaN : Number(trimmedLimit);
    const spec: AlgoOrderSpec = {
      symbol: symbol.trim().toUpperCase(),
      side,
      type,
      qty,
      limit_price: type === "limit" && Number.isFinite(parsedLimit) ? parsedLimit : undefined,
    };
    try {
      const order = await api.algoSubmitOrder(spec);
      setLast(order);
    } catch (e2) {
      setErr(e2 instanceof ApiError ? e2.message : e2 instanceof Error ? e2.message : "submit failed");
    } finally {
      setSubmitting(false);
    }
  };

  const killed = status.data?.kill === true;

  // Resume is non-destructive (just clears the kill flag), so no confirm dialog —
  // fire-and-refetch, same pattern as cancel() in OrdersBlotter.
  const resume = async () => {
    setResuming(true);
    try {
      await api.algoResume();
      setStatusRefreshKey((k) => k + 1);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "resume failed");
    } finally {
      setResuming(false);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead deck={deckLine("algo-ticket")} />
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          gap: 10,
          padding: "8px 10px",
          flex: 1,
          minHeight: 0,
          overflowY: "auto",
        }}
      >
        {killed && (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              gap: 8,
              fontSize: theme.fontSize.micro,
              color: theme.down,
              background: "rgba(248,81,73,0.1)",
              border: `1px solid ${theme.down}`,
              borderRadius: 3,
              padding: "4px 8px",
            }}
          >
            <span>KILL SWITCH ENGAGED — new orders will be rejected.</span>
            <button
              type="button"
              onClick={resume}
              disabled={resuming}
              style={{
                background: "transparent",
                border: `1px solid ${theme.down}`,
                color: theme.down,
                borderRadius: 3,
                padding: "1px 6px",
                fontSize: theme.fontSize.micro,
                cursor: resuming ? "default" : "pointer",
                flexShrink: 0,
              }}
            >
              {resuming ? "…" : "RESUME"}
            </button>
          </div>
        )}

        <form onSubmit={submit} style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
            <label style={fieldStyle}>
              SYMBOL
              <input style={controlStyle} value={symbol} onChange={(e) => setSymbol(e.target.value.toUpperCase())} />
            </label>
            <label style={fieldStyle}>
              QTY
              <input
                style={controlStyle}
                type="number"
                min={1}
                step={1}
                value={qty}
                onChange={(e) => setQty(Math.max(1, Number(e.target.value) || 1))}
              />
            </label>
            <label style={fieldStyle}>
              SIDE
              <select style={controlStyle} value={side} onChange={(e) => setSide(e.target.value as Side)}>
                <option value="buy">Buy</option>
                <option value="sell">Sell</option>
              </select>
            </label>
            <label style={fieldStyle}>
              TYPE
              <select style={controlStyle} value={type} onChange={(e) => setType(e.target.value as OrderType)}>
                <option value="market">Market</option>
                <option value="limit">Limit</option>
              </select>
            </label>
            {type === "limit" && (
              <label style={{ ...fieldStyle, gridColumn: "1 / -1" }}>
                LIMIT PRICE
                <input
                  style={controlStyle}
                  type="number"
                  min={0}
                  step="0.01"
                  value={limitPrice}
                  onChange={(e) => setLimitPrice(e.target.value)}
                />
                {limitPriceInvalid && (
                  <span style={{ color: theme.down, fontSize: theme.fontSize.micro, letterSpacing: "normal" }}>
                    Limit price required
                  </span>
                )}
              </label>
            )}
          </div>

          <div style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>
            {quoteErr ? (
              <span style={{ color: theme.down }}>{quoteErr}</span>
            ) : quote ? (
              <span>
                last{" "}
                <span className="num" style={{ color: theme.text }}>
                  {fmt(quote.price)}
                </span>{" "}
                {quote.currency}
              </span>
            ) : (
              "quote…"
            )}
          </div>

          <button
            type="submit"
            disabled={submitting || !symbol.trim() || limitPriceInvalid}
            style={{
              padding: "6px 14px",
              background: theme.panelHeader,
              color: theme.accent,
              border: `1px solid ${theme.accent}`,
              borderRadius: 4,
              cursor: submitting ? "default" : "pointer",
              fontSize: theme.fontSize.micro,
            }}
          >
            {submitting ? "SUBMITTING…" : `SUBMIT ${side.toUpperCase()}`}
          </button>
        </form>

        {err && <InlineError msg={err} />}
        {last && (
          <div
            style={{
              fontSize: theme.fontSize.micro,
              color: theme.up,
              background: "rgba(63,185,80,0.08)",
              border: `1px solid ${theme.up}`,
              borderRadius: 3,
              padding: "4px 8px",
            }}
          >
            order #{last.id} — {last.status}
          </div>
        )}
      </div>
    </div>
  );
}

registerWidget({
  id: "algo-ticket",
  title: "Order Ticket",
  component: OrderTicket,
  defaultSize: { w: 4, h: 8 },
  minSize: { w: 3, h: 6 },
  defaultConfig: {},
  category: "Algo",
  description: "Manual order entry against the paper OMS — symbol / side / type / qty, live quote preview.",
});
