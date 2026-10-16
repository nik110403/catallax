// Workspaces = named, multi-tab arrangements (Daily / Macro / Research / …). A
// workspace owns an ordered list of tabs; each tab points at a layout
// "namespace" whose panel presets live independently in layouts.ts. Keeping
// workspaces light (tab metadata only) means switching a workspace never moves a
// single panel — it just changes which namespaces are on screen, and each tab's
// presets follow its namespace. Persisted to localStorage; server sync is F4's
// persistence slice.
import { useEffect, useRef, useState } from "react";

import { loadRemote, saveRemote } from "./remoteStore";
import { landingNamespace } from "./sections";

export interface TabMeta {
  id: string;
  name: string;
  namespace: string; // layouts.ts preset-store key for this tab
  builtin?: boolean; // seeded tab (personal/markets) — has a default seed
}

export interface Workspace {
  id: string;
  name: string;
  tabs: TabMeta[];
  activeTabId: string;
}

interface Stored {
  workspaces: Workspace[];
  activeId: string;
}

// v15: Journal + Notes tabs (the analyst journal — Portfolio
// section) appended after Reports — an ordinary appending bump, same shape
// v14 itself used. migrateFromPrev() below now reads the v14 key and appends
// both new tabs.
// v14: Reports tab (the Weekly Advisor report — Portfolio
// section) appended at the end, after Algo — an ordinary appending bump,
// same shape every version before v13 used. v13's Home tab was the one-off
// PREPEND (see its own note below, kept for the record); this bump reverts
// to appending so migrateFromPrev() stays a one-line tab-append rather than
// needing to reorder anything.
// v13 (Phase 14b): Home tab (wordmark banner + start-here nav + demo-mode
// notice) added FIRST, and every page load now forces the active tab back to
// Home ( R4 — a friend must never cold-land on Algo; see
// withHomeLanding below). Every prior bump only ever appended a builtin tab
// and let a stale key reseed from scratch (fine, since reseeding always
// reproduced the same builtin set); this is the first bump that PREPENDS, so
// reseeding would silently discard a real customized tab list instead of just
// recreating it — migrateFromPrev() carries a still-live v12 blob forward.
// (v12 Phase 13 Algo; v11 Phase 12 AI Intelligence; v10 Phase 11 Quant/ML Lab;
// v9 Phase 10b Research; v8 Phase 10a v1.2 Market Breadth; v7 Sentiment; v6
// Market Research; v5 Backtest; v4 Signals; v3 Risk; v2 Macro + Rates).
const VERSION = "v15";
const KEY = `catallax.workspaces.${VERSION}`;
const PREV_KEY = "catallax.workspaces.v14";

let _uid = 0;
function id(prefix: string): string {
  return `${prefix}_${(_uid++).toString(36)}_${Math.floor(performance.now()).toString(36)}`;
}

export const newTabNamespace = () => id("ns");

// Every install's seeded tabs (their namespaces carry default seeds + any
// migrated Phase-6 layout). Home always leads so a fresh install lands there
// too. Built fresh each call so ids are unique.
function defaultTabs(): TabMeta[] {
  return [
    { id: id("t"), name: "Home", namespace: "home", builtin: true },
    { id: id("t"), name: "Personal", namespace: "personal", builtin: true },
    { id: id("t"), name: "Markets", namespace: "markets", builtin: true },
    { id: id("t"), name: "Macro", namespace: "macro", builtin: true },
    { id: id("t"), name: "Rates", namespace: "rates", builtin: true },
    { id: id("t"), name: "Market Research", namespace: "market-research", builtin: true },
    { id: id("t"), name: "Equity Research", namespace: "research", builtin: true },
    { id: id("t"), name: "Risk", namespace: "risk", builtin: true },
    { id: id("t"), name: "Signals", namespace: "signals", builtin: true },
    { id: id("t"), name: "Backtest", namespace: "backtest", builtin: true },
    { id: id("t"), name: "Quant Lab", namespace: "quant-lab", builtin: true },
    { id: id("t"), name: "AI", namespace: "ai", builtin: true },
    { id: id("t"), name: "Algo", namespace: "algo", builtin: true },
    { id: id("t"), name: "Reports", namespace: "reports", builtin: true },
    { id: id("t"), name: "Journal", namespace: "journal", builtin: true },
    { id: id("t"), name: "Notes", namespace: "notes", builtin: true },
  ];
}

