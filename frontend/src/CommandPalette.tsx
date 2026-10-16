// Command palette (⌘K / Ctrl+K). Bloomberg-style function codes + fuzzy search
// over navigation, widgets, and live symbols. Pure frontend: symbol lookup reuses
// GET /api/v1/search via api.search(); everything else dispatches a PaletteCommand
// back to App (navigation.ts).
import { useEffect, useMemo, useRef, useState } from "react";

import { api } from "./api/client";
import type { SymbolMatch } from "./api/types";
import type { PaletteCommand } from "./navigation";
import { listWidgets } from "./widgets/registry";
import type { TabMeta } from "./workspaces";

interface Item {
  key: string;
  label: string;
  hint: string; // right-aligned code/category
  cmd: PaletteCommand;
}

// Subsequence fuzzy score; 0 = no match, higher = better (contiguous wins).
function score(q: string, text: string): number {
  if (!q) return 1;
  const t = text.toLowerCase();
  q = q.toLowerCase();
  let ti = 0;
  let qi = 0;
  let s = 0;
  let streak = 0;
  while (ti < t.length && qi < q.length) {
    if (t[ti] === q[qi]) {
      qi += 1;
      streak += 1;
      s += streak;
    } else {
      streak = 0;
    }
    ti += 1;
  }
  return qi === q.length ? s : 0;
}

// Build the static command set (nav + widgets) once per tab list. One row per
// destination: a tab whose namespace has a canonical function code shows only
// that code (never a truncated fake one alongside it — design review P1,
// 2026-08-15: "Go to Markets" and the MKT row both existed and both rendered
// MARK). A tab with no canonical code (a user-created tab past the built-in
// set) still gets a nav row, just with no hint rather than a fabricated one.
function staticItems(tabs: TabMeta[]): Item[] {
  const items: Item[] = [];
  // Canonical Bloomberg-style function codes → tab navigation. Only surfaced when
  // a tab on that namespace exists (so MACRO stays hidden until Phase 8 adds it).
  const CANON = [
    { code: "NW", label: "Net Worth", namespace: "personal", focus: "net-worth" },
    { code: "MKT", label: "Markets", namespace: "markets" },
    { code: "MACRO", label: "Macro", namespace: "macro", focus: "macro-heatmap" },
    { code: "RATES", label: "Rates", namespace: "rates", focus: "yield-curve" },
    { code: "RISK", label: "Risk", namespace: "risk", focus: "risk-xray" },
    { code: "SIGNALS", label: "Signals", namespace: "signals", focus: "signal-scoreboard" },
    { code: "BT", label: "Backtest", namespace: "backtest", focus: "backtest-lab" },
    { code: "MR", label: "Market Research", namespace: "market-research", focus: "market-regime" },
    { code: "AI", label: "AI", namespace: "ai", focus: "ai-news" },
    { code: "ALGO", label: "Algo", namespace: "algo", focus: "algo-ticket" },
    { code: "REPORTS", label: "Reports", namespace: "reports", focus: "report-view" },
    // Phase 14e — four deep links that had no code: Home, the Portfolio section's
    // default (namespace "personal" — distinct from NW, which lands there but
    // auto-focuses the net-worth widget), the equity Research tab (MR already
    // took the "Market Research" mnemonic), and the Quant Lab. Codes chosen to
    // not collide with the ten above (exact-match fast path is a strict-equality
    // check on the hint, so none of NW/MKT/MACRO/RATES/RISK/SIGNALS/BT/MR/AI/ALGO
    // is a prefix or substring of HOME/PORT/RES/LAB, or vice versa).
    { code: "HOME", label: "Home", namespace: "home" },
    { code: "PORT", label: "Portfolio", namespace: "personal" },
    { code: "RES", label: "Equity Research", namespace: "research" },
    { code: "LAB", label: "Quant Lab", namespace: "quant-lab" },
  ];
  const canonNamespaces = new Set(CANON.map((c) => c.namespace));
  for (const c of CANON) {
    if (tabs.some((t) => t.namespace === c.namespace)) {
      items.push({
        key: `code-${c.code}`,
        label: c.label,
        hint: c.code,
        cmd: { kind: "nav", namespace: c.namespace, focusWidgetId: c.focus },
      });
    }
  }
  // Any tab outside the built-in namespace set (a workspace tab the user made)
  // has no canonical code to show — leave the hint blank rather than inventing
  // one, so every code visible in the palette is real.
  for (const t of tabs) {
    if (!canonNamespaces.has(t.namespace)) {
      items.push({
        key: `nav-${t.id}`,
        label: `Go to ${t.name}`,
        hint: "",
        cmd: { kind: "nav", namespace: t.namespace },
      });
    }
  }
  for (const w of listWidgets()) {
    items.push({
      key: `widget-${w.id}`,
      label: `Add ${w.title}`,
      hint: "WIDGET",
      cmd: { kind: "add-widget", widgetId: w.id },
    });
  }
  return items;
}

