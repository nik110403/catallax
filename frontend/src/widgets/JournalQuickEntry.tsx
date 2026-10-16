// Journal Quick Entry. The primary write surface for the
// analyst journal: view, probability, review date, and subject — all
// required and all above the fold — because "creating an entry emits a
// forecast, in the same transaction path" (a central rule) and the point
// of the phase is that writing IS what feeds calibration; everything else
// (thesis, evidence, invalidation, sizing/confidence notes) lives behind
// one disclosure control. (Subject used to be the one
// required field hidden inside that disclosure — moved up.)
//
// ── Local API layer ─────────────────────────────────────────────────────
// `backend/app/routers/journal.py` is mounted and merged; this file is
// its only frontend caller. Deliberately NOT added to `api/client.ts` /
// `api/types.ts`: those two files are shared with other widgets
// (Cockpit's job-polling reuse of the established `JobStatus`
// contract), so this widget
// carries its own small fetch layer instead, reusing `ApiError` and
// `notifyUnauthorized` (already exported by client.ts, so importing them is
// a read, not a touch — the file's own diff stays empty) so a 401 here still
// flips the app to its locked state exactly like every other widget's call.
// `JournalTimeline.tsx` imports this module for its own reads.
import { useId, useRef, useState } from "react";

import { ApiError, notifyUnauthorized, readOnlyDemoError } from "../api/client";
import { DEMO_WRITE_TITLE, DemoWriteNote, useDemoMode } from "../demo";
import { theme } from "../theme";
import { controlStyle } from "./chartTheme";
import { PanelHead } from "./PanelHead";
import { registerWidget } from "./registry";
import { InlineError } from "./InlineError";

const JOURNAL_API = "/api/v1/journal";

export interface JournalEvidenceNote {
  kind: string;
  ref: string;
  note?: string;
}

export interface JournalEntry {
  id: number;
  entry_date: string;
  title: string;
  view: string;
  probability: number;
  review_date: string;
  horizon_days: number;
  subject: string;
  thesis: string | null;
  evidence: JournalEvidenceNote[] | null;
  invalidation: string | null;
  sizing_note: string | null;
  confidence_note: string | null;
  status: "open" | "reviewed" | "superseded";
  source: "human" | "pack_draft";
  draft_of_week: string | null;
  forecast_id: number | null;
  reviewed_at: string | null;
  review_note: string | null;
  created_at: string;
  updated_at: string;
}

// Lean projection — covers both the `create()` response (id/statement/
// probability/due_date only) and `get()`'s joined read (a superset: status/
// subject/issued_by/decision_date/resolution_source also present but never
// required by this UI).
export interface JournalForecastSummary {
  id: number;
  statement: string;
  probability: number;
  due_date: string;
  status?: string;
}

