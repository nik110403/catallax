// WebSocket price ticker hook. Connects to /api/v1/ws/prices and merges each
// {prices:{SYM:{...}}} push into a symbol->TickEntry map. Auto-reconnects.
// `symbols` tokens may carry a per-symbol source, e.g. "BTC:coingecko".
import { useEffect, useRef, useState } from "react";

import type { TickEntry } from "./types";

export function useTicker(symbols: string[], source?: string): Record<string, TickEntry> {
  const [ticks, setTicks] = useState<Record<string, TickEntry>>({});
  const key = symbols.join(",");
  const wsRef = useRef<WebSocket | null>(null);

  useEffect(() => {
    if (!key) {
      setTicks({});
      return;
    }
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const qs = new URLSearchParams({ symbols: key });
    if (source) qs.set("source", source);
    const url = `${proto}://${location.host}/api/v1/ws/prices?${qs.toString()}`;

    let closed = false;
    let retry: ReturnType<typeof setTimeout> | undefined;

    const connect = () => {
      const ws = new WebSocket(url);
      wsRef.current = ws;
      ws.onmessage = (ev) => {
        try {
          const data = JSON.parse(ev.data);
          // Only merge a plain-object prices map; ignore arrays / odd shapes.
          if (data.prices && typeof data.prices === "object" && !Array.isArray(data.prices)) {
            setTicks((prev) => ({ ...prev, ...data.prices }));
          }
        } catch {
          /* ignore malformed frame */
        }
      };
      ws.onclose = () => {
        if (!closed) retry = setTimeout(connect, 3000);
      };
      ws.onerror = () => ws.close();
    };
    connect();

    return () => {
      closed = true;
      if (retry) clearTimeout(retry);
      wsRef.current?.close();
    };
  }, [key, source]);

  return ticks;
}