const SYM_RE = /^[A-Za-z0-9.\-^=]{1,12}$/;
// `GO <SYM>` with an optional trailing range (1M 3M 6M 1Y 2Y 5Y MAX, case-insensitive).
// Bare `GO SYM` keeps today's default (no range in config -> PriceChart's own default).
const RANGE_RE = "1M|3M|6M|1Y|2Y|5Y|MAX";
const GO_RE = new RegExp(`^go\\s+([A-Za-z0-9.\\-^=]{1,12})(?:\\s+(${RANGE_RE}))?$`, "i");

function chartItem(symbol: string, range?: string): Item {
  const sym = symbol.toUpperCase();
  const rng = range?.toUpperCase();
  const config: Record<string, unknown> = { symbol: sym, interval: "1d" };
  if (rng) config.range = rng;
  return {
    key: rng ? `chart-${sym}-${rng}` : `chart-${sym}`,
    label: rng ? `Open chart · ${sym} · ${rng}` : `Open chart · ${sym}`,
    hint: "GO",
    cmd: { kind: "add-widget", widgetId: "price-chart", config },
  };
}

export function CommandPalette({
  open,
  tabs,
  onClose,
  onCommand,
}: {
  open: boolean;
  tabs: TabMeta[];
  onClose: () => void;
  onCommand: (cmd: PaletteCommand) => void;
}) {
  const [q, setQ] = useState("");
  const [sel, setSel] = useState(0);
  const [matches, setMatches] = useState<SymbolMatch[]>([]);
  const inputRef = useRef<HTMLInputElement>(null);

  const statics = useMemo(() => staticItems(tabs), [tabs]);

  // Reset + focus on open.
  useEffect(() => {
    if (open) {
      setQ("");
      setSel(0);
      setMatches([]);
      // focus after the overlay paints
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open]);

  // Debounced live symbol search (reuses GET /api/v1/search). Skip the `GO `
  // prefix form (handled locally) but search bare tickers/names.
  useEffect(() => {
    if (!open) return;
    const query = q.trim();
    if (query.length < 2 || GO_RE.test(query)) {
      setMatches([]);
      return;
    }
    let alive = true;
    const t = window.setTimeout(() => {
      api
        .search(query)
        .then((r) => alive && setMatches(r.slice(0, 6)))
        .catch(() => alive && setMatches([]));
    }, 180);
    return () => {
      alive = false;
      window.clearTimeout(t);
    };
  }, [q, open]);

  const items = useMemo(() => {
    const query = q.trim();
    const out: Item[] = [];

    // `GO <SYM>` or a bare ticker → open a chart directly.
    const go = query.match(GO_RE);
    if (go) out.push(chartItem(go[1], go[2]));
    else if (query && SYM_RE.test(query) && query === query.toUpperCase()) out.push(chartItem(query));

    // Fuzzy-ranked static items, with an exact function-code fast path: when the
    // query exactly matches a hint (NW, MKT…), surface those and drop weak fuzzy
    // noise so a short code resolves to a near-singleton (the Bloomberg metaphor).
    const code = query.toUpperCase();
    const ranked = statics
      .map((it) => ({ it, s: score(query, `${it.label} ${it.hint}`), exact: it.hint !== "" && it.hint.toUpperCase() === code }))
      .filter((r) => r.s > 0 || r.exact)
      .sort((a, b) => Number(b.exact) - Number(a.exact) || b.s - a.s);
    const hasExact = ranked.some((r) => r.exact);
    out.push(...ranked.filter((r) => (hasExact ? r.exact || r.s >= 3 : true)).map((r) => r.it));

    // Live symbol matches → chart items (dedup against any already present).
    const seen = new Set(out.map((i) => i.key));
    for (const m of matches) {
      const it = chartItem(m.symbol);
      it.label = `Open chart · ${m.symbol} — ${m.name}`;
      it.hint = m.asset_type?.toUpperCase().slice(0, 6) || "SYM";
      if (!seen.has(it.key)) {
        out.push(it);
        seen.add(it.key);
      }
    }
    return out.slice(0, 40);
  }, [q, statics, matches]);

  // Keep selection in range as the list changes.
  useEffect(() => {
    setSel((s) => Math.max(0, Math.min(s, items.length - 1)));
  }, [items.length]);

  if (!open) return null;

  const run = (it: Item | undefined) => {
    if (it) onCommand(it.cmd);
  };

  const onKey = (e: React.KeyboardEvent) => {
    if (e.key === "Escape") {
      e.preventDefault();
      onClose();
    } else if (e.key === "ArrowDown") {
      e.preventDefault();
      setSel((s) => Math.min(s + 1, items.length - 1));
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      setSel((s) => Math.max(s - 1, 0));
    } else if (e.key === "Enter") {
      e.preventDefault();
      run(items[sel]);
    }
  };

  return (
    <div className="palette-overlay" onMouseDown={onClose}>
      <div className="palette" onMouseDown={(e) => e.stopPropagation()}>
        <input
          ref={inputRef}
          className="palette-input"
          value={q}
          onChange={(e) => setQ(e.target.value)}
          onKeyDown={onKey}
          placeholder="Type a code (NW · MKT · GO AAPL), search a widget or symbol…"
          aria-label="command palette"
          role="combobox"
          aria-expanded
          aria-controls="palette-listbox"
          aria-autocomplete="list"
          aria-activedescendant={items[sel] ? `palette-opt-${sel}` : undefined}
          autoComplete="off"
          spellCheck={false}
        />
        <div className="palette-list" id="palette-listbox" role="listbox">
          {items.length === 0 ? (
            <div className="palette-empty">No matches</div>
          ) : (
            items.map((it, i) => (
              <div
                key={it.key}
                id={`palette-opt-${i}`}
                role="option"
                aria-selected={i === sel}
                className={i === sel ? "palette-item active" : "palette-item"}
                onMouseEnter={() => setSel(i)}
                onMouseDown={(e) => {
                  e.preventDefault();
                  run(it);
                }}
              >
                <span className="palette-label">{it.label}</span>
                {it.hint && <span className="palette-code">{it.hint}</span>}
              </div>
            ))
          )}
        </div>
        {/* Grammar legend — plain text below the listbox, no role/id, so it is
            never reachable via aria-activedescendant and never read as an option. */}
        <div className="palette-legend">
          <span className="palette-legend-item">
            <span className="palette-legend-key">GO SYM</span>
            <span aria-hidden="true"> → </span>chart
          </span>
          <span className="palette-legend-item">
            <span className="palette-legend-key">NW, MKT, HOME</span>
            <span aria-hidden="true"> → </span>jump to a section
          </span>
          <span className="palette-legend-item">
            <span className="palette-legend-key">a name</span>
            <span aria-hidden="true"> → </span>find a widget
          </span>
          <span className="palette-legend-item">
            <span className="palette-legend-key">a ticker</span>
            <span aria-hidden="true"> → </span>find a symbol
          </span>
        </div>
      </div>
    </div>
  );
}
