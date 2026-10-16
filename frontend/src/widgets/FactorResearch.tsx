// Factor Research (Phase 11, Quant/ML Lab). IC study runner: pick a factor,
// horizon, and quantile count; POST to create an off-API job; stream live
// progress via SSE; render IC series + quantile-return bars inline on complete.
// Recent-runs list reloads any prior factor study.
import { useEffect, useRef, useState } from "react";
import {
  Bar,
  BarChart,
  CartesianGrid,
  Cell,
  Line,
  LineChart,
  ReferenceLine,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";

import { ApiError, api, labStreamUrl } from "../api/client";
import type { Envelope, FactorReport, LabJob } from "../api/types";
import { DEMO_WRITE_TITLE, DemoWriteNote, useDemoMode } from "../demo";
import { theme } from "../theme";
import { deckLine } from "./help";
import { JobStatus } from "./JobStatus";
import { PanelHead } from "./PanelHead";
import { Provenance } from "./Provenance";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";
import { InlineError } from "./InlineError";

const FALLBACK_FACTORS = ["momentum", "reversal", "lowvol", "trend", "rsi", "volume"];

const ctrl: React.CSSProperties = {
  background: theme.bg,
  color: theme.text,
  border: `1px solid ${theme.border}`,
  borderRadius: 3,
  padding: "2px 5px",
  fontSize: theme.fontSize.micro,
  font: "inherit",
};

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <label
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 2,
        fontSize: theme.fontSize.micro,
        color: theme.muted,
        letterSpacing: "0.04em",
      }}
    >
      {label}
      {children}
    </label>
  );
}

function fmt3(v: number | null): string {
  return v === null ? "—" : v.toFixed(3);
}
function fmtHitRate(v: number | null): string {
  return v === null ? "—" : `${(v * 100).toFixed(1)}%`;
}

// ── Tooltip sub-components ───────────────────────────────────────────────────

function ICTooltip({
  active,
  payload,
}: {
  active?: boolean;
  payload?: Array<{ payload: { date: string; ic: number } }>;
}) {
  if (!active || !payload?.length) return null;
  const p = payload[0].payload;
  return (
    <div
      style={{
        background: theme.panel,
        border: `1px solid ${theme.border}`,
        borderRadius: 3,
        padding: "2px 6px",
        fontSize: theme.fontSize.micro,
        whiteSpace: "nowrap",
      }}
    >
      <span style={{ color: theme.muted }}>{p.date}</span>{" "}
      <span className="num" style={{ color: p.ic >= 0 ? theme.up : theme.down }}>
        {p.ic.toFixed(3)}
      </span>
    </div>
  );
}

function QRetTooltip({
  active,
  payload,
}: {
  active?: boolean;
  payload?: Array<{ payload: { quantile: number; mean_fwd_ret: number | null } }>;
}) {
  if (!active || !payload?.length) return null;
  const p = payload[0].payload;
  return (
    <div
      style={{
        background: theme.panel,
        border: `1px solid ${theme.border}`,
        borderRadius: 3,
        padding: "2px 6px",
        fontSize: theme.fontSize.micro,
        whiteSpace: "nowrap",
      }}
    >
      <span style={{ color: theme.muted }}>Q{p.quantile}</span>{" "}
      <span
        className="num"
        style={{
          color:
            p.mean_fwd_ret !== null && p.mean_fwd_ret >= 0 ? theme.up : theme.down,
        }}
      >
        {p.mean_fwd_ret !== null ? `${p.mean_fwd_ret.toFixed(2)}%` : "—"}
      </span>
    </div>
  );
}

// ── Inline report renderer ───────────────────────────────────────────────────