function seedStored(): Stored {
  const tabs = defaultTabs();
  const ws: Workspace = { id: id("w"), name: "Daily", tabs, activeTabId: tabs[0].id };
  return { workspaces: [ws], activeId: ws.id };
}

// v14 → v15 migration: append fresh Journal + Notes tabs onto every
// workspace of a still-live v14 blob, preserving every existing tab, its
// order, and its layout namespace untouched — no reseed, so nobody's
// customized tab list (renamed tabs, deleted builtins, extra workspaces) is
// silently discarded. Reads the old versioned key once — the same "read a
// superseded key" precedent v13→v14 (and the old Phase-6 LEGACY_TAB_KEY)
// used, just carrying two new tabs forward instead of one.
function migrateFromPrev(): Stored | null {
  try {
    const raw = localStorage.getItem(PREV_KEY);
    if (!raw) return null;
    const prev = validateStored(JSON.parse(raw));
    if (!prev) return null;
    return {
      activeId: prev.activeId,
      workspaces: prev.workspaces.map((w) => ({
        ...w,
        tabs: [
          ...w.tabs,
          { id: id("t"), name: "Journal", namespace: "journal", builtin: true },
          { id: id("t"), name: "Notes", namespace: "notes", builtin: true },
        ],
      })),
    };
  } catch {
    return null;
  }
}

// Landing rule: every page load forces the active workspace onto
// its Home tab, regardless of what was persisted as the last-active tab — a
// friend must never cold-land on Algo. Applied once, only to the initial state
// (see the useState initializer and the remote-hydration effect below);
// in-session tab switching is untouched and still persists normally.
function withHomeLanding(s: Stored): Stored {
  // 14d: land on the URL's target namespace (an explicit deep link wins); a bare
  // "/" or an unrecognized path resolves to "home" (keeps R4 for the
  // default path while making deep links real). Applied at BOTH the useState init
  // and the remote-hydration setState, so deep-link-on-load is race-free with the
  // async server read.
  const target =
    typeof window !== "undefined" ? landingNamespace(window.location.pathname) : "home";
  return {
    ...s,
    workspaces: s.workspaces.map((w) => {
      if (w.id !== s.activeId) return w;
      const tab =
        w.tabs.find((t) => t.namespace === target) ?? w.tabs.find((t) => t.namespace === "home");
      return tab ? { ...w, activeTabId: tab.id } : w;
    }),
  };
}

function isTab(v: unknown): v is TabMeta {
  const t = v as TabMeta;
  return !!t && typeof t.id === "string" && typeof t.name === "string" && typeof t.namespace === "string";
}

// Validate a parsed object (localStorage or backend) as a usable store.
function validateStored(parsed: unknown): Stored | null {
  try {
    const p = parsed as Stored;
    if (
      p &&
      Array.isArray(p.workspaces) &&
      p.workspaces.length > 0 &&
      p.workspaces.every(
        (w) =>
          w &&
          typeof w.id === "string" &&
          typeof w.name === "string" &&
          typeof w.activeTabId === "string" &&
          Array.isArray(w.tabs) &&
          w.tabs.length > 0 &&
          w.tabs.every(isTab),
      ) &&
      typeof p.activeId === "string"
    ) {
      return p;
    }
  } catch {
    /* corrupt → reseed */
  }
  return null;
}

function loadStored(): Stored | null {
  try {
    const raw = localStorage.getItem(KEY);
    if (!raw) return null;
    return validateStored(JSON.parse(raw));
  } catch {
    return null;
  }
}

