import { useEffect, useRef } from "react";

import { namespaceForPath, pathForNamespace } from "./sections";

// Phase 14d — ongoing URL <-> active-tab sync. The INITIAL landing is done in
// workspaces.ts (withHomeLanding reads the URL via landingNamespace), so this hook
// only handles the two ongoing directions:
//   1. active namespace changes in-session (tab/section/sub-nav click, ⌘K) -> push URL
//   2. browser back/forward (popstate) -> activate the popped URL's namespace
// Landing-on-load lives with the state, not here, so a deep link is race-free with
// the async remote hydration (both paths go through withHomeLanding).
export function useUrlSync(activeNamespace: string, focusNamespace: (ns: string) => boolean) {
  const ready = useRef(false);

  // Back/forward. Re-subscribes each render so it always closes over the latest
  // focusNamespace (which captures the current tab set); the listener swap is cheap.
  useEffect(() => {
    const onPop = () => focusNamespace(namespaceForPath(window.location.pathname) ?? "home");
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, [focusNamespace]);

  // Reflect the active tab in the address bar. First run normalizes the URL with
  // replaceState (no history entry) — it handles a loose/unknown initial path like
  // /foo or a trailing slash, since we already landed FROM the URL. Later changes
  // pushState so back/forward walks the section history.
  useEffect(() => {
    const path = pathForNamespace(activeNamespace);
    if (!path) return; // unknown namespace (e.g. a user-created tab) — leave the URL alone
    if (!ready.current) {
      ready.current = true;
      if (path !== window.location.pathname) window.history.replaceState(null, "", path);
      return;
    }
    if (path !== window.location.pathname) window.history.pushState(null, "", path);
  }, [activeNamespace]);
}
