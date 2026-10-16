import { useCallback, useEffect, useRef, useState } from "react";

import { getSession, onUnauthorized } from "./api/client";
import type { Session } from "./api/types";
import { CommandPalette } from "./CommandPalette";
import { Dashboard, aiSeed, algoSeed, backtestSeed, homeSeed, journalSeed, notesSeed, macroSeed, marketResearchSeed, marketsSeed, personalSeed, quantLabSeed, ratesSeed, reportsSeed, researchSeed, riskSeed, signalsSeed } from "./Dashboard";
import type { PanelInstance } from "./layouts";
import { Login } from "./Login";
import { onNavigate } from "./navigation";
import { Orientation } from "./Orientation";
import { SECTIONS, sectionSlugOf } from "./sections";
import { useUrlSync } from "./useUrlSync";
import type { PaletteCommand, PendingAction } from "./navigation";
import { theme } from "./theme";
import { TickerBar } from "./widgets/TickerBar";
import { useWorkspaces } from "./workspaces";

// Cold-load health race fix (Phase 14b): `null` used to mean both "first poll
// still in flight" and "poll failed", so every cold load painted "backend
// offline" against a healthy backend. An explicit phase makes "haven't heard
// back yet" distinct from "genuinely unreachable".
type HealthState = { phase: "checking" } | { phase: "ok"; db: boolean; redis: boolean } | { phase: "down" };

// Default seeds for the built-in namespaces; any other (user-created) tab starts
// empty and is filled from the catalog / palette.
const emptySeed = (): PanelInstance[] => [];
const SEEDS: Record<string, () => PanelInstance[]> = {
  home: homeSeed,
  personal: personalSeed,
  markets: marketsSeed,
  macro: macroSeed,
  rates: ratesSeed,
  "market-research": marketResearchSeed,
  research: researchSeed,
  risk: riskSeed,
  signals: signalsSeed,
  backtest: backtestSeed,
  "quant-lab": quantLabSeed,
  ai: aiSeed,
  algo: algoSeed,
  reports: reportsSeed,
  journal: journalSeed,
  notes: notesSeed,
};
const seedFor = (namespace: string) => SEEDS[namespace] ?? emptySeed;

function dot(ok: boolean) {
  return <span style={{ color: ok ? theme.up : theme.down }}>{ok ? "●" : "○"}</span>;
}

// Platform-correct shortcut label for the palette affordance. The keydown
// handler below already accepts either modifier (metaKey || ctrlKey) — this
// only decides which glyph to print. userAgentData is the modern source;
// platform/userAgent are the fallback for browsers that don't expose it yet.
function isMac(): boolean {
  const uaData = (navigator as { userAgentData?: { platform?: string } }).userAgentData;
  const platform = uaData?.platform ?? navigator.platform ?? navigator.userAgent;
  return /mac/i.test(platform);
}
const SHORTCUT_LABEL = isMac() ? "⌘K" : "Ctrl+K";

function greeting(): string {
  const h = new Date().getHours();
  if (h < 5) return "Burning the midnight oil";
  if (h < 12) return "Good morning";
  if (h < 18) return "Good afternoon";
  return "Good evening";
}

// Session gate (Phase 14a). Fetches the session once, before anything else in
// the tree can fetch: while locked (auth_required && !authenticated) this
// renders ONLY <Login/>, never the app shell underneath it — so AppShell's own
// hooks (workspaces' remote hydration, the health poll, TickerBar's WS) never
// even mount, let alone fire a request. onUnauthorized is registered here
// (not inside AppShell) so it's live before AppShell's first fetch could ever
// come back 401.
// Boot is three-state on purpose. "Couldn't verify" is NOT the same as "no gate
// configured": collapsing them (by defaulting a failed /session to an
// authenticated single-user session) fails open and mounts the shell — with its
// real data — whenever the backend hiccups. It is also not the same as "locked":
// rendering the login form when the passphrase gate is disabled would strand the
// owner at a prompt no passphrase satisfies. So an unverified boot gets its own
// retry card, which mounts no data.
type Boot =
  | { phase: "checking" }
  | { phase: "error" }
  | { phase: "ready"; session: Session };

export default function App() {
  const [boot, setBoot] = useState<Boot>({ phase: "checking" });
  const [locked, setLocked] = useState(false);
  const alive = useRef(true);

  const loadSession = useCallback(() => {
    setBoot({ phase: "checking" });
    getSession()
      .then((s) => {
        if (!alive.current) return;
        setBoot({ phase: "ready", session: s });
        setLocked(s.auth_required && !s.authenticated);
      })
      .catch(() => {
        if (!alive.current) return;
        setBoot({ phase: "error" });
      });
  }, []);

  useEffect(() => {
    alive.current = true;
    loadSession();
    return () => {
      alive.current = false;
    };
  }, [loadSession]);

  useEffect(() => {
    onUnauthorized(() => setLocked(true));
  }, []);

  if (boot.phase === "checking") return null;
  if (boot.phase === "error") return <BootError onRetry={loadSession} />;
  if (locked) return <Login onSuccess={() => window.location.reload()} />;
  return <AppShell session={boot.session} />;
}

