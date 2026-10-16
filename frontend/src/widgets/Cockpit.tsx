// Analyst Cockpit. This is a read-only analytical surface:
// assistant output is cited, and a journal draft remains a user action.
import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError, READ_ONLY_DEMO_MESSAGE, api, isReadOnlyDemoError } from "../api/client";
import { InlineError } from "./InlineError";
import type { CockpitJob, CockpitMessage, CockpitSession } from "../api/types";
import { DEMO_WRITE_TITLE, DemoWriteNote, useDemoMode } from "../demo";
import { theme } from "../theme";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import { registerWidget } from "./registry";

const POLL_MS = 1_500;
const NO_PROGRESS_TIMEOUT_MS = 90_000;
const MAX_DRAFT_LENGTH = 4_000;
const ACTIVE_JOB_KEY = "catallax-cockpit-active-job";
type ActiveJob = { sessionKey: string; jobId: string };

function savedJob(): ActiveJob | null {
  try {
    const value = JSON.parse(window.sessionStorage.getItem(ACTIVE_JOB_KEY) ?? "null");
    return typeof value?.sessionKey === "string" && typeof value?.jobId === "string" ? value : null;
  } catch { return null; }
}

function journalDraftHref(content: string): string | null {
  if (!content.trim()) return null;
  const draft = JSON.stringify({ view: content });
  if (draft.length > MAX_DRAFT_LENGTH) return null;
  const query = new URLSearchParams({ draft }).toString();
  return query.length <= 6_000 ? `/portfolio/journal?${query}` : null;
}

const buttonStyle: React.CSSProperties = {
  padding: "4px 8px", background: "transparent", border: `1px solid ${theme.border}`,
  borderRadius: 3, color: theme.text, cursor: "pointer", fontSize: theme.fontSize.micro,
  minWidth: 44, minHeight: 44, boxSizing: "border-box",
};

function CitationChips({ message }: { message: CockpitMessage }) {
  if (!message.citations?.length) return null;
  return (
    <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: 6 }} aria-label="Record citations">
      {message.citations.map((citation, index) => (
        <span key={`${citation.kind}-${citation.id}-${index}`} style={{ color: theme.accent, border: `1px solid ${theme.accent}`, borderRadius: 3, padding: "1px 5px", fontSize: theme.fontSize.micro, letterSpacing: "0.03em" }}>
          {citation.kind} #{citation.id}
        </span>
      ))}
    </div>
  );
}