function FactorReportView({ env }: { env: Envelope<FactorReport> }) {
  const d = env.data;
  const icSeries = d.ic_series.filter((p) => p.ic !== null);
  const spread = d.long_short_spread;

  const stats: { label: string; val: string; color: string }[] = [
    {
      label: "IC MEAN",
      val: fmt3(d.ic_mean),
      color: d.ic_mean !== null && d.ic_mean > 0 ? theme.up : theme.down,
    },
    { label: "IC IR", val: fmt3(d.ic_ir), color: theme.text },
    { label: "IC T-STAT", val: fmt3(d.ic_tstat), color: theme.text },
    { label: "HIT RATE", val: fmtHitRate(d.ic_hit_rate), color: theme.text },
    { label: "N PERIODS", val: String(d.n_periods), color: theme.text },
    { label: "N SYMBOLS", val: String(d.n_symbols), color: theme.text },
  ];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {/* header stats */}
      <div
        style={{
          display: "grid",
          gridTemplateColumns: "repeat(6, minmax(0,1fr))",
          gap: 4,
        }}
      >
        {stats.map(({ label, val, color }) => (
          <div
            key={label}
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 1,
              alignItems: "center",
              background: theme.panelHeader,
              borderRadius: 3,
              padding: "4px 2px",
            }}
          >
            <span style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.04em" }}>
              {label}
            </span>
            <span className="num" style={{ fontSize: theme.fontSize.micro, color }}>
              {val}
            </span>
          </div>
        ))}
      </div>

      {/* IC series line chart */}
      {icSeries.length > 1 && (
        <div style={{ height: 120 }}>
          <ResponsiveContainer width="100%" height="100%">
            <LineChart
              data={icSeries}
              margin={{ top: 4, right: 8, bottom: 14, left: 0 }}
            >
              <CartesianGrid stroke={theme.grid} strokeWidth={0.5} vertical={false} />
              <XAxis
                dataKey="date"
                tick={{ fill: theme.muted, fontSize: theme.fontSize.micro }}
                stroke={theme.border}
                tickLine={false}
                tickFormatter={(v: string) => v.slice(0, 7)}
                interval="preserveStartEnd"
              />
              <YAxis
                tick={{ fill: theme.muted, fontSize: theme.fontSize.micro }}
                stroke={theme.border}
                tickLine={false}
                width={32}
                tickFormatter={(v: number) => v.toFixed(2)}
              />
              <ReferenceLine
                y={0}
                stroke={theme.muted}
                strokeDasharray="3 2"
                strokeWidth={0.8}
              />
              <Tooltip
                content={<ICTooltip />}
                cursor={{ stroke: theme.muted, strokeWidth: 0.5, strokeDasharray: "2 2" }}
                wrapperStyle={{ outline: "none" }}
              />
              <Line
                type="monotone"
                dataKey="ic"
                stroke={theme.accent}
                strokeWidth={1}
                dot={false}
                isAnimationActive={false}
                connectNulls={false}
              />
            </LineChart>
          </ResponsiveContainer>
        </div>
      )}

      {/* quantile returns bar chart */}
      {d.quantile_returns.length > 0 && (
        <div>
          <div
            style={{
              fontSize: theme.fontSize.micro,
              color: theme.muted,
              letterSpacing: "0.04em",
              marginBottom: 3,
              display: "flex",
              alignItems: "center",
              gap: 8,
            }}
          >
            <span>QUANTILE RETURNS</span>
            {spread !== null && (
              <span
                className="num"
                style={{ color: spread >= 0 ? theme.up : theme.down }}
              >
                L/S {spread >= 0 ? "+" : ""}
                {spread.toFixed(2)}%
              </span>
            )}
          </div>
          <div style={{ height: 80 }}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart
                data={d.quantile_returns}
                margin={{ top: 0, right: 8, bottom: 14, left: 0 }}
              >
                <CartesianGrid stroke={theme.grid} strokeWidth={0.5} vertical={false} />
                <XAxis
                  dataKey="quantile"
                  tick={{ fill: theme.muted, fontSize: theme.fontSize.micro }}
                  stroke={theme.border}
                  tickLine={false}
                />
                <YAxis
                  tick={{ fill: theme.muted, fontSize: theme.fontSize.micro }}
                  stroke={theme.border}
                  tickLine={false}
                  width={32}
                  tickFormatter={(v: number) => `${v.toFixed(1)}%`}
                />
                <Tooltip
                  content={<QRetTooltip />}
                  cursor={{ fill: "rgba(255,255,255,0.03)" }}
                  wrapperStyle={{ outline: "none" }}
                />
                <Bar dataKey="mean_fwd_ret" isAnimationActive={false}>
                  {d.quantile_returns.map((entry) => (
                    <Cell
                      key={entry.quantile}
                      fill={
                        entry.mean_fwd_ret !== null && entry.mean_fwd_ret >= 0
                          ? theme.up
                          : theme.down
                      }
                    />
                  ))}
                </Bar>
              </BarChart>
            </ResponsiveContainer>
          </div>
        </div>
      )}

      <Provenance
        source={env.source}
        cacheHit={env.cache_hit}
        stale={env.stale}
        warnings={env.warnings}
        fetchedAt={env.fetched_at}
      />
    </div>
  );
}

