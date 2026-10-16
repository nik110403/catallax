// Wall-St style marquee strip (not a grid widget). One WebSocket feeds an
// editable, persisted watchlist; the row scrolls continuously right→left,
// pausing on hover (or while the editor below is open).
//
// Seamless loop, generalized: the cell list is rendered `copies` times back
// to back and the track translates by exactly one copy width, i.e.
// `-100/copies %`. The wrap is invisible as long as (copies-1) copies still
// span the container width — see the measurement effect below. copies=2
// (-50%) is the special case that covers a list wide enough to fill the
// screen on its own; small lists get more copies, not a wider one.
//
// Phase 14e: the list is sourced from GET /api/v1/watchlist (falls back to
// FEED, un-persisted, on an empty list or any fetch failure incl. 401) and
// edited in place via an anchored popover — no modal, no window.prompt.
import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties } from "react";
import { createPortal } from "react-dom";

import { ApiError, api } from "../api/client";
import type { WatchlistItemInput } from "../api/types";
import { useTicker } from "../api/useTicker";
import { useDemoMode, demoWriteProps } from "../demo";
import { LOCALE } from "../format";
import { InlineError } from "./InlineError";

type WLItem = { symbol: string; source: string | null };

// token = what the WS is asked for ("SYM" or "SYM:source"); label = what shows.
// Ordered by asset class so the band reads as groups scrolling past. This is
// also the fallback list (unauthenticated, empty watchlist, fetch failure)
// and the source of the built-in label map below.
const FEED: { token: string; label: string }[] = [
  { token: "^GSPC", label: "S&P" },
  { token: "^IXIC", label: "NASDAQ" },
  { token: "^GDAXI", label: "DAX" },
  { token: "^STOXX50E", label: "ESTOXX" },
  { token: "^FTSE", label: "FTSE" },
  { token: "^N225", label: "NIKKEI" },
  { token: "EURUSD=X", label: "EUR/USD" },
  { token: "GBPUSD=X", label: "GBP/USD" },
  { token: "GC=F", label: "GOLD" },
  { token: "SI=F", label: "SILVER" },
  { token: "CL=F", label: "WTI" },
  { token: "AAPL", label: "AAPL" },
  { token: "MSFT", label: "MSFT" },
  { token: "NVDA", label: "NVDA" },
  { token: "TSLA", label: "TSLA" },
  { token: "AMZN", label: "AMZN" },
  { token: "GOOGL", label: "GOOGL" },
  { token: "META", label: "META" },
  { token: "BTC:coingecko", label: "BTC" },
  { token: "ETH:coingecko", label: "ETH" },
  { token: "SOL:coingecko", label: "SOL" },
];

function parseToken(token: string): WLItem {
  const i = token.indexOf(":");
  return i === -1 ? { symbol: token, source: null } : { symbol: token.slice(0, i), source: token.slice(i + 1) };
}
function tokenFor(item: WLItem): string {
  return item.source ? `${item.symbol}:${item.source}` : item.symbol;
}

const DEFAULT_ITEMS: WLItem[] = FEED.map((f) => parseToken(f.token));
// The watchlist API stores symbol+source+order, no label column. Known
// symbols keep their curated label; anything a user adds falls back to the
// symbol itself.
const KNOWN_LABELS: Record<string, string> = Object.fromEntries(FEED.map((f) => [f.token, f.label]));
function labelFor(item: WLItem): string {
  return KNOWN_LABELS[tokenFor(item)] ?? item.symbol.toUpperCase();
}

// Constant per-symbol scroll speed regardless of how many symbols are shown
// or how many copies are rendered — the track always travels exactly one
// copy-width per loop, so this only needs to scale with the item count.
const SECONDS_PER_SYMBOL = 4;

const SOURCE_OPTIONS = [
  { value: "", label: "default" },
  { value: "yfinance", label: "yfinance" },
  { value: "coingecko", label: "coingecko" },
  { value: "stooq", label: "stooq" },
];