// Hook: owns the workspace store. Mirrors usePresets' shape (active + mutators)
// so App can drive the tab bar the same way Dashboard drives the layout bar.
export function useWorkspaces() {
  const [state, setState] = useState<Stored>(() =>
    withHomeLanding(loadStored() ?? migrateFromPrev() ?? seedStored()),
  );
  // Gate remote writes until the server has been read once.
  const hydrated = useRef(false);

  // Local cache (offline fallback) + debounced server write-through.
  useEffect(() => {
    try {
      localStorage.setItem(KEY, JSON.stringify(state));
    } catch {
      /* storage full / disabled */
    }
    if (hydrated.current) saveRemote(KEY, state);
  }, [state]);

  // One-time hydration from the backend (server wins on load → cross-device).
  useEffect(() => {
    let alive = true;
    loadRemote(KEY).then((remote) => {
      if (!alive) return;
      const valid = remote ? validateStored(remote) : null;
      if (valid) setState(withHomeLanding(valid));
      else saveRemote(KEY, state); // seed the server from local on first run
      hydrated.current = true;
    });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const ws = state.workspaces.find((w) => w.id === state.activeId) ?? state.workspaces[0];
  const activeTab = ws.tabs.find((t) => t.id === ws.activeTabId) ?? ws.tabs[0];

  // Update the active workspace in place.
  const mutateWs = (fn: (w: Workspace) => Workspace) =>
    setState((s) => ({
      ...s,
      workspaces: s.workspaces.map((w) => (w.id === s.activeId ? fn(w) : w)),
    }));

  const switchWorkspace = (wid: string) =>
    setState((s) => (s.workspaces.some((w) => w.id === wid) ? { ...s, activeId: wid } : s));

  // New workspace cloned from the current tab set (fresh ids), made active.
  const createWorkspace = (name: string) =>
    setState((s) => {
      const cur = s.workspaces.find((w) => w.id === s.activeId) ?? s.workspaces[0];
      const tabs = cur.tabs.map((t) => ({ ...t, id: id("t") }));
      const w: Workspace = { id: id("w"), name, tabs, activeTabId: tabs[0]?.id ?? "" };
      return { workspaces: [...s.workspaces, w], activeId: w.id };
    });

  const renameWorkspace = (wid: string, name: string) =>
    setState((s) => ({ ...s, workspaces: s.workspaces.map((w) => (w.id === wid ? { ...w, name } : w)) }));

  const deleteWorkspace = (wid: string) =>
    setState((s) => {
      const rest = s.workspaces.filter((w) => w.id !== wid);
      if (!rest.length) return s; // never delete the last workspace
      return { workspaces: rest, activeId: s.activeId === wid ? rest[0].id : s.activeId };
    });

  const setActiveTab = (tid: string) =>
    mutateWs((w) => (w.tabs.some((t) => t.id === tid) ? { ...w, activeTabId: tid } : w));

  // Activate the first tab whose namespace matches (palette navigation by code).
  const focusNamespace = (namespace: string): boolean => {
    const tab = ws.tabs.find((t) => t.namespace === namespace);
    if (tab) setActiveTab(tab.id);
    return !!tab;
  };

  const addTab = (name: string, namespace?: string) =>
    mutateWs((w) => {
      const tab: TabMeta = { id: id("t"), name, namespace: namespace ?? newTabNamespace() };
      return { ...w, tabs: [...w.tabs, tab], activeTabId: tab.id };
    });

  const renameTab = (tid: string, name: string) =>
    mutateWs((w) => ({ ...w, tabs: w.tabs.map((t) => (t.id === tid ? { ...t, name } : t)) }));

  const deleteTab = (tid: string) =>
    mutateWs((w) => {
      const rest = w.tabs.filter((t) => t.id !== tid);
      if (!rest.length) return w; // never delete the last tab
      const activeTabId = w.activeTabId === tid ? rest[0].id : w.activeTabId;
      return { ...w, tabs: rest, activeTabId };
    });

  // Move a tab one slot left (dir -1) or right (dir +1).
  const moveTab = (tid: string, dir: -1 | 1) =>
    mutateWs((w) => {
      const i = w.tabs.findIndex((t) => t.id === tid);
      const j = i + dir;
      if (i < 0 || j < 0 || j >= w.tabs.length) return w;
      const tabs = [...w.tabs];
      [tabs[i], tabs[j]] = [tabs[j], tabs[i]];
      return { ...w, tabs };
    });

  return {
    workspaces: state.workspaces,
    activeWorkspaceId: state.activeId,
    workspace: ws,
    tabs: ws.tabs,
    activeTab,
    switchWorkspace,
    createWorkspace,
    renameWorkspace,
    deleteWorkspace,
    setActiveTab,
    focusNamespace,
    addTab,
    renameTab,
    deleteTab,
    moveTab,
  };
}
