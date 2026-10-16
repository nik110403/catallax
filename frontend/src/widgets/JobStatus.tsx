// SSE job-status progress (Phase 9, engine). Streams a backtest job's live
// progress over Server-Sent Events (GET /backtests/{id}/stream) — a progress bar
// + phase label that resolves to done / failed. The backend emits the current
// state on connect (so a late subscriber isn't stuck) then live deltas; the server
// closes the stream on a terminal state. Used inline by BacktestLab.
import { useEffect, useRef, useState } from "react";

import { backtestStreamUrl } from "../api/client";
import { theme } from "../theme";
import { PanelState } from "./PanelState";

interface Progress {
  status: string; // queued | running | done | failed
  progress: number; // 0..1
  phase?: string | null;
  error?: string;
}

// No progress event/update arriving within this window means the job runner
// has gone quiet — stop showing "running" and surface an error instead of
// spinning forever (Phase 14e: "no infinite spinner reachable").
const NO_PROGRESS_TIMEOUT_MS = 90_000;

export function JobStatus({
  jobId,
  onComplete,
  onFailed,
  streamUrl = backtestStreamUrl,
}: {
  jobId: string;
  onComplete?: () => void;
  onFailed?: (msg: string) => void;
  // Which SSE endpoint to subscribe to — defaults to the backtester; the
  // Quant/ML Lab passes `labStreamUrl`. Both emit the same frame shape.
  streamUrl?: (jobId: string) => string;
}) {
  const [p, setP] = useState<Progress>({ status: "queued", progress: 0 });
  const [timedOut, setTimedOut] = useState(false);
  const [retryKey, setRetryKey] = useState(0);
  const doneRef = useRef(false);

  useEffect(() => {
    doneRef.current = false;
    setP({ status: "queued", progress: 0 });
    setTimedOut(false);
    const es = new EventSource(streamUrl(jobId));

    // Re-armed on every received update; if it ever fires, no update showed up
    // for NO_PROGRESS_TIMEOUT_MS and we treat the job runner as unresponsive.
    let timer: number | undefined;
    const armTimeout = () => {
      clearTimeout(timer);
      timer = setTimeout(() => {
        if (doneRef.current) return;
        doneRef.current = true;
        es.close();
        setTimedOut(true);
      }, NO_PROGRESS_TIMEOUT_MS);
    };

    const finish = (status: string, error?: string) => {
      if (doneRef.current) return;
      doneRef.current = true;
      clearTimeout(timer);
      es.close();
      if (status === "done") onComplete?.();
      else if (status === "failed") onFailed?.(error ?? "backtest failed");
    };

    es.onmessage = (e) => {
      armTimeout();
      try {
        const msg = JSON.parse(e.data) as Progress;
        setP(msg);
        if (msg.status === "done" || msg.status === "failed") finish(msg.status, msg.error);
      } catch {
        /* heartbeat / non-JSON frame */
      }
    };
    // The server sends `event: end` after a terminal message, then closes.
    es.addEventListener("end", () => es.close());
    es.onerror = () => {
      // The stream closing after completion surfaces as an error — ignore once done;
      // otherwise EventSource auto-reconnects and re-reads the current state.
      if (doneRef.current) es.close();
    };
    armTimeout();
    return () => {
      doneRef.current = true;
      clearTimeout(timer);
      es.close();
    };
  }, [jobId, retryKey]);

  if (timedOut) {
    return (
      <PanelState
        kind="error"
        message="No response from the job runner — it may not be running."
        onRetry={() => setRetryKey((k) => k + 1)}
      />
    );
  }

  const pct = Math.round((p.progress ?? 0) * 100);
  const failed = p.status === "failed";
  const done = p.status === "done";
  const barColor = failed ? theme.down : done ? theme.up : theme.accent;

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
      <div style={{ display: "flex", justifyContent: "space-between", fontSize: theme.fontSize.micro, color: theme.muted }} aria-live="polite">
        <span style={{ letterSpacing: "0.04em" }}>
          {failed ? "FAILED" : done ? "DONE" : (p.phase || p.status || "queued").toUpperCase()}
        </span>
        <span className="num">{pct}%</span>
      </div>
      <div style={{ background: theme.grid, height: 6, borderRadius: 3, overflow: "hidden" }}>
        <div
          style={{
            width: `${pct}%`,
            height: "100%",
            background: barColor,
            transition: "width 0.3s ease",
          }}
        />
      </div>
      {failed && p.error && <div style={{ fontSize: theme.fontSize.micro, color: theme.down }}>{p.error}</div>}
    </div>
  );
}
