// First-visit orientation overlay (Phase 14f). A newcomer currently lands with
// zero onboarding and no way to tell what this app is — this is a one-screen
// "what is Catallax" card, shown once and dismissed for good. Mirrors the Login
// card's dark-TUI aesthetic (Login.tsx's .login-screen/.login-card) so it reads
// as part of the same product, not a bolted-on tooltip. Rendered only inside
// AppShell (App.tsx keeps it out of the Login/BootError paths entirely).
import { useCallback, useEffect, useRef, useState } from "react";

import { SECTIONS } from "./sections";

const ORIENTATION_KEY = "catallax.orientation.v1";

// One short blurb per section, keyed by sections.ts's slug — names/roles come
// from SECTIONS itself so these can't drift from the real nav labels. Home
// isn't a Section (sections.ts: "Home is its own implicit section"), so it
// gets its own fixed row below.
// Demo mode serves a cached snapshot, so "live" copy is reworded there.
const DEMO_BLURB: Record<string, string> = {
  portfolio: "A sample portfolio: holdings, allocation, and a risk lens on the whole book.",
  markets: "A cross-asset board, the macro dashboard, rates, and market research — from a data snapshot.",
};

const SECTION_BLURB: Record<string, string> = {
  portfolio: "Your holdings, allocation, and a risk lens on the whole book.",
  markets: "A live cross-asset board, the macro dashboard, rates, and market research.",
  quant: "Equity research, signal scoring, backtesting, and the ML lab.",
  live: "AI news intelligence and paper trading, side by side.",
};

// A read failure (storage disabled/throwing) counts as "already seen" — never
// show the overlay on a guess, and never risk it reappearing every load with
// no way to make a dismissal stick.
function hasSeenOrientation(): boolean {
  try {
    return localStorage.getItem(ORIENTATION_KEY) !== null;
  } catch {
    return true;
  }
}

function markOrientationSeen(): void {
  try {
    localStorage.setItem(ORIENTATION_KEY, "1");
  } catch {
    /* storage disabled — nothing to persist; this visit still dismisses below */
  }
}

export function Orientation({
  demoMode,
  onOpenChange,
}: {
  demoMode: boolean;
  // Lets App.tsx know when this overlay is showing, so it can `inert` the app
  // shell behind it (F3, design review 2026-08-15: Tab escaped the open
  // overlay into the app shell — measured on both this and the palette).
  onOpenChange?: (open: boolean) => void;
}) {
  const [open, setOpen] = useState(() => !hasSeenOrientation());
  const dismissRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    onOpenChange?.(open);
  }, [open, onOpenChange]);

  // Always runs, regardless of whether the write above succeeded — dismissal
  // must never depend on storage working.
  const dismiss = useCallback(() => {
    markOrientationSeen();
    setOpen(false);
  }, []);

  useEffect(() => {
    if (open) dismissRef.current?.focus();
  }, [open]);

  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") dismiss();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, dismiss]);

  if (!open) return null;

  return (
    <div className="orientation-overlay" onMouseDown={dismiss}>
      <div
        className="orientation-card"
        role="dialog"
        aria-modal="true"
        aria-label="What is Catallax"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="orientation-head">
          <span className="orientation-eyebrow">CATALLAX</span>
          <h1 className="orientation-lede">
            A self-hosted terminal for one person's money and the markets around it.
          </h1>
        </div>

        <div className="orientation-sections">
          <div className="orientation-row">
            <span className="orientation-section-name">Home</span>
            <span className="orientation-section-desc">
              Where you land — a snapshot of net worth, markets, and what to check next.
            </span>
          </div>
          {SECTIONS.map((s) => (
            <div className="orientation-row" key={s.slug}>
              <span className="orientation-section-name">{s.name}</span>
              <span className="orientation-section-desc">{(demoMode && DEMO_BLURB[s.slug]) || SECTION_BLURB[s.slug]}</span>
            </div>
          ))}
        </div>

        <p className="orientation-note">
          {demoMode
            ? "These figures are a sample portfolio on a data snapshot, not actual holdings. Actions that write or run jobs are disabled in this demo, and nothing here trades real money — the trading desk is paper-only by construction."
            : "All the data is real — but nothing here trades real money. The trading desk is paper-only by construction."}
        </p>
        <p className="orientation-hint">Press ⌘K / Ctrl+K anywhere to jump straight to a section or widget.</p>

        <button ref={dismissRef} type="button" className="orientation-dismiss" onClick={dismiss}>
          START
        </button>
      </div>
    </div>
  );
}