async function journalFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${JOURNAL_API}${path}`, init);
  if (!res.ok) {
    const ro = await readOnlyDemoError(res, init?.method ?? "GET");
    if (ro) throw ro;
    let code = "ERROR";
    let message = res.statusText;
    try {
      const body = await res.json();
      if (body?.error) {
        code = body.error.code ?? code;
        message = body.error.message ?? message;
        if (Array.isArray(body.error.details) && body.error.details.length) {
          message = body.error.details
            .map((d: { message?: string; msg?: string }) => d.message ?? d.msg)
            .filter(Boolean)
            .join(" · ");
        }
      }
    } catch {
      /* non-JSON error body */
    }
    notifyUnauthorized(res.status);
    throw new ApiError(code, message, res.status);
  }
  return res.json() as Promise<T>;
}

function qs(params?: Record<string, string | number | undefined>): string {
  if (!params) return "";
  const entries = Object.entries(params).filter(([, v]) => v !== undefined && v !== "");
  return entries.length ? `?${new URLSearchParams(entries.map(([k, v]) => [k, String(v)])).toString()}` : "";
}

export interface JournalEntryCreatePayload {
  title: string;
  view: string;
  probability: number;
  review_date: string;
  subject: string;
  entry_date?: string;
  thesis?: string | null;
  evidence?: JournalEvidenceNote[] | null;
  invalidation?: string | null;
  sizing_note?: string | null;
  confidence_note?: string | null;
}

export const journalApi = {
  create: (payload: JournalEntryCreatePayload) =>
    journalFetch<{ entry: JournalEntry; forecast: JournalForecastSummary }>("/entries", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }),
  list: (params?: { status?: string; subject?: string; due_before?: string; limit?: number }) =>
    journalFetch<{ entries: JournalEntry[]; count: number }>(`/entries${qs(params)}`),
  due: (before?: string) => journalFetch<{ entries: JournalEntry[]; count: number }>(`/due${qs({ before })}`),
  review: (id: number, note: string, status?: "reviewed" | "superseded") =>
    journalFetch<{ entry: JournalEntry }>(`/entries/${encodeURIComponent(id)}/review`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ note, status }),
    }),
};

// ── shared field styling ────────────────────────────────────────────────
const fieldStyle: React.CSSProperties = {
  display: "flex",
  flexDirection: "column",
  gap: 3,
  fontSize: theme.fontSize.micro,
  color: theme.muted,
};
const hintStyle = (invalid: boolean): React.CSSProperties => ({
  fontSize: theme.fontSize.micro,
  color: invalid ? theme.down : theme.muted,
});
const invalidBorder = (invalid: boolean): React.CSSProperties => ({
  border: `1px solid ${invalid ? theme.down : theme.border}`,
});

// Prefill shape used both by the standalone Quick Entry widget and by
// JournalTimeline's inline "Review and sign" disclosure on a pack_draft row
// — the SAME form component renders in both places (there is no separate
// cross-widget "open the quick entry" event; this component IS the quick
// entry, mounted wherever it's needed).
export interface JournalEntryPrefill {
  subject?: string;
  view?: string;
  probability?: number;
  review_date?: string;
  thesis?: string | null;
  evidence?: JournalEvidenceNote[] | null;
  invalidation?: string | null;
  sizing_note?: string | null;
  confidence_note?: string | null;
}

const ISO_DATE_RE = /^\d{4}-\d{2}-\d{2}$/;
// URLSearchParams decodes once. Bound the decoded JSON before parsing, then
// accept only a short, explicit shape; a URL is never authority to write.
const MAX_DRAFT_LENGTH = 4_000;

function draftFromUrl(): { prefill?: JournalEntryPrefill; error?: string } {
  const draft = new URLSearchParams(window.location.search).get("draft");
  if (draft === null) return {};
  if (draft.length > MAX_DRAFT_LENGTH) return { error: "Journal draft link is too long. Start a new entry instead." };
  try {
    const parsed: unknown = JSON.parse(draft);
    if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) throw new Error("Invalid draft");
    const view = (parsed as Record<string, unknown>).view;
    if (typeof view !== "string" || !view.trim() || view.length > MAX_DRAFT_LENGTH) throw new Error("Invalid view");
    return { prefill: { view } };
  } catch {
    return { error: "Journal draft link is invalid. Start a new entry instead." };
  }
}

export function JournalEntryForm({
  prefill,
  autoExpand,
  onEmitted,
  onCancel,
}: {
  prefill?: JournalEntryPrefill;
  autoExpand?: boolean;
  onEmitted?: (result: { entry: JournalEntry; forecast: JournalForecastSummary }) => void;
  onCancel?: () => void;
}) {
  const demo = useDemoMode();
  const [view, setView] = useState(prefill?.view ?? "");
  const [probability, setProbability] = useState(prefill?.probability !== undefined ? String(prefill.probability) : "");
  const [reviewDate, setReviewDate] = useState(prefill?.review_date ?? "");
  const [subject, setSubject] = useState(prefill?.subject ?? "");
  const [thesis, setThesis] = useState(prefill?.thesis ?? "");
  const [invalidation, setInvalidation] = useState(prefill?.invalidation ?? "");
  const [sizingNote, setSizingNote] = useState(prefill?.sizing_note ?? "");
  const [confidenceNote, setConfidenceNote] = useState(prefill?.confidence_note ?? "");
  const [evidenceRows, setEvidenceRows] = useState<JournalEvidenceNote[]>(prefill?.evidence ?? []);
  const [disclosureOpen, setDisclosureOpen] = useState(!!autoExpand);

  const [touched, setTouched] = useState({ view: false, probability: false, reviewDate: false, subject: false });
  const [submitting, setSubmitting] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  const [emitted, setEmitted] = useState<{ entry: JournalEntry; forecast: JournalForecastSummary } | null>(null);

  const viewRef = useRef<HTMLTextAreaElement>(null);
  const probabilityRef = useRef<HTMLInputElement>(null);
  const reviewDateRef = useRef<HTMLInputElement>(null);
  const subjectRef = useRef<HTMLInputElement>(null);
  // This form mounts once per due draft row in
  // JournalTimeline PLUS the standalone widget — static hint ids collided
  // across every simultaneously-mounted instance.
  const uid = useId();

  const viewInvalid = view.trim().length === 0;
  const probNum = Number(probability);
  const probabilityInvalid = probability.trim().length === 0 || !Number.isFinite(probNum) || !(probNum > 0 && probNum < 1);
  const reviewDateInvalid = !ISO_DATE_RE.test(reviewDate) || Date.parse(reviewDate) <= Date.now() - 86_400_000; // must resolve after today
  const subjectInvalid = subject.trim().length === 0;
  const anyInvalid = viewInvalid || probabilityInvalid || reviewDateInvalid || subjectInvalid;

  const submit = async () => {
    if (demo) return; // before validation: never make the visitor fix a form for nothing
    if (anyInvalid) {
      setTouched({ view: true, probability: true, reviewDate: true, subject: true });
      if (viewInvalid) {
        viewRef.current?.focus();
      } else if (probabilityInvalid) {
        probabilityRef.current?.focus();
      } else if (reviewDateInvalid) {
        reviewDateRef.current?.focus();
      } else if (subjectInvalid) {
        subjectRef.current?.focus();
      }
      return;
    }
    // WRITE ENTRY used to stay live after a successful
    // emit with the form still full of the just-submitted values — one more
    // click re-submitted the same view/probability/subject and emitted a
    // second, duplicate forecast. Defensive guard in addition to the
    // disabled button below (belt + suspenders against a stray Enter etc).
    if (emitted) return;
    setSubmitting(true);
    setErr(null);
    try {
      const result = await journalApi.create({
        // Title isn't a separate above-the-fold field (the spec allows
        // exactly three) — derived from the view's claim itself, truncated
        // to the schema's 200-char cap. See widget-level deviation note.
        title: view.trim().slice(0, 200),
        view: view.trim(),
        probability: probNum,
        review_date: reviewDate,
        subject: subject.trim(),
        thesis: thesis.trim() || null,
        evidence: evidenceRows.length ? evidenceRows : null,
        invalidation: invalidation.trim() || null,
        sizing_note: sizingNote.trim() || null,
        confidence_note: confidenceNote.trim() || null,
      });
      setEmitted(result);
      onEmitted?.(result);
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "could not create entry — please try again");
    } finally {
      setSubmitting(false);
    }
  };

  // Explicit reset for a genuinely new entry, distinct
  // from CANCEL (which discards without ever having submitted).
  const resetForm = () => {
    setView("");
    setProbability("");
    setReviewDate("");
    setSubject("");
    setThesis("");
    setInvalidation("");
    setSizingNote("");
    setConfidenceNote("");
    setEvidenceRows([]);
    setDisclosureOpen(!!autoExpand);
    setTouched({ view: false, probability: false, reviewDate: false, subject: false });
    setErr(null);
    setEmitted(null);
    // Focus fell to `<body>` after WRITE ANOTHER — the
    // button that triggered the reset unmounts itself (the `emitted` banner
    // and this button both gate on `emitted`), and nothing claimed focus
    // next. Send it to the first field of the now-blank form, same as a
    // fresh mount would expect.
    viewRef.current?.focus();
  };

  const addEvidenceRow = () => setEvidenceRows((rows) => [...rows, { kind: "", ref: "", note: "" }]);
  const updateEvidenceRow = (i: number, patch: Partial<JournalEvidenceNote>) =>
    setEvidenceRows((rows) => rows.map((r, idx) => (idx === i ? { ...r, ...patch } : r)));
  const removeEvidenceRow = (i: number) => setEvidenceRows((rows) => rows.filter((_, idx) => idx !== i));

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
      <label style={fieldStyle}>
        <span>View</span>
        <textarea
          ref={viewRef}
          value={view}
          onChange={(e) => setView(e.target.value)}
          onBlur={() => setTouched((t) => ({ ...t, view: true }))}
          rows={3}
          placeholder="What do you believe, and why — the claim in prose"
          aria-required="true"
          aria-invalid={touched.view && viewInvalid}
          aria-describedby={`je-view-hint-${uid}`}
          style={{ ...controlStyle, ...invalidBorder(touched.view && viewInvalid), resize: "vertical", fontFamily: "inherit" }}
        />
        <span id={`je-view-hint-${uid}`} style={hintStyle(touched.view && viewInvalid)}>
          {touched.view && viewInvalid ? "View is required" : "The claim — this becomes the emitted forecast's statement"}
        </span>
      </label>

      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
        <label style={fieldStyle}>
          <span>Probability (0–1)</span>
          <input
            ref={probabilityRef}
            type="number"
            min={0}
            max={1}
            step="0.01"
            value={probability}
            onChange={(e) => setProbability(e.target.value)}
            onBlur={() => setTouched((t) => ({ ...t, probability: true }))}
            aria-required="true"
            aria-invalid={touched.probability && probabilityInvalid}
            aria-describedby={`je-prob-hint-${uid}`}
            style={{ ...controlStyle, ...invalidBorder(touched.probability && probabilityInvalid) }}
          />
          <span id={`je-prob-hint-${uid}`} style={hintStyle(touched.probability && probabilityInvalid)}>
            {touched.probability && probabilityInvalid
              ? "Enter a probability strictly between 0 and 1"
              : Number.isFinite(probNum) && probNum > 0 && probNum < 1
                ? `Plain language: about ${Math.round(probNum * 100)}% likely`
                : "e.g. 0.65 = 65% likely — 0 and 1 are certainties, not forecasts"}
          </span>
        </label>
        <label style={fieldStyle}>
          <span>Review date</span>
          {/* a native `type="date"` input's VALUE is ISO
              but its rendered widget follows the browser/OS locale (en-US
              Chrome shows mm/dd/yyyy) — a US-formatted control sitting under
              a label that says "ISO". Plain validated text field instead,
              same convention as RecommendationRow's ExecuteForm "Executed
              at" field: the DISPLAY stays ISO, not just the value. */}
          <input
            ref={reviewDateRef}
            type="text"
            placeholder="YYYY-MM-DD"
            value={reviewDate}
            onChange={(e) => setReviewDate(e.target.value)}
            onBlur={() => setTouched((t) => ({ ...t, reviewDate: true }))}
            aria-required="true"
            aria-invalid={touched.reviewDate && reviewDateInvalid}
            aria-describedby={`je-date-hint-${uid}`}
            style={{ ...controlStyle, ...invalidBorder(touched.reviewDate && reviewDateInvalid) }}
          />
          <span id={`je-date-hint-${uid}`} style={hintStyle(touched.reviewDate && reviewDateInvalid)}>
            {touched.reviewDate && reviewDateInvalid ? "Enter an ISO date (YYYY-MM-DD) after today" : "ISO date, YYYY-MM-DD — when this view should resolve"}
          </span>
        </label>
      </div>

      {/* Subject is required (it's what the forecast is
          about) but used to be hidden inside the disclosure below — a
          required field a reader could easily never open. Moved above the
          fold with the other three required fields. */}
      <label style={fieldStyle}>
        <span>Subject (symbol or label)</span>
        <input
          ref={subjectRef}
          value={subject}
          onChange={(e) => setSubject(e.target.value)}
          onBlur={() => setTouched((t) => ({ ...t, subject: true }))}
          maxLength={64}
          aria-required="true"
          aria-invalid={touched.subject && subjectInvalid}
          aria-describedby={`je-subject-hint-${uid}`}
          style={{ ...controlStyle, ...invalidBorder(touched.subject && subjectInvalid), maxWidth: 240 }}
        />
        <span id={`je-subject-hint-${uid}`} style={hintStyle(touched.subject && subjectInvalid)}>
          {touched.subject && subjectInvalid
            ? "Subject is required — what this forecast is about"
            : "The symbol, series, or free-text label the forecast tracks"}
        </span>
      </label>

      <details open={disclosureOpen} onToggle={(e) => setDisclosureOpen((e.target as HTMLDetailsElement).open)}>
        <summary style={{ fontSize: theme.fontSize.micro, color: theme.accent, cursor: "pointer" }}>
          Add thesis, evidence, invalidation
        </summary>
        <div style={{ display: "flex", flexDirection: "column", gap: 8, marginTop: 8 }}>
          <label style={fieldStyle}>
            <span>Thesis</span>
            <textarea
              value={thesis}
              onChange={(e) => setThesis(e.target.value)}
              rows={2}
              style={{ ...controlStyle, resize: "vertical", fontFamily: "inherit" }}
            />
          </label>
          <div style={fieldStyle}>
            <span>Evidence</span>
            {evidenceRows.map((row, i) => (
              <div key={i} style={{ display: "flex", gap: 6, alignItems: "center" }}>
                <input
                  aria-label={`Evidence ${i + 1} kind`}
                  placeholder="kind"
                  value={row.kind}
                  onChange={(e) => updateEvidenceRow(i, { kind: e.target.value })}
                  style={{ ...controlStyle, width: 90 }}
                />
                <input
                  aria-label={`Evidence ${i + 1} reference`}
                  placeholder="ref"
                  value={row.ref}
                  onChange={(e) => updateEvidenceRow(i, { ref: e.target.value })}
                  style={{ ...controlStyle, width: 110 }}
                />
                <input
                  aria-label={`Evidence ${i + 1} note`}
                  placeholder="note"
                  value={row.note ?? ""}
                  onChange={(e) => updateEvidenceRow(i, { note: e.target.value })}
                  style={{ ...controlStyle, flex: 1 }}
                />
                <button
                  type="button"
                  onClick={() => removeEvidenceRow(i)}
                  aria-label={`Remove evidence ${i + 1}`}
                  style={{ background: "transparent", color: theme.muted, border: `1px solid ${theme.border}`, borderRadius: 4, minHeight: 24, minWidth: 24, cursor: "pointer" }}
                >
                  ×
                </button>
              </div>
            ))}
            <button
              type="button"
              onClick={addEvidenceRow}
              style={{ alignSelf: "flex-start", background: "transparent", color: theme.accent, border: `1px solid ${theme.accent}`, borderRadius: 4, padding: "2px 8px", fontSize: theme.fontSize.micro, cursor: "pointer" }}
            >
              + add evidence
            </button>
          </div>
          <label style={fieldStyle}>
            <span>Invalidation</span>
            <textarea
              value={invalidation}
              onChange={(e) => setInvalidation(e.target.value)}
              rows={2}
              style={{ ...controlStyle, resize: "vertical", fontFamily: "inherit" }}
            />
          </label>
          <label style={fieldStyle}>
            <span>Sizing note</span>
            <input value={sizingNote} onChange={(e) => setSizingNote(e.target.value)} style={controlStyle} />
          </label>
          <label style={fieldStyle}>
            <span>Confidence note</span>
            <input value={confidenceNote} onChange={(e) => setConfidenceNote(e.target.value)} style={controlStyle} />
          </label>
        </div>
      </details>

      <DemoWriteNote />
      <div style={{ display: "flex", gap: 8 }}>
        <button
          type="button"
          disabled={demo || submitting || !!emitted}
          title={demo ? DEMO_WRITE_TITLE : undefined}
          onClick={() => void submit()}
          style={{
            padding: "6px 14px",
            background: theme.panelHeader,
            color: theme.accent,
            border: `1px solid ${theme.accent}`,
            borderRadius: 4,
            cursor: submitting || emitted ? "default" : "pointer",
            fontSize: theme.fontSize.micro,
            opacity: emitted ? 0.5 : 1,
          }}
        >
          {submitting ? "WRITING…" : "WRITE ENTRY"}
        </button>
        {/* WRITE ENTRY above stays disabled once a
            forecast has been emitted — this is the only way back to a
            writable form, and it resets every field rather than leaving the
            just-submitted values sitting there for a second click. */}
        {emitted && (
          <button
            type="button"
            onClick={resetForm}
            style={{ padding: "6px 14px", background: "transparent", color: theme.accent, border: `1px solid ${theme.accent}`, borderRadius: 4, cursor: "pointer", fontSize: theme.fontSize.micro }}
          >
            WRITE ANOTHER
          </button>
        )}
        {onCancel && (
          <button
            type="button"
            onClick={onCancel}
            style={{ padding: "6px 14px", background: "transparent", color: theme.muted, border: `1px solid ${theme.border}`, borderRadius: 4, cursor: "pointer", fontSize: theme.fontSize.micro }}
          >
            CANCEL
          </button>
        )}
      </div>

      {err && <InlineError msg={err} />}

      {/* On submit it shows the emitted forecast inline — the point of the
          phase is that writing is what feeds calibration, so the user must
          SEE that happen, not just get a generic "saved" toast. Rendered at
          the reading-layer size (Phase 15 fold-in, Q5) since a forecast
          statement is prose to be read, not a dense grid value. */}
      {emitted && (
        <div
          role="status"
          style={{
            fontSize: theme.reading.fontSize,
            lineHeight: theme.reading.lineHeight,
            maxWidth: theme.reading.measure,
            color: theme.up,
            background: "rgba(63,185,80,0.08)",
            border: `1px solid ${theme.up}`,
            borderRadius: 3,
            padding: "8px 10px",
            fontFamily: theme.fontSerif,
          }}
        >
          Forecast emitted — <strong>{emitted.forecast.statement}</strong>
          <br />
          <span className="num" style={{ fontFamily: theme.fontMono, fontSize: theme.fontSize.micro }}>
            {Math.round(emitted.forecast.probability * 100)}% · due {emitted.forecast.due_date}
          </span>
        </div>
      )}
    </div>
  );
}

function JournalQuickEntry() {
  const [draft] = useState(draftFromUrl);
  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead deck="Write a view — probability, review date, and the forecast it emits" />
      <div style={{ padding: "8px 10px", flex: 1, minHeight: 0, overflowY: "auto" }}>
        {draft.error && <div role="alert" style={{ color: theme.down, fontSize: theme.fontSize.micro, marginBottom: 8 }}>{draft.error}</div>}
        {draft.prefill && <div style={{ color: theme.muted, fontSize: theme.fontSize.micro, marginBottom: 8 }}>Analyst draft — review and edit before writing. Probability, review date, and subject are yours to set.</div>}
        <JournalEntryForm prefill={draft.prefill} />
      </div>
    </div>
  );
}

registerWidget({
  id: "journal-quick-entry",
  title: "Journal Quick Entry",
  component: JournalQuickEntry,
  defaultSize: { w: 4, h: 12 },
  minSize: { w: 3, h: 8 },
  defaultConfig: {},
  category: "Journal",
  help:
    "Write a view — the claim, a probability, a review date, and the subject it's about. Submitting emits a real forecast into the ledger in the same transaction, which the confirmation below shows immediately; the form then locks until you press WRITE ANOTHER, so a second click can't emit a duplicate. Add thesis, evidence and an invalidation condition behind the disclosure.",
  description: "The claim that emits a forecast — view, probability, review date, subject",
});
