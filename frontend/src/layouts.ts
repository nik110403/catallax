// Named layout presets. Each tab ("namespace") owns a set of presets; a preset is
// a named arrangement of grid panels. Create / rename / delete / switch from the
// UI, persisted to localStorage. Server-side persistence is a later phase.
import { useEffect, useRef, useState } from "react";

import { loadRemote, saveRemote } from "./remoteStore";

export interface PanelInstance {
  i: string;
  widgetId: string;
  config: unknown;
  x: number;
  y: number;
  w: number;
  h: number;
}

export interface Preset {
  id: string;
  name: string;
  builtin: boolean; // the seeded default — can be reset but not deleted to empty
  panels: PanelInstance[];
}

interface Stored {
  presets: Preset[];
  activeId: string;
}

const VERSION = "v1";
const keyFor = (ns: string) => `catallax.layouts.${ns}.${VERSION}`;
// Pre-preset single-layout key (Phase 6 migration source for the personal tab).
const LEGACY_PERSONAL_KEY = "catallax.dashboard.v3";

let _uid = 0;
function id(prefix: string): string {
  // Monotonic counter + clock keeps ids unique across rapid calls in one tick.
  return `${prefix}_${(_uid++).toString(36)}_${Math.floor(performance.now()).toString(36)}`;
}

export const panelUid = () => id("p");

// Fresh-id copy of a panel set, so a cloned preset never shares grid keys.
export function clonePanels(panels: PanelInstance[]): PanelInstance[] {
  return panels.map((p) => ({ ...p, i: panelUid() }));
}

function isPanelArray(v: unknown): v is PanelInstance[] {
  return (
    Array.isArray(v) &&
    v.every((p) => p && typeof p.i === "string" && typeof p.widgetId === "string")
  );
}

// Self-heal for the pre-fix tab-switch bug: while the Dashboard failed to remount
// on a tab switch, its persist effect wrote the *personal* layout into the markets
// namespace key. A genuine markets layout always carries ≥1 markets widget, so a
// stored markets layout with none is treated as poisoned → falls through to reseed.
const MARKETS_WIDGETS = new Set(["markets-board", "market-composite", "geo-map"]);

function isHealthy(ns: string, stored: Stored): boolean {
  if (ns !== "markets") return true;
  return stored.presets.some((p) => p.panels.some((panel) => MARKETS_WIDGETS.has(panel.widgetId)));
}

// News belongs only on the Markets tab. Stale layouts (persisted before the
// news panels were pulled from the other seeds) still carry a `news` panel in
// localStorage / the server blob; strip it from every non-markets namespace on
// load so the rule self-heals regardless of what was persisted, and the cleaned
// state is written straight back through (local + server).
function sanitize(ns: string, stored: Stored): Stored {
  if (ns === "markets") return stored;
  let changed = false;
  const presets = stored.presets.map((pr) => {
    const panels = pr.panels.filter((panel) => panel.widgetId !== "news");
    if (panels.length !== pr.panels.length) changed = true;
    return { ...pr, panels };
  });
  return changed ? { ...stored, presets } : stored;
}

// Validate a parsed object (from localStorage or the backend) as a usable store.
function validateStored(ns: string, parsed: unknown): Stored | null {
  try {
    const p = parsed as Stored;
    if (
      p &&
      Array.isArray(p.presets) &&
      p.presets.every((pr) => pr && typeof pr.id === "string" && isPanelArray(pr.panels)) &&
      p.presets.length > 0 &&
      typeof p.activeId === "string" &&
      isHealthy(ns, p)
    ) {
      const clean = sanitize(ns, p);
      // If stripping news emptied a preset (a degenerate all-news blob), don't
      // persist a blank canvas — fall through to a fresh seed instead.
      if (clean.presets.some((pr) => pr.panels.length === 0)) return null;
      return clean;
    }
  } catch {
    /* corrupt / tampered → fall through to seed */
  }
  return null;
}

function loadStored(ns: string): Stored | null {
  try {
    const raw = localStorage.getItem(keyFor(ns));
    if (!raw) return null;
    return validateStored(ns, JSON.parse(raw));
  } catch {
    return null;
  }
}

