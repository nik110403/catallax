// Widget catalog browser. A real "add widget" gallery over the registry,
// grouped by category, with a search filter — replaces the plain dropdown. A
// widget's category comes from its definition (`category`); legacy widgets fall
// back to the map below. Adding here never mounts the widget (no data calls); it
// only places a panel.
import { useEffect, useMemo, useState } from "react";

import { listWidgets } from "./widgets/registry";

// Fallback categories for widgets that don't declare `category` on their def.
const CATEGORY_BY_ID: Record<string, string> = {
  "net-worth": "Personal",
  allocation: "Personal",
  holdings: "Personal",
  "currency-breakdown": "Personal",
  "account-tree": "Personal",
  "market-composite": "Markets",
  "markets-board": "Markets",
  "geo-map": "Markets",
  "price-chart": "Equity Research",
  quotes: "Equity Research",
  news: "General",
};

// Catalog blurbs for widgets that don't declare `description` on their def.
const DESCRIPTION_BY_ID: Record<string, string> = {
  "net-worth": "EUR net-worth area chart + latest figure",
  allocation: "Asset-class allocation donut + legend",
  holdings: "Positions: qty / price / value / P&L",
  "currency-breakdown": "Per-currency units / value / share",
  "account-tree": "Indented multi-currency ledger balances",
  quotes: "Live watchlist quotes (WebSocket)",
  news: "Aggregated market RSS headlines",
  "markets-board": "Cross-asset quotes board",
  "market-composite": "Risk-on / risk-off composite gauge",
};

// Order follows the five permanent sections' reading order (sections.ts):
// Portfolio -> Markets -> Quant -> Live, General last. Within a section,
// categories are ordered to match that section's member namespaces.
const CATEGORY_ORDER = [
  "Personal",
  "Risk",
  "Reports",
  "Markets",
  "Macro",
  "Rates",
  "Market Research",
  "Equity Research",
  "Signals",
  "Backtest",
  "Quant Lab",
  "AI",
  "Algo",
  "General",
];

function categoryOf(id: string, declared?: string): string {
  return declared ?? CATEGORY_BY_ID[id] ?? "General";
}

function descriptionOf(id: string, declared?: string): string {
  return declared ?? DESCRIPTION_BY_ID[id] ?? id;
}

export function WidgetCatalog({
  open,
  onClose,
  onAdd,
}: {
  open: boolean;
  onClose: () => void;
  onAdd: (widgetId: string) => void;
}) {
  const [q, setQ] = useState("");

  useEffect(() => {
    if (open) setQ("");
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const groups = useMemo(() => {
    const needle = q.trim().toLowerCase();
    const byCat = new Map<string, ReturnType<typeof listWidgets>>();
    for (const w of listWidgets()) {
      if (needle && !`${w.title} ${w.id} ${w.description ?? ""}`.toLowerCase().includes(needle)) continue;
      const cat = categoryOf(w.id, w.category);
      const arr = byCat.get(cat) ?? [];
      arr.push(w);
      byCat.set(cat, arr);
    }
    const cats = [...byCat.keys()].sort(
      (a, b) =>
        (CATEGORY_ORDER.indexOf(a) + 1 || 99) - (CATEGORY_ORDER.indexOf(b) + 1 || 99) ||
        a.localeCompare(b),
    );
    return cats.map((c) => ({ cat: c, widgets: byCat.get(c)! }));
  }, [q]);

  if (!open) return null;

  return (
    <div className="catalog-overlay" onMouseDown={onClose}>
      <div className="catalog" onMouseDown={(e) => e.stopPropagation()}>
        <div className="catalog-head">
          <span className="catalog-title">ADD WIDGET</span>
          <input
            className="catalog-search"
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Filter widgets…"
            aria-label="filter widgets"
            autoFocus
            spellCheck={false}
          />
          <button className="icon-btn" title="Close" onClick={onClose}>
            ✕
          </button>
        </div>
        <div className="catalog-body">
          {groups.length === 0 ? (
            <div className="palette-empty">No widgets match</div>
          ) : (
            groups.map((g) => (
              <div className="catalog-group" key={g.cat}>
                <div className="catalog-group-label">{g.cat}</div>
                <div className="catalog-grid">
                  {g.widgets.map((w) => (
                    <button
                      key={w.id}
                      className="catalog-card"
                      title={`Add ${w.title}`}
                      onClick={() => {
                        onAdd(w.id);
                        onClose();
                      }}
                    >
                      <span className="catalog-card-title">{w.title}</span>
                      <span className="catalog-card-desc">{descriptionOf(w.id, w.description)}</span>
                      <span className="catalog-card-size">
                        {w.defaultSize.w}×{w.defaultSize.h}
                      </span>
                    </button>
                  ))}
                </div>
              </div>
            ))
          )}
        </div>
      </div>
    </div>
  );
}
