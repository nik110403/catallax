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

export const DEMO_WRITE_LINE = "Read-only demo — this action is disabled; the terminal runs it locally.";
// Disabled-control tooltip: the line above is the reason.
export const DEMO_WRITE_TITLE = DEMO_WRITE_LINE;

// Persistent quiet inline line for write widgets (same look as the Reports
// demo line). Renders nothing outside demo mode.
export function DemoLine({ text = DEMO_WRITE_LINE, style }: { text?: string; style?: React.CSSProperties }) {
  return (
    <div role="status" style={{ fontSize: theme.fontSize.micro, color: theme.muted, padding: "2px 0", ...style }}>
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
