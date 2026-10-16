import { useRef } from "react";

import { useTicker } from "../api/useTicker";
import { LOCALE } from "../format";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { useDemoMode } from "../demo";
import type { WidgetProps } from "./registry";
import { registerWidget } from "./registry";

interface QuotesConfig {
  symbols: string[];
}

function Quotes({ config }: WidgetProps<QuotesConfig>) {
  const demo = useDemoMode();
  const ticks = useTicker(config.symbols);
  // Colour vs session-baseline (first observed price), so direction persists.
  const base = useRef<Record<string, number>>({});

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead deck={deckLine("quotes")} />
      <div tabIndex={0} style={{ flex: 1, minHeight: 0, overflowY: "auto" }}>
        <table style={{ width: "100%", borderCollapse: "collapse" }}>
          <tbody>
            {config.symbols.map((tok) => {
              const sym = tok.split(":")[0].toUpperCase();
              const t = ticks[sym];
              const px = t?.price ? Number(t.price) : undefined;
              if (px !== undefined && base.current[sym] === undefined) base.current[sym] = px;
              const b = base.current[sym];
              if (demo && t?.error) return null; // demo: only symbols with data
              const cls =
                px !== undefined && b !== undefined
                  ? px > b
                    ? "ticker-up"
                    : px < b
                      ? "ticker-down"
                      : ""
                  : "";
              return (
                <tr key={tok} className="kv">
                  <td className="k">{sym}</td>
                  <td className="num" style={{ textAlign: "right" }}>
                    {t?.error ? (
                      <span className="ticker-err">{t.error.code}</span>
                    ) : px !== undefined ? (
                      <span className={cls}>
                        {px.toLocaleString(LOCALE, { maximumFractionDigits: 2 })}
                      </span>
                    ) : (
                      "…"
                    )}
                  </td>
                  <td className="acct-bal" style={{ textAlign: "right", fontSize: theme.fontSize.micro }}>
                    {t?.source ?? ""}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

registerWidget({
  id: "quotes",
  title: "Watchlist",
  component: Quotes,
  defaultSize: { w: 4, h: 7 },
  minSize: { w: 2, h: 3 },
  defaultConfig: { symbols: ["AAPL", "MSFT", "BTC:coingecko", "ETH:coingecko"] } as QuotesConfig,
});
