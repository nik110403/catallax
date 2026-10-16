// Hosted read-only demo awareness. Single source of truth is /api/v1/session's
// `demo_mode` (already fetched once by App's session gate); App publishes it
// here via setDemoMode() before the shell mounts, so every widget can read it
// synchronously without re-fetching or threading a prop. Outside demo mode
// this is always false and every helper below is inert.
import { useSyncExternalStore } from "react";

import { theme } from "./theme";

let demo = false;
const listeners = new Set<() => void>();

export function setDemoMode(v: boolean): void {
  if (v === demo) return;
  demo = v;
  document.body.classList.toggle("demo", v); // CSS hook: disabled-control treatment
  listeners.forEach((l) => l());
}

export const isDemoMode = (): boolean => demo;

export function useDemoMode(): boolean {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    () => demo,
  );
}

export const DEMO_WRITE_LINE = "Disabled in this demo; works in a self-hosted install.";
export const DEMO_DESC_ID = "demo-write-line";
// Spread onto a disabled write control: tooltip + accessible description
// (the single sr-only #demo-write-line element rendered by App).
export const demoWriteProps = (demo: boolean) =>
  demo ? { title: DEMO_WRITE_LINE, "aria-describedby": DEMO_DESC_ID } : {};

// Upstream is cache-only (backend UpstreamDisabled, code UPSTREAM_DISABLED,
// public message "Live upstream fetching is disabled (cache-only demo)"):
// the only error that really means "not in the snapshot".
export const isSnapshotMiss = (msg: string): boolean => /cache-only|UPSTREAM_DISABLED/i.test(msg);

// Persistent quiet inline line for write widgets (same look as the Reports
// demo line). Renders nothing outside demo mode.
export function DemoLine({ text = DEMO_WRITE_LINE, style }: { text?: string; style?: React.CSSProperties }) {
  return (
    <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, padding: "2px 0", ...style }}>
      {text}
    </div>
  );
}

// Same line, rendered only in demo mode (for widgets shared with the private terminal).
export function DemoWriteNote(props: { text?: string; style?: React.CSSProperties }) {
  const on = useDemoMode();
  return on ? <DemoLine {...props} /> : null;
}

// Muted empty state for data absent from the demo snapshot.
export const NOT_IN_SNAPSHOT = "not in demo snapshot";

// Internal-label → visitor-facing label (demo mode only; callers gate).
const LABELS: Record<string, string> = {
  "OFF-API": "background job",
  "LAB:SCAN": "Scanner",
  EMIT: "Generate signals",
  pack: "weekly report",
};
// "CPI.united_states" -> "CPI · United States"
export const humanSeries = (s: string): string => {
  const i = s.indexOf(".");
  if (i < 1) return s;
  const country = s.slice(i + 1).split("_").map((w) => w.charAt(0).toUpperCase() + w.slice(1)).join(" ");
  return `${s.slice(0, i)} · ${country}`;
};
export const humanLabel = (s: string): string => LABELS[s] ?? s;