function migrateLegacy(ns: string): PanelInstance[] | null {
  if (ns !== "personal") return null;
  try {
    const raw = localStorage.getItem(LEGACY_PERSONAL_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (isPanelArray(parsed)) return parsed;
  } catch {
    /* ignore */
  }
  return null;
}

function init(ns: string, seed: () => PanelInstance[]): Stored {
  const legacy = migrateLegacy(ns);
  const panels = legacy ?? seed();
  if (legacy) localStorage.removeItem(LEGACY_PERSONAL_KEY); // one-time lift into a preset
  const preset: Preset = { id: id("L"), name: "Default", builtin: true, panels };
  return { presets: [preset], activeId: preset.id };
}

// Hook: owns the preset store for one tab. `seed` builds the built-in default's
// panels (called only when no stored/legacy layout exists or on reset).
export function usePresets(namespace: string, seed: () => PanelInstance[]) {
  const [state, setState] = useState<Stored>(() => loadStored(namespace) ?? init(namespace, seed));
  const remoteKey = `presets:${namespace}`;
  // Gate remote writes until we've read the server once, so the local seed can't
  // clobber a remote layout we haven't loaded yet.
  const hydrated = useRef(false);

  // Local cache (synchronous, offline fallback) + debounced server write-through.
  useEffect(() => {
    try {
      localStorage.setItem(keyFor(namespace), JSON.stringify(state));
    } catch {
      /* storage full / disabled */
    }
    if (hydrated.current) saveRemote(remoteKey, state);
  }, [state, namespace, remoteKey]);

  // One-time hydration from the backend (server wins on load → cross-device).
  useEffect(() => {
    let alive = true;
    hydrated.current = false;
    loadRemote(remoteKey).then((remote) => {
      if (!alive) return;
      const valid = remote ? validateStored(namespace, remote) : null;
      if (valid) setState(valid);
      else saveRemote(remoteKey, state); // seed the server from local on first run
      hydrated.current = true;
    });
    return () => {
      alive = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [namespace, remoteKey]);

  const active = state.presets.find((p) => p.id === state.activeId) ?? state.presets[0];

  const setPanels = (
    updater: PanelInstance[] | ((prev: PanelInstance[]) => PanelInstance[]),
  ) =>
    setState((s) => ({
      ...s,
      presets: s.presets.map((p) =>
        p.id === s.activeId
          ? { ...p, panels: typeof updater === "function" ? updater(p.panels) : updater }
          : p,
      ),
    }));

  const switchPreset = (pid: string) =>
    setState((s) => (s.presets.some((p) => p.id === pid) ? { ...s, activeId: pid } : s));

  // New preset cloned from the current one (fresh panel ids), made active.
  const createPreset = (name: string) =>
    setState((s) => {
      const cur = s.presets.find((p) => p.id === s.activeId) ?? s.presets[0];
      const preset: Preset = { id: id("L"), name, builtin: false, panels: clonePanels(cur.panels) };
      return { presets: [...s.presets, preset], activeId: preset.id };
    });

  const renamePreset = (pid: string, name: string) =>
    setState((s) => ({ ...s, presets: s.presets.map((p) => (p.id === pid ? { ...p, name } : p)) }));

  const deletePreset = (pid: string) =>
    setState((s) => {
      const rest = s.presets.filter((p) => p.id !== pid);
      if (!rest.length) return s; // never delete the last preset
      return { presets: rest, activeId: s.activeId === pid ? rest[0].id : s.activeId };
    });

  // Reset the active preset's panels back to a fresh seed (the "reset layout" action).
  const resetActive = (panels: PanelInstance[]) =>
    setState((s) => ({
      ...s,
      presets: s.presets.map((p) => (p.id === s.activeId ? { ...p, panels } : p)),
    }));

  return {
    presets: state.presets,
    activeId: state.activeId,
    active,
    panels: active?.panels ?? [],
    setPanels,
    switchPreset,
    createPreset,
    renamePreset,
    deletePreset,
    resetActive,
  };
}