// ── Main component ────────────────────────────────────────────────────────────

function FactorResearch() {
  const demo = useDemoMode();
  const enginesAsync = useAsync(() => api.labEngines(), []);
  const factors = enginesAsync.data?.factors ?? FALLBACK_FACTORS;

  const [factor, setFactor] = useState("");
  const [horizon, setHorizon] = useState(21);
  const [quantiles, setQuantiles] = useState(5);

  const [job, setJob] = useState<LabJob | null>(null);
  const [report, setReport] = useState<Envelope<FactorReport> | null>(null);
  const [reportJobId, setReportJobId] = useState<string | null>(null);
  const [launching, setLaunching] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [recent, setRecent] = useState<LabJob[]>([]);
  const reportRef = useRef<HTMLDivElement>(null);

  const activeFactor = factor || factors[0] || "momentum";

  useEffect(() => {
    if (reportJobId) reportRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
  }, [reportJobId]);

  const refreshRecent = () => {
    api
      .listLabRuns(8)
      .then((r) => setRecent(r.jobs.filter((j) => j.kind === "factor")))
      .catch(() => {});
  };
  // Load prior runs on mount, not only after a launch.
  useEffect(refreshRecent, []);

  const launch = async () => {
    if (demo) return;
    setLaunching(true);
    setErr(null);
    setReport(null);
    setReportJobId(null);
    try {
      const j = await api.createFactorStudy({
        name: `${activeFactor} · h${horizon} · Q${quantiles}`,
        factor: activeFactor,
        horizon_days: horizon,
        quantiles,
      });
      setJob(j);
      refreshRecent();
    } catch (e) {
      setErr(
        e instanceof ApiError
          ? e.message
          : e instanceof Error
            ? e.message
            : "launch failed",
      );
    } finally {
      setLaunching(false);
    }
  };

  const openRun = async (j: LabJob) => {
    setErr(null);
    if (j.status === "done") {
      setJob(null);
      setReport(null);
      setReportJobId(null);
      try {
        const env = await api.labFactorReport(j.job_id);
        setReport(env);
        setReportJobId(j.job_id);
      } catch (e) {
        setErr(
          e instanceof ApiError
            ? e.message
            : e instanceof Error
              ? e.message
              : "load failed",
        );
      }
    } else {
      setJob(j);
      setReport(null);
      setReportJobId(null);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        figure={report ? fmt3(report.data.ic_mean) : undefined}
        deck={deckLine("lab-factor")}
        agate={<span style={{ letterSpacing: "0.06em" }}>{demo ? "BACKGROUND JOB" : "OFF-API"}</span>}
      />

      <div
        style={{
          flex: 1,
          minHeight: 0,
          overflowY: "auto",
          padding: "8px 10px",
          display: "flex",
          flexDirection: "column",
          gap: 10,
        }}
      >
        {/* config form */}
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "repeat(3, minmax(0,1fr))",
            gap: 8,
          }}
        >
          <Field label="FACTOR">
            <select
              style={ctrl}
              value={activeFactor}
              onChange={(e) => setFactor(e.target.value)}
            >
              {factors.map((f) => (
                <option key={f} value={f}>
                  {f}
                </option>
              ))}
            </select>
          </Field>
          <Field label="HORIZON (days)">
            <input
              style={ctrl}
              type="number"
              min={1}
              max={252}
              value={horizon}
              onChange={(e) =>
                setHorizon(Math.max(1, Math.min(252, Number(e.target.value) || 21)))
              }
            />
          </Field>
          <Field label="QUANTILES">
            <input
              style={ctrl}
              type="number"
              min={2}
              max={10}
              value={quantiles}
              onChange={(e) =>
                setQuantiles(Math.max(2, Math.min(10, Number(e.target.value) || 5)))
              }
            />
          </Field>
        </div>

        <div style={{ display: "flex", justifyContent: "flex-end" }}>
          <button
            onClick={launch}
            disabled={launching || demo}
            title={demo ? DEMO_WRITE_TITLE : undefined}
            style={{
              padding: "5px 14px",
              background: theme.panelHeader,
              color: theme.accent,
              border: `1px solid ${theme.accent}`,
              borderRadius: 4,
              cursor: launching ? "default" : "pointer",
              fontSize: theme.fontSize.micro,
            }}
          >
            {launching ? "LAUNCHING…" : "RUN STUDY"}
          </button>
        </div>

        <DemoWriteNote />
        {err && <InlineError msg={err} />}

        {/* live progress */}
        {job && job.status !== "done" && (
          <div
            style={{
              display: "flex",
              flexDirection: "column",
              gap: 4,
              borderTop: `1px solid ${theme.grid}`,
              paddingTop: 8,
            }}
          >
            <span style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>{job.name}</span>
            <JobStatus
              jobId={job.job_id}
              streamUrl={labStreamUrl}
              onComplete={async () => {
                const jid = job.job_id;
                setJob(null);
                refreshRecent();
                try {
                  const env = await api.labFactorReport(jid);
                  setReport(env);
                  setReportJobId(jid);
                } catch (e) {
                  setErr(
                    e instanceof ApiError
                      ? e.message
                      : e instanceof Error
                        ? e.message
                        : "report load failed",
                  );
                }
              }}
              onFailed={(msg) => {
                setErr(msg);
                setJob(null);
                refreshRecent();
              }}
            />
          </div>
        )}

        {/* inline report */}
        {report && (
          <div
            ref={reportRef}
            style={{ borderTop: `1px solid ${theme.grid}`, paddingTop: 6 }}
          >
            <FactorReportView env={report} />
          </div>
        )}

        {/* recent runs */}
        <div style={{ borderTop: `1px solid ${theme.grid}`, paddingTop: 6 }}>
          <div
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              marginBottom: 4,
            }}
          >
            <span style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.05em" }}>
              RECENT RUNS
            </span>
            <button
              onClick={refreshRecent}
              style={{ ...ctrl, cursor: "pointer", padding: "1px 6px", fontSize: theme.fontSize.micro }}
            >
              REFRESH
            </button>
          </div>
          {recent.length === 0 ? (
            <div style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>
              none yet — run one above.
            </div>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
              {recent.map((j, idx) => (
                <button
                  key={j.job_id}
                  onClick={() => openRun(j)}
                  title={j.job_id}
                  style={{
                    display: "flex",
                    justifyContent: "space-between",
                    gap: 8,
                    background: "transparent",
                    border: "none",
                    borderBottom: idx < recent.length - 1 ? `1px solid ${theme.grid}` : "none",
                    color: theme.text,
                    fontSize: theme.fontSize.micro,
                    padding: "3px 2px",
                    cursor: "pointer",
                    textAlign: "left",
                  }}
                >
                  <span
                    style={{
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {j.name}
                  </span>
                  <span
                    style={{
                      flexShrink: 0,
                      color:
                        j.status === "done"
                          ? theme.up
                          : j.status === "failed"
                            ? theme.down
                            : theme.muted,
                    }}
                  >
                    {j.status}
                  </span>
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
  id: "lab-factor",
  title: "Factor Research",
  component: FactorResearch,
  defaultSize: { w: 6, h: 18 },
  minSize: { w: 4, h: 12 },
  defaultConfig: {},
  category: "Quant Lab",
  description:
    "Factor IC study runner: pick a factor, horizon, and quantile count; IC series + quantile-return breakdown",
});