// Shown when /session could not be reached or did not answer. Deliberately
// data-free: no shell, no widgets, no polling — just a way back in.
function BootError({ onRetry }: { onRetry: () => void }) {
  return (
    <div className="login-screen">
      <div className="login-card">
        <div className="login-wordmark">C A T A L L A X</div>
        <div className="login-tagline">personal financial terminal</div>
        <p className="login-error" role="alert">
          Can’t reach the backend. The terminal could not verify this session.
        </p>
        <button className="login-submit" type="button" onClick={onRetry}>
          RETRY
        </button>
      </div>
    </div>
  );
}

function AppShell({ session }: { session: Session }) {
  const [health, setHealth] = useState<HealthState>({ phase: "checking" });
  const [paletteOpen, setPaletteOpen] = useState(false);
  const [orientationOpen, setOrientationOpen] = useState(false);
  const [pending, setPending] = useState<{ namespace: string; action: PendingAction } | null>(null);
  const nonce = useRef(0);

  // Focus trap (F3, design review 2026-08-15): Tab escaped the open palette
  // and the open orientation overlay into the app shell behind them — neither
  // is a real focus trap. `inert` on the shell (header/nav/dashboard, but not
  // the overlays themselves) removes it from the tab order and the a11y tree
  // while either overlay is open, without touching Escape (unaffected — each
  // overlay still handles it) or introducing a trap of our own: the shell is
  // freed the instant both overlays are closed. `display: contents` on the
  // wrapper keeps its children direct flex items of `.app`, so this changes
  // nothing about layout (see the comment on the pane map below).
  const shellRef = useRef<HTMLDivElement>(null);
  const shellInert = paletteOpen || orientationOpen;
  useEffect(() => {
    if (shellRef.current) shellRef.current.inert = shellInert;
  }, [shellInert]);

  const ws = useWorkspaces();
  const activeNs = ws.activeTab.namespace;
  useUrlSync(activeNs, ws.focusNamespace);

  // Keep-alive: once a tab has been opened, keep its Dashboard mounted and just
  // hide it on switch (display toggle) instead of remounting — so returning to a
  // tab is instant with no reload/refetch flash. A tab is added to the mounted
  // set only when it becomes active, so its first mount happens while visible
  // (react-grid-layout / MapLibre measure a real width, never 0 from a hidden
  // pane — the Phase 6 remount existed for exactly that reason).
  const [mounted, setMounted] = useState<string[]>([ws.activeTab.id]);
  useEffect(() => {
    setMounted((m) => (m.includes(ws.activeTab.id) ? m : [...m, ws.activeTab.id]));
  }, [ws.activeTab.id]);
  // Drop panes for tabs that no longer exist (deleted) so they unmount + free up.
  const liveTabIds = new Set(ws.tabs.map((t) => t.id));
  const paneTabs = ws.tabs.filter((t) => mounted.includes(t.id) && liveTabIds.has(t.id));

  // ⌘K / Ctrl+K toggles the command palette anywhere.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPaletteOpen((o) => !o);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  useEffect(() => {
    let alive = true;
    let fails = 0;
    const poll = () => {
      fetch("/api/v1/health", { signal: AbortSignal.timeout(4000) })
        .then((r) => r.json())
        .then((body: { db?: boolean; redis?: boolean }) => {
          if (!alive) return;
          fails = 0;
          setHealth({ phase: "ok", db: !!body.db, redis: !!body.redis });
        })
        .catch(() => {
          if (!alive) return;
          fails += 1;
          // Transient first-paint races and single blips never flash red — only
          // two CONSECUTIVE failures (any success resets the counter) flip to
          // "down". The 4s AbortSignal.timeout above counts as a failure too, so
          // a hung request can't pin "checking" forever.
          if (fails >= 2) setHealth({ phase: "down" });
        });
    };
    poll();
    const t = setInterval(poll, 15_000);
    return () => {
      alive = false;
      clearInterval(t);
    };
  }, []);

  // Resolve a palette command to a tab switch + a pending action for that tab's
  // Dashboard (which may need to mount first).
  const runCommand = (cmd: PaletteCommand) => {
    if (cmd.kind === "nav") {
      ws.focusNamespace(cmd.namespace);
      setPending({
        namespace: cmd.namespace,
        action: { nonce: ++nonce.current, focusWidgetId: cmd.focusWidgetId },
      });
    } else {
      // add-widget → current tab
      setPending({
        namespace: activeNs,
        action: { nonce: ++nonce.current, add: { widgetId: cmd.widgetId, config: cmd.config } },
      });
    }
    setPaletteOpen(false);
  };

  // Widgets render several layers below here (Dashboard → Panel → widget), so
  // one of them (HomeIntro's "start here" row) can't reach runCommand through
  // props without threading a callback through the WidgetProps contract
  // app-wide. Register the same handler the palette drives via the tiny
  // navigation.ts hook instead — no dependency array, so it always re-registers
  // the latest closure (ws/activeNs/nonce would otherwise go stale).
  useEffect(() => {
    onNavigate(runCommand);
  });

  const activeSection = sectionSlugOf(activeNs);
  const activeMembers = SECTIONS.find((s) => s.slug === activeSection)?.members ?? [];

  return (
    <>
      {/* Always rendered; CSS alone (index.css) decides which of this and .app
          is visible, so a resize can never leave it in the wrong state. */}
      <div className="small-screen-notice">
        <div className="small-screen-title">Built for a bigger screen</div>
        <p className="small-screen-body">
          Catallax is a desktop terminal — please open it on a laptop or desktop.
        </p>
      </div>
      <div className="app">
        <Orientation demoMode={session.demo_mode} onOpenChange={setOrientationOpen} />
        {/* `display: contents` so this wrapper is invisible to `.app`'s flex
            layout (its children remain direct flex items, exactly as before) —
            it exists only to hold the `ref` the inert toggle above needs. Both
            overlays stay outside it so `inert` never applies to whichever one
            is actually open. */}
        <div ref={shellRef} style={{ display: "contents" }}>
        <header className="app-header">
          <span className="app-title">CATALLAX</span>

          <nav className="section-nav">
            <button
              className={activeSection === "home" ? "section active" : "section"}
              onClick={() => ws.focusNamespace("home")}
            >
              Home
            </button>
            {SECTIONS.map((s) => (
              <button
                key={s.slug}
                className={activeSection === s.slug ? "section active" : "section"}
                onClick={() => ws.focusNamespace(s.members[0])}
              >
                {s.name}
              </button>
            ))}
          </nav>

          <span className="app-spacer" />
          <button className="palette-hint" title="Command palette" onClick={() => setPaletteOpen(true)}>
            {SHORTCUT_LABEL}
          </button>
          <span className="app-greeting">
            {session.owner_name ? `${greeting()}, ${session.owner_name}` : greeting()}
          </span>
          <span className="app-health">
            {health.phase === "ok" ? (
              <>
                db {dot(health.db)} &nbsp;redis {dot(health.redis)}
              </>
            ) : health.phase === "down" ? (
              <span style={{ color: theme.down }}>backend offline</span>
            ) : (
              <span style={{ color: theme.muted }}>checking…</span>
            )}
          </span>
        </header>

        {activeMembers.length > 1 && (
          <nav className="subnav" aria-label="Section sub-navigation">
            {activeMembers.map((ns) => {
              const tab = ws.tabs.find((t) => t.namespace === ns);
              return tab ? (
                <button
                  key={ns}
                  className={activeNs === ns ? "subtab active" : "subtab"}
                  onClick={() => ws.focusNamespace(ns)}
                >
                  {tab.name}
                </button>
              ) : null;
            })}
          </nav>
        )}

        <TickerBar />

        {/* Every opened tab stays mounted; only the active one is shown (the rest
            are display:none) so returning to a tab is instant — no remount, no
            refetch flash. `display:contents` on the active pane keeps its .grid-wrap
            the direct flex child of .app, so layout matches a single Dashboard.
            Keyed by tab id → each namespace owns its instance, so there's no
            cross-tab layout bleed (Phase 6 bug). First mount happens while the tab
            is active (visible) so grid/MapLibre measure a real width, never 0. A
            pending palette action is handed to the active tab only. */}
        {paneTabs.map((t) => {
          const isActive = t.id === ws.activeTab.id;
          return (
            <div key={t.id} style={{ display: isActive ? "contents" : "none" }}>
              <Dashboard
                namespace={t.namespace}
                seed={seedFor(t.namespace)}
                pendingAction={isActive && pending?.namespace === t.namespace ? pending.action : undefined}
                onPendingHandled={() => setPending(null)}
              />
            </div>
          );
        })}
        </div>

        <CommandPalette
          open={paletteOpen}
          tabs={ws.tabs}
          onClose={() => setPaletteOpen(false)}
          onCommand={runCommand}
        />
      </div>
    </>
  );
}
