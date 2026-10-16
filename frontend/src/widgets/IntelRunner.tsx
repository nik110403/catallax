// Intel Runner. Trigger a news-enrichment run off the main API (the
// ai-runner worker does the actual Claude Code calls) and watch its live SSE
// progress via the shared JobStatus component (mirrors Backtest Lab's usage).
import { useState } from "react";

import { ApiError, api, intelStreamUrl } from "../api/client";
import { theme } from "../theme";
import { deckLine } from "./help";
import { JobStatus } from "./JobStatus";
import { PanelHead } from "./PanelHead";
import { registerWidget } from "./registry";

function IntelRunner() {
  const [jobId, setJobId] = useState<string | null>(null);
  const [launching, setLaunching] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [last, setLast] = useState<string | null>(null);

  const run = async () => {
    setLaunching(true);
    setErr(null);
    setLast(null);
    try {
      const job = await api.intelEnrich();
      setJobId(job.job_id);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : e instanceof Error ? e.message : "enqueue failed");
    } finally {
      setLaunching(false);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        deck={deckLine("ai-intel-runner")}
        agate={<span style={{ letterSpacing: "0.06em" }}>NEWS ENRICHMENT · OFF-API</span>}
      >
        <button
          onClick={run}
          disabled={launching}
          style={{ padding: "5px 14px", background: theme.panelHeader, color: theme.accent, border: `1px solid ${theme.accent}`, borderRadius: 4, cursor: launching ? "default" : "pointer", fontSize: theme.fontSize.micro }}
        >
          {launching ? "ENQUEUING…" : "ENRICH NOW"}
        </button>
      </PanelHead>
      <div tabIndex={0} style={{ flex: 1, minHeight: 0, overflowY: "auto", display: "flex", flexDirection: "column", gap: 10, padding: "8px 10px" }}>
        {err && (
          <div style={{ fontSize: theme.fontSize.micro, color: theme.down, background: "rgba(248,81,73,0.08)", border: `1px solid ${theme.down}`, borderRadius: 3, padding: "4px 8px" }}>
            {err}
          </div>
        )}

        {jobId && (
          <JobStatus
            jobId={jobId}
            streamUrl={intelStreamUrl}
            onComplete={() => setLast("Enrichment complete — see AI News.")}
            onFailed={(msg) => setErr(msg)}
          />
        )}

        {last && <div style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>{last}</div>}
      </div>
    </div>
  );
}

registerWidget({
  id: "ai-intel-runner",
  title: "Intel Runner",
  component: IntelRunner,
  defaultSize: { w: 5, h: 6 },
  minSize: { w: 3, h: 4 },
  defaultConfig: {},
  category: "AI",
  description: "Trigger a news-enrichment run and watch its live progress.",
});
