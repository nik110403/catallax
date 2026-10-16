// Tiny fetch helper: loading / data / error, with optional polling. Widgets own
// their own loading/error states (widget-system rule); this just standardises it.
import { useEffect, useState } from "react";

import { ApiError } from "../api/client";

export interface AsyncState<T> {
  data?: T;
  loading: boolean;
  error?: string;
  // Machine-readable failure code from ApiError.code (e.g. "SIDECAR_NOT_ENABLED"),
  // set only when the rejection was an ApiError — undefined otherwise. Additive:
  // `error` above is unchanged, so existing consumers that only read `error` keep
  // compiling and behaving identically.
  errorCode?: string;
}

export function useAsync<T>(
  fn: () => Promise<T>,
  deps: unknown[],
  refreshMs?: number,
): AsyncState<T> {
  const [state, setState] = useState<AsyncState<T>>({ loading: true });

  useEffect(() => {
    let alive = true;
    const run = () =>
      fn()
        .then((data) => alive && setState({ loading: false, data }))
        .catch(
          (e) =>
            alive &&
            setState({
              loading: false,
              error: e?.message ?? String(e),
              errorCode: e instanceof ApiError ? e.code : undefined,
            }),
        );
    setState((prev) => ({ ...prev, loading: true }));
    run();
    const t = refreshMs ? setInterval(run, refreshMs) : undefined;
    return () => {
      alive = false;
      if (t) clearInterval(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  return state;
}