function Transcript({ messages }: { messages: CockpitMessage[] }) {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }} aria-live="polite">
      {messages.map((message) => {
        const assistant = message.role === "assistant";
        const draftHref = assistant && !message.refused ? journalDraftHref(message.content) : null;
        return (
          <div key={message.id} style={{ borderTop: `1px solid ${theme.border}`, paddingTop: 6 }}>
            <div style={{ color: assistant ? theme.accent : theme.muted, fontSize: theme.fontSize.micro, letterSpacing: "0.05em" }}>
              {assistant ? "ANALYST" : "YOU"}
            </div>
            <div style={{ color: message.refused ? theme.muted : theme.text, fontSize: theme.fontSize.base, lineHeight: 1.45, whiteSpace: "pre-wrap", marginTop: 3 }}>
              {message.content}
            </div>
            <CitationChips message={message} />
            {assistant && !message.refused && (
              <div style={{ border: `1px solid ${theme.border}`, borderRadius: 3, padding: "6px 8px", marginTop: 8, color: theme.muted, fontSize: theme.fontSize.micro }}>
                <div style={{ color: theme.text, letterSpacing: "0.04em", marginBottom: 3 }}>JOURNAL DRAFT</div>
                This is a draft for your review; it cannot write a journal entry.
                {draftHref
                  ? <a href={draftHref} style={{ color: theme.accent, marginLeft: 6 }}>Open Journal Quick Entry</a>
                  : <span style={{ marginLeft: 6 }}>Answer too long to prefill; open Journal Quick Entry manually.</span>}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

function CockpitProgress({ jobId, onComplete, onFailed }: { jobId: string; onComplete: () => void; onFailed: (message: string) => void }) {
  const [job, setJob] = useState<CockpitJob>({ status: "queued", phase: "queued", frac: 0 });
  const [pollError, setPollError] = useState<string | null>(null);
  const [retryKey, setRetryKey] = useState(0);
  const callbacks = useRef({ onComplete, onFailed });
  callbacks.current = { onComplete, onFailed };

  useEffect(() => {
    let stopped = false;
    setJob({ status: "queued", phase: "queued", frac: 0 });
    setPollError(null);
    let pollTimer: number | undefined;
    let timeout: number | undefined;
    let lastProgress = "";
    const armTimeout = () => {
      window.clearTimeout(timeout);
      timeout = window.setTimeout(() => {
        stopped = true;
        window.clearTimeout(pollTimer);
        setPollError("No job progress for 90 seconds — the runner may be stalled. Retry status checks for this job.");
      }, NO_PROGRESS_TIMEOUT_MS);
    };
    const poll = async () => {
      try {
        const next = await api.cockpitJob(jobId);
        if (stopped) return;
        setJob(next);
        if (next.status === "done" || next.status === "failed") {
          stopped = true;
          window.clearTimeout(timeout);
          if (next.status === "done") callbacks.current.onComplete();
          else callbacks.current.onFailed(next.message ?? "cockpit job failed");
          return;
        }
        const progress = JSON.stringify([next.status, next.phase, next.frac]);
        if (progress !== lastProgress) { lastProgress = progress; armTimeout(); }
        pollTimer = window.setTimeout(() => void poll(), POLL_MS);
      } catch (error) {
        if (stopped) return;
        stopped = true;
        window.clearTimeout(timeout);
        setPollError(error instanceof ApiError ? error.message : "Unable to read cockpit job status");
      }
    };
    armTimeout();
    void poll();
    return () => { stopped = true; window.clearTimeout(timeout); window.clearTimeout(pollTimer); };
  }, [jobId, retryKey]);

  if (pollError) return <PanelState kind="error" message={pollError} onRetry={() => setRetryKey((key) => key + 1)} />;
  const pct = Math.round((job.frac ?? 0) * 100);
  return <div style={{ display: "flex", flexDirection: "column", gap: 4 }}><div style={{ display: "flex", justifyContent: "space-between", color: theme.muted, fontSize: theme.fontSize.micro }}><span>{(job.phase ?? job.status).toUpperCase()}</span><span className="num">{pct}%</span></div><div style={{ height: 6, overflow: "hidden", borderRadius: 3, background: theme.grid }}><div style={{ width: `${pct}%`, height: "100%", background: theme.accent }} /></div></div>;
}

function Cockpit() {
  const demo = useDemoMode();
  const [sessions, setSessions] = useState<CockpitSession[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [messages, setMessages] = useState<CockpitMessage[]>([]);
  const [text, setText] = useState("");
  const [activeJob, setActiveJob] = useState<ActiveJob | null>(savedJob);
  const [jobFailure, setJobFailure] = useState<string | null>(null);
  const [uncertainSession, setUncertainSession] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [sessionReady, setSessionReady] = useState(false);
  const submitLock = useRef(false);
  const recoveryLock = useRef(false);
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  const [loading, setLoading] = useState(true);
  const [disabled, setDisabled] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const rememberJob = (job: ActiveJob | null) => {
    setActiveJob(job);
    try {
      if (job) window.sessionStorage.setItem(ACTIVE_JOB_KEY, JSON.stringify(job));
      else window.sessionStorage.removeItem(ACTIVE_JOB_KEY);
    } catch { /* Storage may be unavailable; in-memory state still protects this mount. */ }
  };

  const loadSessions = useCallback(async () => {
    setLoading(true); setError(null);
    try { const result = await api.cockpitSessions(); setSessions(result.sessions); setSelected((current) => current ?? result.sessions[0]?.session_key ?? null); }
    catch (e) { if (e instanceof ApiError && e.code === "COCKPIT_DISABLED") setDisabled(true); else setError(e instanceof Error ? e.message : "Unable to load cockpit sessions"); }
    finally { setLoading(false); }
  }, []);
  useEffect(() => { void loadSessions(); }, []);
  useEffect(() => {
    if (!selected || disabled) return;
    let cancelled = false;
    setMessages([]);
    setSessionReady(false);
    void api.cockpitSession(selected).then((result) => {
      if (cancelled) return;
      setMessages(result.messages);
      setSessionReady(true);
      // Recover a queued turn even if the POST response was lost or the page reloaded.
      const last = result.messages.at(-1);
      if (!activeJob && last?.role === "user" && last.job_id) rememberJob({ sessionKey: selected, jobId: last.job_id });
    }).catch((e) => { if (!cancelled) setError(e instanceof Error ? e.message : "Unable to load session"); });
    return () => { cancelled = true; };
  }, [selected, disabled]);

  const ask = async () => {
    if (demo) return;
    const question = text.trim(); if (!question || (selected && !sessionReady) || submitLock.current || activeJob || jobFailure || uncertainSession) return;
    submitLock.current = true;
    setSubmitting(true);
    setError(null);
    let submittedTo: string | null = null;
    let accepted = false;
    try {
      let key = selected;
      if (!key) { const created = await api.createCockpitSession(); setSessions((current) => [created, ...current]); setSelected(created.session_key); key = created.session_key; }
      submittedTo = key;
      const result = await api.askCockpit(key, question);
      accepted = true;
      setText(""); rememberJob({ sessionKey: key, jobId: result.job_id });
      if (selectedRef.current === key) {
        const session = await api.cockpitSession(key);
        if (selectedRef.current === key) setMessages(session.messages);
      }
    } catch (e) {
      if (isReadOnlyDemoError(e)) { setError(READ_ONLY_DEMO_MESSAGE); return; }
      if (submittedTo && !accepted) setUncertainSession(submittedTo);
      setError(`${e instanceof Error ? e.message : "Unable to queue cockpit question"}. Check the session transcript before submitting again; the request may have been accepted.`);
    }
    finally { submitLock.current = false; setSubmitting(false); }
  };
  const refreshJobSession = (sessionKey: string) => {
    void api.cockpitSession(sessionKey).then((result) => {
      if (selectedRef.current === sessionKey) setMessages(result.messages);
    }).catch((e) => setError(e instanceof Error ? e.message : "Unable to load session"));
  };
  const checkUncertainSubmit = async () => {
    if (!uncertainSession || recoveryLock.current) return;
    recoveryLock.current = true;
    try {
      const result = await api.cockpitSession(uncertainSession);
      if (selectedRef.current === uncertainSession) setMessages(result.messages);
      const last = result.messages.at(-1);
      if (last?.role === "user" && last.job_id) rememberJob({ sessionKey: uncertainSession, jobId: last.job_id });
      setUncertainSession(null);
      setError(null);
    } catch (e) { setError(e instanceof Error ? e.message : "Unable to verify question status"); }
    finally { recoveryLock.current = false; }
  };

  {/* NEW SESSION used to render live and full-brightness
      even when COCKPIT_ENABLED=false (the whole content area below shows
      the "not enabled" empty state at that point) — the one remaining
      interactive control on an otherwise-disabled surface, and brighter
      than ASK ever gets. Hidden, not just disabled, matching this app's own
      convention elsewhere (ReportView's PDF anchor) for "not available
      right now" rather than "available but greyed out". */}
  return <div style={{ display: "flex", flexDirection: "column", height: "100%" }}><PanelHead deck="Read-only, cited answers from your records" agate={<span style={{ letterSpacing: "0.06em" }}>READ-ONLY ANALYST</span>}>{!disabled && <button type="button" style={buttonStyle} onClick={() => { setSelected(null); setMessages([]); }}>NEW SESSION</button>}</PanelHead><div style={{ padding: "8px 10px", minHeight: 0, overflowY: "auto", flex: 1, display: "flex", flexDirection: "column", gap: 10 }}>
    {loading ? <PanelState kind="loading" skeleton="table" /> : disabled ? <PanelState kind="empty" message="Cockpit is not enabled (COCKPIT_ENABLED=false)." hint="Start the AI profile with docker compose --profile ai up -d after enabling the flag." fill /> : <>
      {error === READ_ONLY_DEMO_MESSAGE ? <InlineError msg={error} /> : error && <PanelState kind="error" message={error} onRetry={() => { setError(null); if (selected) refreshJobSession(selected); else void loadSessions(); }} />}
      {uncertainSession && <button type="button" style={buttonStyle} onClick={() => void checkUncertainSubmit()}>Check submitted question before asking again</button>}
      {sessions.length > 1 && <label style={{ color: theme.muted, fontSize: theme.fontSize.micro }}>SESSION <select value={selected ?? ""} onChange={(event) => setSelected(event.target.value)} style={{ marginLeft: 6, ...buttonStyle }}>{sessions.map((session) => <option key={session.session_key} value={session.session_key}>{session.title ?? "Untitled session"}</option>)}</select></label>}
      {messages.length ? <Transcript messages={messages} /> : <PanelState kind="empty" message="Ask a question about the record" hint="Answers cite the records they use; the cockpit cannot write or execute." />}
      {activeJob && <div role="status">{selected !== activeJob.sessionKey && "A question is still processing in another session. "}<CockpitProgress key={activeJob.jobId} jobId={activeJob.jobId} onComplete={() => { refreshJobSession(activeJob.sessionKey); rememberJob(null); }} onFailed={(message) => { refreshJobSession(activeJob.sessionKey); setJobFailure(message); }} /></div>}
      {jobFailure && <PanelState kind="error" message={`Question failed: ${jobFailure}. Review the transcript; no answer was produced. Retry this question only after checking the transcript.`}><button type="button" style={buttonStyle} onClick={() => { rememberJob(null); setJobFailure(null); }}>Acknowledge failure and enable ASK</button></PanelState>}
      <div style={{ display: "flex", flexDirection: "column", gap: 6, borderTop: `1px solid ${theme.border}`, paddingTop: 8 }}><DemoWriteNote text="Read-only demo — questions to the analyst are disabled; the terminal answers them locally." /><label htmlFor="cockpit-question" style={{ color: theme.muted, fontSize: theme.fontSize.micro }}>QUESTION FOR ANALYST</label><div style={{ display: "flex", gap: 6 }}><textarea id="cockpit-question" value={text} onChange={(event) => setText(event.target.value)} placeholder="Ask about your portfolio, reports, forecasts, journal, or notes" rows={2} style={{ flex: 1, minWidth: 0, resize: "vertical", background: theme.bg, color: theme.text, border: `1px solid ${theme.border}`, borderRadius: 3, fontFamily: theme.fontMono, fontSize: theme.fontSize.base, padding: "5px 7px" }} /><button type="button" style={{ ...buttonStyle, flexShrink: 0, color: theme.accent, borderColor: theme.accent }} disabled={demo || !text.trim() || (!!selected && !sessionReady) || submitting || !!activeJob || !!jobFailure || !!uncertainSession} title={demo ? DEMO_WRITE_TITLE : undefined} onClick={() => void ask()}>ASK</button></div></div>
    </>}
  </div></div>;
}

registerWidget({ id: "cockpit", title: "Analyst Cockpit", component: Cockpit, defaultSize: { w: 7, h: 16 }, minSize: { w: 4, h: 8 }, defaultConfig: {}, category: "AI", description: "Cited, read-only analyst conversations over your records", help: "Ask the read-only analyst cockpit about records it can retrieve. Every answer should cite record IDs. It can never write or execute; a journal draft only opens the journal for your review." });