export function TickerBar() {
  const [items, setItems] = useState<WLItem[]>(DEFAULT_ITEMS);
  const [copies, setCopies] = useState(2);

  const [editorOpen, setEditorOpen] = useState(false);
  const [draft, setDraft] = useState<WLItem[]>([]);
  const [newSymbol, setNewSymbol] = useState("");
  const [newSource, setNewSource] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [pos, setPos] = useState({ top: 0, left: 0 });

  const containerRef = useRef<HTMLDivElement>(null);
  const copyRef = useRef<HTMLDivElement>(null);
  const editBtnRef = useRef<HTMLButtonElement>(null);
  const popRef = useRef<HTMLDivElement>(null);

  const demo = useDemoMode();
  const symbols = items.map(tokenFor);
  const ticks = useTicker(symbols);
  // Baseline = first observed price this session; colour persists as direction
  // vs that baseline rather than flickering on each tick.
  const base = useRef<Record<string, number>>({});

  // Load the persisted watchlist once. An empty list or any failure (offline,
  // 401 from an expired share-gate session, server error) keeps the built-in
  // FEED on screen — the stored list itself stays empty until the user saves.
  useEffect(() => {
    let cancelled = false;
    api
      .getWatchlist()
      .then((list) => {
        if (cancelled || list.length === 0) return;
        setItems(list.map((w) => ({ symbol: w.symbol, source: w.source })));
      })
      .catch(() => {
        /* keep the default feed */
      });
    return () => {
      cancelled = true;
    };
  }, []);

  // Measure one rendered copy against the container and pick the smallest
  // copy count `copies` such that (copies-1) copies still span the container
  // — that keeps the wrap seam permanently off-screen regardless of list
  // length. Translate distance is `-100/copies %`, derived below, not fixed.
  useLayoutEffect(() => {
    const container = containerRef.current;
    const copyEl = copyRef.current;
    if (!container || !copyEl) return;
    const measure = () => {
      const copyWidth = copyEl.scrollWidth;
      const containerWidth = container.clientWidth;
      if (!copyWidth || !containerWidth) return;
      const needed = Math.max(2, Math.ceil(containerWidth / copyWidth) + 1);
      setCopies((prev) => (prev === needed ? prev : needed));
    };
    measure();
    const ro = new ResizeObserver(measure);
    ro.observe(container);
    return () => ro.disconnect();
  }, [items]);

  function cell(item: WLItem, copyIdx: number) {
    const token = tokenFor(item);
    const sym = item.symbol.toUpperCase();
    const t = ticks[sym];
    const px = t?.price ? Number(t.price) : undefined;
    if (px !== undefined && base.current[sym] === undefined) base.current[sym] = px;
    const b = base.current[sym];
    const cls =
      px !== undefined && b !== undefined ? (px > b ? "ticker-up" : px < b ? "ticker-down" : "") : "";
    if (demo && t?.error) return null; // demo: only symbols with data
    return (
      <div className="ticker-cell" key={`${copyIdx}-${token}`}>
        <span className="ticker-sym">{labelFor(item)}</span>
        {t?.error ? (
          <span className="ticker-err">—</span>
        ) : px !== undefined ? (
          <span className={`ticker-px ${cls}`}>
            {/* FX and sub-10 quotes move in the 3rd/4th decimal — 2dp would render
                EUR/USD as a flat 1.15. Larger quotes stay at 2dp. */}
            {px.toLocaleString(LOCALE, {
              minimumFractionDigits: 2,
              maximumFractionDigits: px < 10 ? 4 : 2,
            })}
          </span>
        ) : (
          <span className="ticker-sym">…</span>
        )}
      </div>
    );
  }

  // Demo hides errored symbols, so only count the cells actually shown.
  const shown = demo ? items.filter((it) => !ticks[it.symbol.toUpperCase()]?.error).length : items.length;
  const duration = `${Math.max(1, shown) * SECONDS_PER_SYMBOL}s`;
  const shiftPct = 100 / copies;
  const trackStyle = {
    animationDuration: duration,
    animationPlayState: editorOpen ? "paused" : undefined,
    "--marquee-shift": `-${shiftPct}%`,
  } as CSSProperties;

  function openEditor() {
    const r = editBtnRef.current?.getBoundingClientRect();
    if (r) {
      const width = Math.min(420, window.innerWidth * 0.92);
      setPos({
        top: r.bottom + 6,
        left: Math.max(8, Math.min(r.right - width, window.innerWidth - width - 8)),
      });
    }
    setDraft(items.map((it) => ({ ...it })));
    setError(null);
    setNewSymbol("");
    setNewSource("");
    setEditorOpen(true);
  }
  function closeEditor() {
    setEditorOpen(false);
    editBtnRef.current?.focus();
  }

  // Focus the first field on open, close on Escape or an outside click, and
  // tear both listeners down on every close path — no lingering trap.
  useEffect(() => {
    if (!editorOpen) return;
    const firstField = popRef.current?.querySelector<HTMLElement>("input, select, button");
    firstField?.focus();
    function onKey(e: KeyboardEvent) {
      if (e.key === "Escape") closeEditor();
    }
    function onClick(e: MouseEvent) {
      const t = e.target as Node;
      if (popRef.current?.contains(t) || editBtnRef.current?.contains(t)) return;
      closeEditor();
    }
    document.addEventListener("keydown", onKey);
    document.addEventListener("mousedown", onClick);
    return () => {
      document.removeEventListener("keydown", onKey);
      document.removeEventListener("mousedown", onClick);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [editorOpen]);

  function moveDraft(idx: number, dir: -1 | 1) {
    setDraft((prev) => {
      const j = idx + dir;
      if (j < 0 || j >= prev.length) return prev;
      const next = [...prev];
      [next[idx], next[j]] = [next[j], next[idx]];
      return next;
    });
  }
  function removeDraft(idx: number) {
    setDraft((prev) => prev.filter((_, i) => i !== idx));
  }
  function updateDraft(idx: number, patch: Partial<WLItem>) {
    setDraft((prev) => prev.map((it, i) => (i === idx ? { ...it, ...patch } : it)));
  }
  function addDraft() {
    const symbol = newSymbol.trim().toUpperCase();
    if (!symbol) return;
    const source = newSource || null;
    // Light client-side check only — the server is the real gate (400 +
    // message surfaced below on save).
    const dup = draft.some((d) => d.symbol === symbol && d.source === source);
    if (dup) {
      setError(`${symbol} is already in the list.`);
      return;
    }
    setDraft((prev) => [...prev, { symbol, source }]);
    setNewSymbol("");
    setNewSource("");
    setError(null);
  }

  async function save() {
    setSaving(true);
    setError(null);
    const body: WatchlistItemInput[] = draft.map((d) => ({ symbol: d.symbol, source: d.source }));
    try {
      const saved = await api.saveWatchlist(body);
      setItems(saved.map((w) => ({ symbol: w.symbol, source: w.source })));
      setEditorOpen(false);
      editBtnRef.current?.focus();
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Failed to save watchlist — try again.");
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="marquee" ref={containerRef}>
      <div className="marquee-track" style={trackStyle}>
        {Array.from({ length: copies }, (_, c) => (
          <div className="marquee-copy" key={c} ref={c === 0 ? copyRef : undefined} aria-hidden={c > 0 || undefined}>
            {items.map((item) => cell(item, c))}
          </div>
        ))}
      </div>
      <div className="marquee-edit">
        <button
          type="button"
          className="icon-btn"
          ref={editBtnRef}
          onClick={() => (editorOpen ? closeEditor() : openEditor())}
          aria-haspopup="dialog"
          aria-expanded={editorOpen}
          disabled={demo}
          {...demoWriteProps(demo)}
        >
          EDIT
        </button>
      </div>
      {editorOpen &&
        createPortal(
          <div
            className="ticker-editor"
            style={{ top: pos.top, left: pos.left }}
            role="dialog"
            aria-modal="false"
            aria-label="Edit ticker watchlist"
            ref={popRef}
          >
            <div className="ticker-editor-title">Watchlist</div>
            <div className="ticker-editor-list">
              {draft.length === 0 && <div className="ticker-editor-empty">No symbols — add one below.</div>}
              {draft.map((d, i) => (
                <div className="ticker-editor-row" key={i}>
                  <input
                    type="text"
                    aria-label="Symbol"
                    value={d.symbol}
                    onChange={(e) => updateDraft(i, { symbol: e.target.value.toUpperCase() })}
                  />
                  <select
                    aria-label="Source"
                    value={d.source ?? ""}
                    onChange={(e) => updateDraft(i, { source: e.target.value || null })}
                  >
                    {SOURCE_OPTIONS.map((s) => (
                      <option key={s.value} value={s.value}>
                        {s.label}
                      </option>
                    ))}
                  </select>
                  <button
                    type="button"
                    className="icon-btn"
                    aria-label={`Move ${d.symbol} up`}
                    disabled={i === 0}
                    onClick={() => moveDraft(i, -1)}
                  >
                    ↑
                  </button>
                  <button
                    type="button"
                    className="icon-btn"
                    aria-label={`Move ${d.symbol} down`}
                    disabled={i === draft.length - 1}
                    onClick={() => moveDraft(i, 1)}
                  >
                    ↓
                  </button>
                  <button
                    type="button"
                    className="icon-btn"
                    aria-label={`Remove ${d.symbol}`}
                    onClick={() => removeDraft(i)}
                  >
                    ✕
                  </button>
                </div>
              ))}
            </div>
            <div className="ticker-editor-add">
              <input
                type="text"
                aria-label="New symbol"
                placeholder="Symbol (e.g. NVDA)"
                value={newSymbol}
                onChange={(e) => setNewSymbol(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter") {
                    e.preventDefault();
                    addDraft();
                  }
                }}
              />
              <select aria-label="New symbol source" value={newSource} onChange={(e) => setNewSource(e.target.value)}>
                {SOURCE_OPTIONS.map((s) => (
                  <option key={s.value} value={s.value}>
                    {s.label}
                  </option>
                ))}
              </select>
              <button type="button" className="ticker-editor-btn" onClick={addDraft} disabled={draft.length >= 50}>
                Add
              </button>
            </div>
            {error && <InlineError msg={error} variant="text" />}
            <div className="ticker-editor-actions">
              <button type="button" className="ticker-editor-btn" onClick={closeEditor} disabled={saving}>
                Cancel
              </button>
              <button type="button" className="ticker-editor-btn" onClick={save} disabled={saving}>
                {saving ? "Saving…" : "Save"}
              </button>
            </div>
          </div>,
          document.body,
        )}
    </div>
  );
}
