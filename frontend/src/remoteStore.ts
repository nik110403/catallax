// Server-side persistence for UI state. Thin client over
// GET/PUT /api/v1/layouts/{key}: a key→JSON store so workspaces + layout presets
// follow the user across devices. localStorage stays the synchronous source of
// truth for first paint and the offline fallback; this layer hydrates from and
// writes through to the backend. Writes are debounced; failures are swallowed
// (offline → localStorage still holds the state).

import { notifyUnauthorized } from "./api/client";

const BASE = "/api/v1/layouts";

// Returns the stored blob for a key, or null if absent / unreachable.
export async function loadRemote(key: string): Promise<unknown | null> {
  try {
    const res = await fetch(`${BASE}/${encodeURIComponent(key)}`);
    // This layer bypasses api/client.ts (raw fetch), so it must report a 401
    // itself — otherwise a mid-session cookie expiry that only the background
    // workspace sync notices would leave the app unlocked with dead writes.
    notifyUnauthorized(res.status);
    if (!res.ok) return null; // 404 (no state yet) or transient → fall back to local
    const body = await res.json();
    return body?.data ?? null;
  } catch {
    return null;
  }
}

const timers = new Map<string, ReturnType<typeof setTimeout>>();

// Debounced write-through. Coalesces rapid edits (drag/resize) into one PUT.
export function saveRemote(key: string, value: unknown, delayMs = 600): void {
  const prev = timers.get(key);
  if (prev) clearTimeout(prev);
  timers.set(
    key,
    setTimeout(() => {
      timers.delete(key);
      fetch(`${BASE}/${encodeURIComponent(key)}`, {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ data: value }),
      })
        .then((res) => notifyUnauthorized(res.status))
        .catch(() => {
          /* offline → localStorage remains the fallback */
        });
    }, delayMs),
  );
}
