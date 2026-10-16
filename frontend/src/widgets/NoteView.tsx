// Note View. The reading surface: one sentence rendered
// large, the rest of the ladder as progressive disclosure, evidence as a
// table of cited ids. Export: POST /export first
// to validate the pipeline (exporter labels available, privacy scan
// clean) and get the rendered filename, then trigger the actual download
// against the GET .../export.pdf route — same pipeline, browser-handled
// transfer (fix-first #1).
import { useState } from "react";

import { ApiError } from "../api/client";
import { theme } from "../theme";
import { controlStyle } from "./chartTheme";
import type { Note } from "./NoteEditor";
import { notesApi, notesExportPdfUrl } from "./NoteEditor";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";
import { InlineError } from "./InlineError";

const LADDER_SECTIONS: { key: keyof Pick<Note, "why_it_matters" | "action" | "risk_invalidation" | "body">; label: string }[] = [
  { key: "why_it_matters", label: "Why it matters" },
  { key: "action", label: "Action" },
  { key: "risk_invalidation", label: "Risk / invalidation" },
  { key: "body", label: "Body" },
];

function NoteView() {
  const [selectedSlug, setSelectedSlug] = useState<string>("");
  // fix-first #12: the note picker never refetched after an edit/sign
  // elsewhere (NoteEditor), so a just-signed note kept showing "draft" here
  // until the whole widget remounted. Poll every 30s — same convention
  // JournalTimeline already uses for its own list/due fetches.
  const notesListState = useAsync(() => notesApi.list({ limit: 100 }), [], 30_000);
  // fix-first #6: Retry used to be `setSelectedSlug(s => s)` — same
  // value in, same value out, so React never re-ran the effect and Retry
  // did nothing. `noteRefreshKey` gives it something to actually bump.
  const [noteRefreshKey, setNoteRefreshKey] = useState(0);
  const noteState = useAsync(
    () => (selectedSlug ? notesApi.get(selectedSlug) : Promise.resolve(null)),
    [selectedSlug, noteRefreshKey],
  );
  const [exporting, setExporting] = useState(false);
  const [exportErr, setExportErr] = useState<string | null>(null);

  const doExport = async () => {
    if (!selectedSlug) return;
    setExporting(true);
    setExportErr(null);
    try {
      const result = await notesApi.export(selectedSlug);
      const a = document.createElement("a");
      a.href = notesExportPdfUrl(selectedSlug);
      a.download = result.filename;
      document.body.appendChild(a);
      a.click();
      a.remove();
    } catch (e) {
      setExportErr(e instanceof ApiError ? e.message : "could not export note");
    } finally {
      setExporting(false);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead deck="The reading surface — one sentence, then the ladder, then the record">
        <select
          aria-label="Select note to view"
          value={selectedSlug}
          onChange={(e) => setSelectedSlug(e.target.value)}
          style={controlStyle}
        >
          <option value="">Select a note…</option>
          {(notesListState.data?.notes ?? []).map((n) => (
            <option key={n.slug} value={n.slug}>
              {n.title}
            </option>
          ))}
        </select>
        <button
          type="button"
          disabled={!selectedSlug || exporting}
          onClick={() => void doExport()}
          title="Export a redacted PDF of this note"
          style={{
            padding: "2px 8px",
            background: "transparent",
            color: !selectedSlug || exporting ? theme.muted : theme.accent,
            border: `1px solid ${!selectedSlug || exporting ? theme.border : theme.accent}`,
            borderRadius: 4,
            fontSize: theme.fontSize.micro,
            cursor: !selectedSlug || exporting ? "not-allowed" : "pointer",
          }}
        >
          {exporting ? "EXPORTING…" : "EXPORT"}
        </button>
      </PanelHead>
      <div style={{ padding: "10px 12px", flex: 1, minHeight: 0, overflowY: "auto" }}>
        {exportErr && <InlineError msg={exportErr} style={{ marginBottom: 8 }} />}
        {!selectedSlug ? (
          <PanelState kind="empty" message="No note selected" hint="Pick one from the dropdown above." />
        ) : noteState.loading ? (
          <PanelState kind="loading" message="loading…" />
        ) : noteState.error ? (
          <PanelState kind="error" message={noteState.error} onRetry={() => setNoteRefreshKey((k) => k + 1)} />
        ) : noteState.data ? (
          <div style={{ display: "flex", flexDirection: "column", gap: 12 }}>
            <div style={{ display: "flex", gap: 8, alignItems: "center", flexWrap: "wrap" }}>
              <span style={{ fontSize: theme.fontSize.micro, color: theme.muted, textTransform: "uppercase", letterSpacing: "0.05em" }}>
                {noteState.data.note.kind === "thesis" ? "Thesis" : "Research note"}
              </span>
              <span
                style={{
                  fontSize: theme.fontSize.micro,
                  color: noteState.data.note.status === "signed" ? theme.up : theme.accent,
                  border: `1px solid ${noteState.data.note.status === "signed" ? theme.up : theme.accent}`,
                  borderRadius: 3,
                  padding: "0px 5px",
                  textTransform: "uppercase",
                  letterSpacing: "0.04em",
                }}
              >
                {noteState.data.note.status}
              </span>
            </div>
            <h2 style={{ margin: 0, fontFamily: theme.fontSerif, fontSize: theme.fontSize.lg, color: theme.text, fontWeight: 700 }}>
              {noteState.data.note.title}
            </h2>
            {/* The one sentence, rendered large — this is the reading
                surface's whole point: the claim you can read at a glance,
                not a dense grid row. Reading-layer tokens (Phase 15 fold-in,
                Q5): measured line length + open leading. */}
            <p
              style={{
                margin: 0,
                fontFamily: theme.fontSerif,
                fontSize: theme.fontSize.xl,
                lineHeight: theme.reading.lineHeight,
                maxWidth: theme.reading.measure,
                color: theme.text,
              }}
            >
              {noteState.data.note.one_sentence}
            </p>

            {LADDER_SECTIONS.map(({ key, label }) => {
              const value = noteState.data!.note[key];
              if (!value) return null;
              return (
                <details key={key}>
                  <summary style={{ fontSize: theme.fontSize.sm, color: theme.accent, cursor: "pointer", fontWeight: 600 }}>{label}</summary>
                  <p
                    style={{
                      marginTop: 6,
                      fontFamily: theme.fontSerif,
                      fontSize: theme.reading.fontSize,
                      lineHeight: theme.reading.lineHeight,
                      maxWidth: theme.reading.measure,
                      color: theme.text,
                      whiteSpace: "pre-line",
                    }}
                  >
                    {value}
                  </p>
                </details>
              );
            })}

            {noteState.data.note.subjects.length > 0 && (
              <div style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>
                subjects: {noteState.data.note.subjects.join(", ")}
              </div>
            )}

            <div>
              <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.05em", marginBottom: 4 }}>EVIDENCE</div>
              {noteState.data.evidence.length === 0 ? (
                <span style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>No cited evidence.</span>
              ) : (
                <table style={{ width: "100%", borderCollapse: "collapse", fontSize: theme.fontSize.micro }}>
                  <thead>
                    <tr style={{ borderBottom: `1px solid ${theme.border}`, textAlign: "left", color: theme.muted }}>
                      <th style={{ padding: "3px 6px" }}>kind</th>
                      <th style={{ padding: "3px 6px" }}>id</th>
                      <th style={{ padding: "3px 6px" }}>label</th>
                    </tr>
                  </thead>
                  <tbody>
                    {noteState.data.evidence.map((e, i) => (
                      <tr key={i} style={{ borderBottom: `1px solid ${theme.grid}` }}>
                        <td style={{ padding: "3px 6px", color: theme.muted }}>{e.kind}</td>
                        <td className="num" style={{ padding: "3px 6px" }}>
                          {e.id}
                        </td>
                        <td style={{ padding: "3px 6px", color: e.resolved ? theme.text : theme.down }}>
                          {e.resolved ? e.label : "unresolved"}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              )}
            </div>
          </div>
        ) : null}
      </div>
    </div>
  );
}

registerWidget({
  id: "note-view",
  title: "Note View",
  component: NoteView,
  defaultSize: { w: 5, h: 20 },
  minSize: { w: 4, h: 10 },
  defaultConfig: {},
  category: "Journal",
  help:
    "The reading surface for a signed or drafted note: one sentence up front, the rest of the ladder folded behind disclosures, and every cited evidence ref hydrated into a real table row. Export renders a redacted PDF by default and downloads it.",
  description: "One sentence, then the ladder, then the cited record",
});
