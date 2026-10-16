// Backtest Lab (Phase 9, engine) — configure a declarative, signal-driven strategy
// and run it OFF the main API. Pick a logged signal, a rebalance cadence, a
// quantile and costs, then launch: the job is validated pre-flight (structured
// errors shown inline), queued, and run by the worker; progress streams live over
// SSE (JobStatus), and the equity curve + metric bundle render inline when done
// (BacktestReport, lazy). A recent-runs list reloads any prior backtest.
import { Suspense, lazy, useEffect, useRef, useState } from "react";

import { ApiError, api } from "../api/client";
import type { BacktestConfigInput, BacktestJob } from "../api/types";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import { JobStatus } from "./JobStatus";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";
import { InlineError } from "./InlineError";

const LazyReport = lazy(() => import("./BacktestReport").then((m) => ({ default: m.BacktestReport })));

const ctrl: React.CSSProperties = {
  background: theme.bg,
  color: theme.text,
  border: `1px solid ${theme.border}`,
  borderRadius: 3,
  padding: "2px 5px",
  fontSize: theme.fontSize.micro,
  font: "inherit",
};

const iso = (d: Date) => d.toISOString().slice(0, 10);

function defaultStart(): string {
  const d = new Date();
  d.setFullYear(d.getFullYear() - 2);
  return iso(d);
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label style={{ display: "flex", flexDirection: "column", gap: 2, fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.04em" }}>
      {label}
      {children}
    </label>
  );
}

function BacktestLab() {
  const cat = useAsync(() => api.signalCatalog(), []);
  const signalIds = cat.data?.data.signals.map((s) => s.signal_id) ?? [];

  const [signalId, setSignalId] = useState<string>("");
  const [rebalance, setRebalance] = useState<"W" | "M" | "Q">("M");
  const [quantile, setQuantile] = useState(5);
  const [costs, setCosts] = useState(10);
  const [longShort, setLongShort] = useState(false);
  const [weighting, setWeighting] = useState<"equal" | "score">("equal");
  const [start, setStart] = useState(defaultStart());
  const [end, setEnd] = useState(iso(new Date()));
  const [benchmark, setBenchmark] = useState("");

  const [job, setJob] = useState<BacktestJob | null>(null);
  const [reportJobId, setReportJobId] = useState<string | null>(null);
  const [launching, setLaunching] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [recent, setRecent] = useState<BacktestJob[]>([]);
  const reportRef = useRef<HTMLDivElement>(null);

  const sid = signalId || signalIds[0];

  // Scroll the finished report into view so results aren't buried below the form.
  useEffect(() => {
    if (reportJobId) reportRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [reportJobId]);

  const refreshRecent = () => {
    api.listBacktests(8).then((r) => setRecent(r.jobs)).catch(() => {});
  };
  // Load prior runs on mount, not only after a launch.
  useEffect(refreshRecent, []);

  const launch = async () => {
    if (!sid) return;
    setLaunching(true);
    setErr(null);
    setReportJobId(null);
    const cfg: BacktestConfigInput = {
      name: `${sid} · ${rebalance} · Q${quantile}${longShort ? " L/S" : ""}`,
      signal_id: sid,
      start,
      end,
      rebalance,
      quantile,
      costs_bps: costs,
      long_short: longShort,
      weighting,
      benchmark_symbol: benchmark.trim() || null,
    };
    try {
      const j = await api.createBacktest(cfg);
      setJob(j);
      refreshRecent();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : e instanceof Error ? e.message : "launch failed");
    } finally {
      setLaunching(false);
    }
  };

  const openRun = (j: BacktestJob) => {
    setErr(null);
    if (j.status === "done") {
      setJob(null);
      setReportJobId(j.job_id);
    } else {
      setJob(j);
      setReportJobId(null);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        deck={deckLine("backtest-lab")}
        agate={<span style={{ letterSpacing: "0.06em" }}>SIGNAL-DRIVEN · OFF-API</span>}
      />

      <div style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "8px 10px", display: "flex", flexDirection: "column", gap: 10 }}>
        {/* ── config form ── */}
        <div style={{ display: "grid", gridTemplateColumns: "repeat(4, minmax(0,1fr))", gap: 8 }}>
          <Field label="SIGNAL">
            <select style={ctrl} value={sid ?? ""} onChange={(e) => setSignalId(e.target.value)}>
              {signalIds.length === 0 && <option value="">— none logged —</option>}
              {signalIds.map((id) => (
                <option key={id} value={id}>{id}</option>
              ))}
            </select>
          </Field>
          <Field label="REBALANCE">
            <select style={ctrl} value={rebalance} onChange={(e) => setRebalance(e.target.value as "W" | "M" | "Q")}>
              <option value="W">Weekly</option>
              <option value="M">Monthly</option>
              <option value="Q">Quarterly</option>
            </select>
          </Field>
          <Field label="QUANTILE (top 1/n)">
            <input style={ctrl} type="number" min={2} max={10} value={quantile} onChange={(e) => setQuantile(Math.max(2, Math.min(10, Number(e.target.value) || 5)))} />
          </Field>
          <Field label="COSTS (bps)">
            <input style={ctrl} type="number" min={0} max={1000} value={costs} onChange={(e) => setCosts(Math.max(0, Number(e.target.value) || 0))} />
          </Field>
          <Field label="START">
            <input style={ctrl} type="date" value={start} onChange={(e) => setStart(e.target.value)} />
          </Field>
          <Field label="END">
            <input style={ctrl} type="date" value={end} onChange={(e) => setEnd(e.target.value)} />
          </Field>
          <Field label="WEIGHTING">
            <select style={ctrl} value={weighting} onChange={(e) => setWeighting(e.target.value as "equal" | "score")}>
              <option value="equal">Equal</option>
              <option value="score">Score-prop</option>
            </select>
          </Field>
          <Field label="BENCHMARK (opt)">
            <input style={ctrl} type="text" placeholder="^GSPC" value={benchmark} onChange={(e) => setBenchmark(e.target.value)} />
          </Field>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
          <label style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: theme.fontSize.micro, color: theme.text }}>
            <input type="checkbox" checked={longShort} onChange={(e) => setLongShort(e.target.checked)} />
            long / short (top − bottom)
          </label>
          <span style={{ flex: 1 }} />
          <button
            onClick={launch}
            disabled={launching || !sid}
            style={{ padding: "5px 14px", background: theme.panelHeader, color: theme.accent, border: `1px solid ${theme.accent}`, borderRadius: 4, cursor: launching || !sid ? "default" : "pointer", fontSize: theme.fontSize.micro }}
          >
            {launching ? "LAUNCHING…" : "RUN BACKTEST"}
          </button>
        </div>

        {err && <InlineError msg={err} />}

        {/* ── live progress for the running job ── */}
        {job && job.status !== "done" && (
          <div style={{ display: "flex", flexDirection: "column", gap: 4, borderTop: `1px solid ${theme.grid}`, paddingTop: 8 }}>
            <span style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>{job.name}</span>
            <JobStatus
              jobId={job.job_id}
              onComplete={() => {
                setReportJobId(job.job_id);
                setJob(null);
                refreshRecent();
              }}
              onFailed={(msg) => {
                setErr(msg);
                setJob(null);
                refreshRecent();
              }}
            />
          </div>
        )}

        {/* ── the finished report (lazy chart chunk) ── */}
        {reportJobId && (
          <div ref={reportRef} style={{ borderTop: `1px solid ${theme.grid}`, paddingTop: 6, minHeight: 430, display: "flex", flexDirection: "column" }}>
            <Suspense fallback={<PanelState kind="loading" message="loading report…" />}>
              <LazyReport config={{ jobId: reportJobId }} />
            </Suspense>
          </div>
        )}

        {/* ── recent runs ── */}
        <div style={{ borderTop: `1px solid ${theme.grid}`, paddingTop: 6 }}>
          <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: 4 }}>
            <span style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.05em" }}>RECENT RUNS</span>
            <button onClick={refreshRecent} style={{ ...ctrl, cursor: "pointer", padding: "1px 6px", fontSize: theme.fontSize.micro }}>REFRESH</button>
          </div>
          {recent.length === 0 ? (
            <div style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>none yet — run one above.</div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
              {recent.map((j) => (
                <button
                  key={j.job_id}
                  onClick={() => openRun(j)}
                  title={j.job_id}
                  style={{ display: "flex", justifyContent: "space-between", gap: 8, background: "transparent", border: "none", borderBottom: `1px solid ${theme.grid}`, color: theme.text, fontSize: theme.fontSize.micro, padding: "3px 2px", cursor: "pointer", textAlign: "left" }}
                >
                  <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>{j.name}</span>
                  <span style={{ flexShrink: 0, color: j.status === "done" ? theme.up : j.status === "failed" ? theme.down : theme.muted }}>{j.status}</span>
                </button>
              ))}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}

registerWidget({
  id: "backtest-lab",
  title: "Backtest Lab",
  component: BacktestLab,
  defaultSize: { w: 7, h: 20 },
  minSize: { w: 5, h: 12 },
  defaultConfig: {},
  category: "Backtest",
  description: "Configure + run a signal-driven backtest off-API; live progress + equity curve",
});
