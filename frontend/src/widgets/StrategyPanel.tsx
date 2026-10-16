// Strategy Panel (Phase 13, Algo Hub). Signal-driven strategy CRUD (create /
// start / pause / delete) against the paper OMS's strategy runner, plus the
// global kill switch. Engine dot reflects the worker heartbeat (algoStatus);
// status/create fetch their own state (not useAsync) so kill/resume and
// create/start/pause/delete get an instant local refresh instead of waiting
// out the poll interval.
import { useEffect, useRef, useState } from "react";

import { ApiError, api } from "../api/client";
import type { AlgoStatus, AlgoStrategy, AlgoStrategyCreate } from "../api/types";
import { NOT_IN_SNAPSHOT, DEMO_WRITE_TITLE, DemoWriteNote, useDemoMode } from "../demo";
import { theme } from "../theme";
import { controlStyle } from "./chartTheme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";
import { InlineError } from "./InlineError";

const STATUS_COLOR: Record<string, string> = {
  running: theme.up,
  active: theme.up,
  paused: theme.muted,
  stopped: theme.muted,
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

function StrategyPanel() {
  const demo = useDemoMode();
  const [status, setStatus] = useState<AlgoStatus | null>(null);
  const [strategies, setStrategies] = useState<AlgoStrategy[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState<number | null>(null);
  const [killing, setKilling] = useState(false);
  // Inline two-step confirm state (replaces the old browser confirm dialog):
  // first click arms the "confirming" label, second click commits; blur/Escape cancels.
  const [confirmingKill, setConfirmingKill] = useState(false);
  const [confirmingDeleteId, setConfirmingDeleteId] = useState<number | null>(null);

  const catalog = useAsync(() => api.signalCatalog(), []);
  const signalIds = catalog.data?.data.signals.map((s) => s.signal_id) ?? [];

  const [name, setName] = useState("");
  const [signalId, setSignalId] = useState("");
  const [universe, setUniverse] = useState("");
  const [topN, setTopN] = useState(10);
  const [rebalance, setRebalance] = useState<"daily" | "weekly" | "monthly">("weekly");
  const [creating, setCreating] = useState(false);

  // Tracks component liveness so refreshStatus/refreshStrategies (called from
  // the effect's poll and from create/act/del/toggleKill handlers) never set
  // state after unmount.
  const aliveRef = useRef(true);

  const refreshStatus = () => api.algoStatus().then((s) => aliveRef.current && setStatus(s)).catch(() => {});
  const refreshStrategies = () =>
    api
      .algoStrategies()
      .then((r) => aliveRef.current && setStrategies(r.strategies))
      .catch((e) => aliveRef.current && setErr(e instanceof ApiError ? e.message : "load failed"));

  useEffect(() => {
    aliveRef.current = true;
    refreshStatus();
    refreshStrategies();
    const t = setInterval(() => {
      refreshStatus();
      refreshStrategies();
    }, 15_000);
    return () => {
      aliveRef.current = false;
      clearInterval(t);
    };
  }, []);

  const create = async (e: React.FormEvent) => {
    e.preventDefault();
    if (demo) return;
    setCreating(true);
    setErr(null);
    const sid = signalId || signalIds[0] || "";
    const body: AlgoStrategyCreate = {
      name: name.trim() || `strategy-${Date.now().toString(36)}`,
      signal_id: sid,
      universe: universe
        .split(",")
        .map((s) => s.trim().toUpperCase())
        .filter(Boolean),
      top_n: topN,
      rebalance,
    };
    try {
      await api.algoCreateStrategy(body);
      setName("");
      setUniverse("");
      refreshStrategies();
    } catch (e2) {
      setErr(e2 instanceof ApiError ? e2.message : e2 instanceof Error ? e2.message : "create failed");
    } finally {
      setCreating(false);
    }
  };

  const act = async (s: AlgoStrategy, action: "start" | "pause") => {
    if (demo) return;
    setBusy(s.id);
    try {
      await api.algoStrategyAction(s.id, action);
      refreshStrategies();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "action failed");
    } finally {
      setBusy(null);
    }
  };

  const del = async (s: AlgoStrategy) => {
    if (demo) return;
    setBusy(s.id);
    try {
      await api.algoDeleteStrategy(s.id);
      refreshStrategies();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "delete failed");
    } finally {
      setBusy(null);
    }
  };

  const killed = status?.kill === true;
  const engineOk = status?.worker_heartbeat_fresh === true;

  const toggleKill = async () => {
    if (demo) return;
    setKilling(true);
    setErr(null);
    try {
      if (killed) await api.algoResume();
      else await api.algoKill();
      refreshStatus();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "kill/resume failed");
    } finally {
      setKilling(false);
    }
  };

  // Resume needs no confirmation (matches prior behaviour); engaging the kill
  // switch is safety-critical, so it stays a deliberate two-step click.
  const handleKillClick = () => {
    if (demo) return; // kill switch is disabled in demo, never confirm-then-refuse
    if (killed) {
      void toggleKill();
      return;
    }
    if (!confirmingKill) {
      setConfirmingKill(true);
      return;
    }
    setConfirmingKill(false);
    void toggleKill();
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead deck={deckLine("algo-strategies")} />
      <div
        style={{
          display: "flex",
          justifyContent: "space-between",
          alignItems: "center",
          padding: "6px 8px",
          borderBottom: `1px solid ${theme.border}`,
        }}
      >
        <span style={{ fontSize: theme.fontSize.micro, color: theme.muted, display: "flex", alignItems: "center", gap: 6 }}>
          <span title={engineOk ? "worker heartbeat fresh" : "worker heartbeat stale"} style={{ color: engineOk ? theme.up : demo ? theme.muted : theme.down }}>
            ●
          </span>
          ENGINE {status?.broker ?? ""}
        </span>
        <button
          onClick={handleKillClick}
          onBlur={() => setConfirmingKill(false)}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              setConfirmingKill(false);
              e.currentTarget.blur();
            }
          }}
          disabled={killing || demo}
          title={demo ? DEMO_WRITE_TITLE : undefined}
          style={{
            padding: "4px 12px",
            background: killed ? theme.panelHeader : confirmingKill ? theme.down : "rgba(248,81,73,0.12)",
            color: killed ? theme.up : confirmingKill ? theme.bg : theme.down,
            border: `1px solid ${killed ? theme.up : theme.down}`,
            borderRadius: 4,
            cursor: killing ? "default" : "pointer",
            fontSize: theme.fontSize.micro,
            fontWeight: 700,
          }}
        >
          {killing ? "…" : killed ? "RESUME" : confirmingKill ? "CONFIRM KILL?" : "KILL SWITCH"}
        </button>
      </div>

      <div tabIndex={0} style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "8px 10px", display: "flex", flexDirection: "column", gap: 10 }}>
        <DemoWriteNote />
        {err && <InlineError msg={err} />}

        <div style={{ display: "flex", flexDirection: "column", gap: 4 }}>
          {strategies.map((s) => (
            <div
              key={s.id}
              style={{
                display: "flex",
                justifyContent: "space-between",
                alignItems: "center",
                gap: 6,
                borderBottom: `1px solid ${theme.grid}`,
                padding: "3px 2px",
              }}
            >
              <span style={{ display: "flex", alignItems: "center", gap: 6, minWidth: 0 }}>
                <span style={{ color: STATUS_COLOR[s.status] ?? theme.muted }}>●</span>
                <span style={{ fontSize: theme.fontSize.micro, fontWeight: 600, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                  {s.name}
                </span>
                <span style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>
                  {s.last_rebalance_at ? s.last_rebalance_at.slice(0, 10) : "never rebalanced"}
                </span>
              </span>
              <span style={{ display: "flex", gap: 4, flexShrink: 0 }}>
                {s.status === "running" || s.status === "active" ? (
                  <button onClick={() => act(s, "pause")} disabled={busy === s.id || demo} title={demo ? DEMO_WRITE_TITLE : undefined} style={ctrlBtn}>
                    PAUSE
                  </button>
                ) : (
                  <button onClick={() => act(s, "start")} disabled={busy === s.id || demo} title={demo ? DEMO_WRITE_TITLE : undefined} style={ctrlBtn}>
                    START
                  </button>
                )}
                <button
                  onClick={() => {
                    if (confirmingDeleteId === s.id) {
                      setConfirmingDeleteId(null);
                      void del(s);
                    } else {
                      setConfirmingDeleteId(s.id);
                    }
                  }}
                  onBlur={() => setConfirmingDeleteId((id) => (id === s.id ? null : id))}
                  onKeyDown={(e) => {
                    if (e.key === "Escape") {
                      setConfirmingDeleteId(null);
                      e.currentTarget.blur();
                    }
                  }}
                  disabled={busy === s.id || demo}
                  title={demo ? DEMO_WRITE_TITLE : undefined}
                  style={
                    confirmingDeleteId === s.id
                      ? { ...ctrlBtn, color: theme.bg, background: theme.down, border: `1px solid ${theme.down}` }
                      : { ...ctrlBtn, color: theme.down }
                  }
                >
                  {confirmingDeleteId === s.id ? "CONFIRM?" : "DEL"}
                </button>
              </span>
            </div>
          ))}
          {!strategies.length && <div style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>{demo ? NOT_IN_SNAPSHOT : "No strategies yet — create one below."}</div>}
        </div>

        <form onSubmit={create} style={{ display: "flex", flexDirection: "column", gap: 6, borderTop: `1px solid ${theme.grid}`, paddingTop: 8 }}>
          <span style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.05em" }}>NEW STRATEGY</span>
          <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
            <Field label="NAME">
              <input style={controlStyle} value={name} onChange={(e) => setName(e.target.value)} placeholder="my-strategy" />
            </Field>
            <Field label="SIGNAL">
              <select style={controlStyle} value={signalId || signalIds[0] || ""} onChange={(e) => setSignalId(e.target.value)}>
                {signalIds.length === 0 && <option value="">— none logged —</option>}
                {signalIds.map((id) => (
                  <option key={id} value={id}>
                    {id}
                  </option>
                ))}
              </select>
            </Field>
            <Field label="TOP N">
              <input
                style={controlStyle}
                type="number"
                min={1}
                max={50}
                value={topN}
                onChange={(e) => setTopN(Math.max(1, Number(e.target.value) || 10))}
              />
            </Field>
            <Field label="CADENCE">
              <select style={controlStyle} value={rebalance} onChange={(e) => setRebalance(e.target.value as typeof rebalance)}>
                <option value="daily">Daily</option>
                <option value="weekly">Weekly</option>
                <option value="monthly">Monthly</option>
              </select>
            </Field>
            <Field label="UNIVERSE (comma-separated)">
              <textarea
                style={{ ...controlStyle, gridColumn: "1 / -1", resize: "vertical", minHeight: 40 }}
                value={universe}
                onChange={(e) => setUniverse(e.target.value)}
                placeholder="AAPL, MSFT, GOOGL"
              />
            </Field>
          </div>
          <button
            type="submit"
            disabled={demo || creating || !signalIds.length}
            title={demo ? DEMO_WRITE_TITLE : undefined}
            style={{
              alignSelf: "flex-start",
              padding: "5px 14px",
              background: theme.panelHeader,
              color: theme.accent,
              border: `1px solid ${theme.accent}`,
              borderRadius: 4,
              cursor: creating ? "default" : "pointer",
              fontSize: theme.fontSize.micro,
            }}
          >
            {creating ? "CREATING…" : "CREATE STRATEGY"}
          </button>
        </form>
      </div>
    </div>
  );
}

const ctrlBtn: React.CSSProperties = {
  background: "transparent",
  border: `1px solid ${theme.border}`,
  color: theme.text,
  borderRadius: 3,
  padding: "1px 6px",
  fontSize: theme.fontSize.micro,
  cursor: "pointer",
};

registerWidget({
  id: "algo-strategies",
  title: "Strategy Panel",
  component: StrategyPanel,
  defaultSize: { w: 5, h: 9 },
  minSize: { w: 4, h: 6 },
  defaultConfig: {},
  category: "Algo",
  description: "Signal-driven strategy CRUD, start/pause/delete, and the global kill switch.",
});
