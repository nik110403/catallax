// Note Editor. Creates/edits a research note or thesis: the
// ladder (roadmap §5.3, one-sentence → evidence) as five labelled fields in
// order, evidence refs by kind+id with the resolved label echoed back on
// blur, sign with a confirm since signing is one-way.
//
// ── Local API layer ─────────────────────────────────────────────────────
// Same reasoning as JournalQuickEntry.tsx: `api/client.ts`/`api/types.ts`
// are off-limits here, so notes gets its own small fetch
// layer, reusing the exported `ApiError`/`notifyUnauthorized` from
// client.ts (a read, not a touch). `backend/app/routers/notes.py` has
// since merged; this file's own small fetch layer stays as-is rather than
// folding into `api/client.ts` after the fact (still off-limits).
//
// ── Evidence-ref live validation, and why it's shaped this way ─────────
// The pinned contract has no dedicated "check one ref" endpoint — only
// POST/PATCH validate the whole `evidence_refs` array atomically, and only
// GET /{slug} returns hydrated labels. So "the resolved label echoed back
// on blur" is implemented as: once the note has a slug (i.e. it has been
// saved at least once), blurring a completed evidence row's id field PATCHes
// the FULL current evidence_refs array (previously-committed rows plus this
// new/edited one). Success -> a follow-up GET hydrates the label for the
// row just added. Failure -> nothing was saved (the PATCH is atomic), the
// 422's message renders inline under that one row, not as a banner. Before
// the note has ever been saved, there is nothing to PATCH against — rows
// added at that point show "checked when the note is saved" until the
// first Save, whose own error (if the ref doesn't resolve) is attached to
// the same row.
import { useEffect, useRef, useState, useId } from "react";

import { ApiError, notifyUnauthorized } from "../api/client";
import { theme } from "../theme";
import { controlStyle } from "./chartTheme";
import { PanelHead } from "./PanelHead";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";

const NOTES_API = "/api/v1/notes";

export type EvidenceRefKind = "forecast" | "recommendation" | "journal_entry" | "report_pack" | "shadow_verdict" | "url";
export const EVIDENCE_KINDS: EvidenceRefKind[] = ["forecast", "recommendation", "journal_entry", "report_pack", "shadow_verdict", "url"];

export interface EvidenceRef {
  kind: EvidenceRefKind;
  id: number | string;
  label?: string;
}

export interface HydratedEvidenceRef {
  kind: string;
  id: number | string;
  label: string;
  resolved: boolean;
}

export interface Note {
  id: number;
  slug: string;
  kind: "research_note" | "thesis";
  title: string;
  status: "draft" | "signed";
  one_sentence: string;
  why_it_matters: string | null;
  action: string | null;
  risk_invalidation: string | null;
  body: string | null;
  evidence_refs: EvidenceRef[];
  subjects: string[];
  signed_at: string | null;
  created_at: string;
  updated_at: string;
}

// a handful of notes.py's own routes (`export_note` /
// `download_note_pdf`'s `_export`) raise a raw `HTTPException(detail=...)`
// instead of a `CatallaxError` — no `{error:{...}}` envelope, just
// `{detail:"..."}` or `{detail:{code:"..."}}` with no `message` key at all.
// Map the codes we know are actionable; anything else falls back to
// `res.statusText` same as before.
const DETAIL_CODE_MESSAGES: Record<string, string> = {
  PRIVACY_LABELS_UNAVAILABLE:
    "Export needs the privacy label file on the server — the portfolio account labels used for redaction couldn't be loaded.",
};

async function notesFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${NOTES_API}${path}`, init);
  if (!res.ok) {
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
      } else if (typeof body?.detail === "string") {
        message = body.detail;
      } else if (body?.detail && typeof body.detail === "object") {
        code = body.detail.code ?? code;
        message = DETAIL_CODE_MESSAGES[code] ?? body.detail.message ?? message;
      }
    } catch {
      /* non-JSON error body */
    }
    notifyUnauthorized(res.status);
    throw new ApiError(code, message, res.status);
  }
  return res.json() as Promise<T>;
}

// `subjects`/`evidence_refs` are nullable in the DB
// (3/4 live notes had `subjects: null`) but every caller — NoteView's
// `.length`, NoteEditor's `.join`/`setEvidenceRows` — assumed an array,
// throwing and blanking the whole widget (or, pre-boundary, the app).
// Normalise at the one place every note enters the frontend rather than at
// each read site.
function normalizeNote(n: Note): Note {
  return { ...n, subjects: n.subjects ?? [], evidence_refs: n.evidence_refs ?? [] };
}

function qs(params?: Record<string, string | number | undefined>): string {
  if (!params) return "";
  const entries = Object.entries(params).filter(([, v]) => v !== undefined && v !== "");
  return entries.length ? `?${new URLSearchParams(entries.map(([k, v]) => [k, String(v)])).toString()}` : "";
}

export interface NoteWritePayload {
  kind?: "research_note" | "thesis";
  title?: string;
  one_sentence?: string;
  why_it_matters?: string | null;
  action?: string | null;
  risk_invalidation?: string | null;
  body?: string | null;
  evidence_refs?: EvidenceRef[];
  subjects?: string[];
}

export interface NoteExportResult {
  path: string;
  filename: string;
  redacted: boolean;
  scan: { clean: boolean; rules: Record<string, number> };
}

export const notesApi = {
  // `notes.py`'s list/create routes are declared as
  // `@router.get("/")`/`@router.post("/")` under `prefix="/notes"` — the
  // canonical path is `/api/v1/notes/`, WITH the trailing slash. Calling it
  // without one 307-redirects, and that redirect's `Location` is built from
  // the ASGI scope behind nginx, dropping the port (and, behind TLS, the
  // scheme). Call the canonical path directly rather than relying on the
  // redirect. (Every other route here — `/{slug}`, `/{slug}/sign`, … — has
  // no root-level ambiguity, so only these two needed the leading `/`.)
  create: (payload: NoteWritePayload) =>
    notesFetch<{ note: Note }>("/", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }).then(
      (r) => ({ note: normalizeNote(r.note) }),
    ),
  list: (params?: { kind?: string; status?: string; limit?: number }) =>
    notesFetch<{ notes: Note[]; count: number }>(`/${qs(params)}`).then((r) => ({ ...r, notes: r.notes.map(normalizeNote) })),
  get: (slug: string) =>
    notesFetch<{ note: Note; evidence: HydratedEvidenceRef[] }>(`/${encodeURIComponent(slug)}`).then((r) => ({ ...r, note: normalizeNote(r.note) })),
  update: (slug: string, payload: Omit<NoteWritePayload, "kind">) =>
    notesFetch<{ note: Note }>(`/${encodeURIComponent(slug)}`, { method: "PATCH", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) }).then(
      (r) => ({ note: normalizeNote(r.note) }),
    ),
  sign: (slug: string) =>
    notesFetch<{ note: Note }>(`/${encodeURIComponent(slug)}/sign`, { method: "POST" }).then((r) => ({ note: normalizeNote(r.note) })),
  // Redacted export (backend/app/routers/notes.py `POST /{slug}/export`,
  // default `redacted=true`). Returns the rendered PDF's metadata + privacy
  // scan summary — NOT the PDF bytes. The caller triggers the actual
  // download against `notesExportPdfUrl` (the `GET .../export.pdf` route,
  // same pipeline, browser-handled transfer — same convention as
  // `reportPdfUrl` in api/client.ts) once this call confirms the export
  // succeeds, so an account-label failure or a privacy-residual 409
  // surfaces as an inline error instead of a downloaded error-JSON file.
  // (the GET .../export.pdf route re-runs the FULL
  // `_export` pipeline itself rather than serving what this POST already
  // rendered — no path/token in this response that the GET route accepts,
  // so the double-render can't be collapsed to one request without a
  // backend change. Left as-is this wave; see handoff report.)
  export: (slug: string) => notesFetch<NoteExportResult>(`/${encodeURIComponent(slug)}/export`, { method: "POST" }),
};

export const notesExportPdfUrl = (slug: string): string => `${NOTES_API}/${encodeURIComponent(slug)}/export.pdf`;

const fieldStyle: React.CSSProperties = { display: "flex", flexDirection: "column", gap: 3, fontSize: theme.fontSize.micro, color: theme.muted };
const hintStyle = (invalid: boolean): React.CSSProperties => ({ fontSize: theme.fontSize.micro, color: invalid ? theme.down : theme.muted });
const invalidBorder = (invalid: boolean): React.CSSProperties => ({ border: `1px solid ${invalid ? theme.down : theme.border}` });

type RowState = { status: "idle" | "checking" | "resolved" | "error"; label?: string; error?: string };

const emptyLadder = () => ({
  kind: "research_note" as Note["kind"],
  title: "",
  subjectsText: "",
  oneSentence: "",
  whyItMatters: "",
  action: "",
  riskInvalidation: "",
  body: "",
});

function NoteEditor() {
  // Bumped after a successful save/sign so the note-picker dropdown (title,
  // signed status) reflects what was just written — the list fetch itself
  // has no other trigger otherwise (fix-first #12).
  const [notesRefreshKey, setNotesRefreshKey] = useState(0);
  const notesListState = useAsync(() => notesApi.list({ limit: 100 }), [notesRefreshKey]);
  const [selectedSlug, setSelectedSlug] = useState<string>(""); // "" = new note

  const [note, setNote] = useState(emptyLadder());
  const [slug, setSlug] = useState<string | null>(null);
  const [status, setStatus] = useState<Note["status"]>("draft");
  const [evidenceRows, setEvidenceRows] = useState<EvidenceRef[]>([]);
  const [rowState, setRowState] = useState<Record<number, RowState>>({});

  const [touched, setTouched] = useState({ title: false, oneSentence: false });
  const [saving, setSaving] = useState(false);
  const [saveErr, setSaveErr] = useState<string | null>(null);
  const [confirmingSign, setConfirmingSign] = useState(false);
  const [signing, setSigning] = useState(false);
  // Dirty tracking (fix-first #5 + #14): true once the ladder or
  // evidence rows change after a load/save, so Sign can save-then-sign
  // instead of signing the server's stale copy, and switching notes mid-edit
  // can ask instead of silently discarding. `skipDirtyRef` swallows the ONE
  // effect run that fires from loadNote's own `setNote`/`setEvidenceRows`
  // calls (a programmatic load is not the user editing).
  const [dirty, setDirty] = useState(false);
  const skipDirtyRef = useRef(true);
  useEffect(() => {
    if (skipDirtyRef.current) {
      skipDirtyRef.current = false;
      return;
    }
    setDirty(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [note, evidenceRows]);

  const titleRef = useRef<HTMLInputElement>(null);
  const oneSentenceRef = useRef<HTMLTextAreaElement>(null);
  const uid = useId();

  const signed = status === "signed";
  const titleInvalid = note.title.trim().length === 0;
  const oneSentenceInvalid = note.oneSentence.trim().length === 0;

  const loadNote = async (targetSlug: string) => {
    setSelectedSlug(targetSlug);
    skipDirtyRef.current = true;
    setDirty(false);
    if (!targetSlug) {
      setNote(emptyLadder());
      setSlug(null);
      setStatus("draft");
      setEvidenceRows([]);
      setRowState({});
      setSaveErr(null);
      return;
    }
    try {
      const { note: n, evidence } = await notesApi.get(targetSlug);
      skipDirtyRef.current = true;
      setNote({
        kind: n.kind,
        title: n.title,
        subjectsText: n.subjects.join(", "),
        oneSentence: n.one_sentence,
        whyItMatters: n.why_it_matters ?? "",
        action: n.action ?? "",
        riskInvalidation: n.risk_invalidation ?? "",
        body: n.body ?? "",
      });
      setSlug(n.slug);
      setStatus(n.status);
      setEvidenceRows(n.evidence_refs);
      setRowState(
        Object.fromEntries(evidence.map((e, i) => [i, { status: e.resolved ? "resolved" : "error", label: e.label } as RowState])),
      );
    } catch (e) {
      setSaveErr(e instanceof ApiError ? e.message : "could not load note");
    }
  };

  // Note-picker switch mid-edit (fix-first #14): a plain `onChange`
  // silently discarded unsaved ladder/evidence edits. Confirm first when
  // dirty; the select itself is uncontrolled-enough here (`value={selectedSlug}`
  // only changes via `loadNote`) that declining simply leaves it as-is.
  const requestLoadNote = (targetSlug: string) => {
    if (dirty && !window.confirm("Discard unsaved changes and switch notes?")) return;
    void loadNote(targetSlug);
  };

  const buildPayload = (evidence: EvidenceRef[]): NoteWritePayload => ({
    kind: note.kind,
    title: note.title.trim(),
    one_sentence: note.oneSentence.trim(),
    why_it_matters: note.whyItMatters.trim() || null,
    action: note.action.trim() || null,
    risk_invalidation: note.riskInvalidation.trim() || null,
    body: note.body.trim() || null,
    evidence_refs: evidence,
    subjects: note.subjectsText.split(",").map((s) => s.trim()).filter(Boolean),
  });

  const save = async (): Promise<boolean> => {
    if (titleInvalid || oneSentenceInvalid) {
      setTouched({ title: true, oneSentence: true });
      // fix-first #14: focus the first invalid field, same convention
      // as JournalQuickEntry's submit() / RecommendationRow's ExecuteForm.
      if (titleInvalid) titleRef.current?.focus();
      else if (oneSentenceInvalid) oneSentenceRef.current?.focus();
      return false;
    }
    setSaving(true);
    setSaveErr(null);
    try {
      const payload = buildPayload(evidenceRows);
      // `kind` is create-only: the PATCH schema is extra="forbid" and 422s on it.
      const { kind: _kind, ...patch } = payload;
      const result = slug ? await notesApi.update(slug, patch) : await notesApi.create(payload);
      setSlug(result.note.slug);
      setStatus(result.note.status);
      setSelectedSlug(result.note.slug);
      // Refresh hydrated labels now that the note (and any evidence added
      // before the first save) is actually persisted.
      const { evidence } = await notesApi.get(result.note.slug);
      setRowState(
        Object.fromEntries(evidence.map((e, i) => [i, { status: e.resolved ? "resolved" : "error", label: e.label } as RowState])),
      );
      setDirty(false);
      setNotesRefreshKey((k) => k + 1);
      return true;
    } catch (e) {
      setSaveErr(e instanceof ApiError ? e.message : "could not save note");
      return false;
    } finally {
      setSaving(false);
    }
  };

  const addEvidenceRow = () => setEvidenceRows((rows) => [...rows, { kind: "forecast", id: "" }]);
  const updateEvidenceRow = (i: number, patch: Partial<EvidenceRef>) => {
    setEvidenceRows((rows) => rows.map((r, idx) => (idx === i ? { ...r, ...patch } : r)));
    setRowState((s) => ({ ...s, [i]: { status: "idle" } }));
  };
  // fix-first #14: rowState is keyed by array index, so removing row i
  // used to leave every later row's hydrated/error state attached to the
  // WRONG row (row i+1's state now describes row i's data). Reindex down
  // rather than just deleting key i.
  const removeEvidenceRow = (i: number) => {
    setEvidenceRows((rows) => rows.filter((_, idx) => idx !== i));
    setRowState((s) => {
      const next: Record<number, RowState> = {};
      for (const [key, value] of Object.entries(s)) {
        const idx = Number(key);
        if (idx < i) next[idx] = value;
        else if (idx > i) next[idx - 1] = value;
        // idx === i: the removed row's state is dropped.
      }
      return next;
    });
  };

  // "the resolved label echoed back on blur" — fix-first #4: this used
  // to PATCH `buildPayload(evidenceRows)` — the WHOLE note, including
  // title/thesis/etc fields the user may be mid-edit on but never clicked
  // Save for. Blurring an evidence id field silently persisted them. Now
  // scoped to `evidence_refs` only (the backend's PATCH applies only the
  // keys present in the payload — see `NotesService.update`), so a blur can
  // only ever validate/save evidence, never anything else in the ladder.
  // Only a 422 (`InvalidEvidenceRef`) is this row's problem; anything else
  // (409 signed, network, 5xx) is a whole-request failure and belongs in
  // `saveErr`, not attributed to one evidence row.
  const checkRow = async (i: number) => {
    const row = evidenceRows[i];
    if (!row || row.id === "" || row.id === undefined) return;
    if (!slug) {
      setRowState((s) => ({ ...s, [i]: { status: "idle" } }));
      return;
    }
    setRowState((s) => ({ ...s, [i]: { status: "checking" } }));
    try {
      const result = await notesApi.update(slug, { evidence_refs: evidenceRows });
      const idx = result.note.evidence_refs.length - 1;
      const { evidence } = await notesApi.get(slug);
      const hydrated = evidence[idx] ?? evidence[i];
      setRowState((s) => ({ ...s, [i]: { status: "resolved", label: hydrated?.label } }));
    } catch (e) {
      if (e instanceof ApiError && e.status === 422) {
        setRowState((s) => ({ ...s, [i]: { status: "error", error: e.message } }));
      } else {
        setRowState((s) => ({ ...s, [i]: { status: "idle" } }));
        setSaveErr(e instanceof ApiError ? e.message : "could not validate evidence");
      }
    }
  };

  // fix-first #5: Sign used to sign whatever the SERVER already had,
  // ignoring unsaved local edits, then lock the fields showing the
  // now-misleading unsigned-but-displayed text. Save-then-sign: if the form
  // is dirty, save first; a failed save (validation or network) aborts the
  // sign entirely rather than signing the stale server copy.
  const doSign = async () => {
    if (!slug) return;
    setSigning(true);
    try {
      if (dirty) {
        const saved = await save();
        if (!saved) return;
      }
      const result = await notesApi.sign(slug);
      setStatus(result.note.status);
      setConfirmingSign(false);
      setNotesRefreshKey((k) => k + 1);
    } catch (e) {
      setSaveErr(e instanceof ApiError ? e.message : "could not sign note");
    } finally {
      setSigning(false);
    }
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead deck="The ladder, one sentence to evidence — draft, cite, sign">
        <select
          aria-label="Select note to edit"
          value={selectedSlug}
          onChange={(e) => requestLoadNote(e.target.value)}
          style={controlStyle}
        >
          <option value="">New note</option>
          {(notesListState.data?.notes ?? []).map((n) => (
            <option key={n.slug} value={n.slug}>
              {n.title} {n.status === "signed" ? "(signed)" : ""}
            </option>
          ))}
        </select>
      </PanelHead>
      <div style={{ padding: "8px 10px", flex: 1, minHeight: 0, overflowY: "auto", display: "flex", flexDirection: "column", gap: 8 }}>
        {signed && (
          <div role="status" style={{ fontSize: theme.fontSize.micro, color: theme.muted, border: `1px solid ${theme.border}`, borderRadius: 3, padding: "4px 8px" }}>
            Signed — this note is now immutable. Editing needs a new revision.
          </div>
        )}
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
          <label style={fieldStyle}>
            <span>Kind</span>
            <select
              value={note.kind}
              disabled={signed || !!slug || saving || signing}
              onChange={(e) => setNote((n) => ({ ...n, kind: e.target.value as Note["kind"] }))}
              style={controlStyle}
            >
              <option value="research_note">Research note</option>
              <option value="thesis">Thesis</option>
            </select>
          </label>
          <label style={fieldStyle}>
            <span>Subjects (comma-separated, optional)</span>
            <input
              value={note.subjectsText}
              disabled={signed || saving || signing}
              onChange={(e) => setNote((n) => ({ ...n, subjectsText: e.target.value }))}
              style={controlStyle}
            />
          </label>
        </div>
        <label style={fieldStyle}>
          <span>Title</span>
          <input
            value={note.title}
            disabled={signed || saving || signing}
            onChange={(e) => setNote((n) => ({ ...n, title: e.target.value }))}
            onBlur={() => setTouched((t) => ({ ...t, title: true }))}
            maxLength={200}
            aria-required="true"
            aria-invalid={touched.title && titleInvalid}
            aria-describedby={`ne-title-hint-${uid}`}
            style={{ ...controlStyle, ...invalidBorder(touched.title && titleInvalid) }}
          />
          <span id={`ne-title-hint-${uid}`} style={hintStyle(touched.title && titleInvalid)}>
            {touched.title && titleInvalid ? "Title is required" : " "}
          </span>
        </label>

        {/* The ladder — five labelled fields, in order (roadmap §5.3). */}
        <label style={fieldStyle}>
          <span>1. One sentence</span>
          <textarea
            ref={oneSentenceRef}
            value={note.oneSentence}
            disabled={signed || saving || signing}
            onChange={(e) => setNote((n) => ({ ...n, oneSentence: e.target.value }))}
            onBlur={() => setTouched((t) => ({ ...t, oneSentence: true }))}
            rows={2}
            aria-required="true"
            aria-invalid={touched.oneSentence && oneSentenceInvalid}
            aria-describedby={`ne-sentence-hint-${uid}`}
            style={{ ...controlStyle, ...invalidBorder(touched.oneSentence && oneSentenceInvalid), resize: "vertical", fontFamily: "inherit" }}
          />
          <span id={`ne-sentence-hint-${uid}`} style={hintStyle(touched.oneSentence && oneSentenceInvalid)}>
            {touched.oneSentence && oneSentenceInvalid ? "One sentence is required" : "The claim, stated once, plainly"}
          </span>
        </label>
        <label style={fieldStyle}>
          <span>2. Why it matters</span>
          <textarea
            value={note.whyItMatters}
            disabled={signed || saving || signing}
            onChange={(e) => setNote((n) => ({ ...n, whyItMatters: e.target.value }))}
            rows={2}
            style={{ ...controlStyle, resize: "vertical", fontFamily: "inherit" }}
          />
        </label>
        <label style={fieldStyle}>
          <span>3. Action</span>
          <textarea
            value={note.action}
            disabled={signed || saving || signing}
            onChange={(e) => setNote((n) => ({ ...n, action: e.target.value }))}
            rows={2}
            style={{ ...controlStyle, resize: "vertical", fontFamily: "inherit" }}
          />
        </label>
        <label style={fieldStyle}>
          <span>4. Risk / invalidation</span>
          <textarea
            value={note.riskInvalidation}
            disabled={signed || saving || signing}
            onChange={(e) => setNote((n) => ({ ...n, riskInvalidation: e.target.value }))}
            rows={2}
            style={{ ...controlStyle, resize: "vertical", fontFamily: "inherit" }}
          />
        </label>
        <label style={fieldStyle}>
          <span>5. Body (long-form)</span>
          <textarea
            value={note.body}
            disabled={signed || saving || signing}
            onChange={(e) => setNote((n) => ({ ...n, body: e.target.value }))}
            rows={5}
            style={{ ...controlStyle, resize: "vertical", fontFamily: "inherit" }}
          />
        </label>

        <div style={fieldStyle}>
          <span>Evidence</span>
          {!slug && evidenceRows.length > 0 && (
            <span style={{ color: theme.muted }}>Checked once you save the note.</span>
          )}
          {evidenceRows.map((row, i) => {
            const rs = rowState[i] ?? { status: "idle" };
            const errId = `ne-evidence-${i}-error`;
            return (
              <div key={i} style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
                  <select
                    aria-label={`Evidence ${i + 1} kind`}
                    value={row.kind}
                    disabled={signed || saving || signing}
                    onChange={(e) => updateEvidenceRow(i, { kind: e.target.value as EvidenceRefKind })}
                    style={{ ...controlStyle, width: 130 }}
                  >
                    {EVIDENCE_KINDS.map((k) => (
                      <option key={k} value={k}>
                        {k}
                      </option>
                    ))}
                  </select>
                  <input
                    aria-label={`Evidence ${i + 1} id`}
                    placeholder="id or url"
                    value={row.id}
                    disabled={signed || saving || signing}
                    onChange={(e) => updateEvidenceRow(i, { id: e.target.value })}
                    onBlur={() => void checkRow(i)}
                    aria-invalid={rs.status === "error"}
                    aria-describedby={rs.status === "error" ? errId : undefined}
                    style={{ ...controlStyle, flex: 1, ...invalidBorder(rs.status === "error") }}
                  />
                  {!signed && (
                    <button
                      type="button"
                      onClick={() => removeEvidenceRow(i)}
                      aria-label={`Remove evidence ${i + 1}`}
                      style={{ background: "transparent", color: theme.muted, border: `1px solid ${theme.border}`, borderRadius: 4, minHeight: 24, minWidth: 24, cursor: "pointer" }}
                    >
                      ×
                    </button>
                  )}
                </div>
                {rs.status === "checking" && <span style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>checking…</span>}
                {rs.status === "resolved" && (
                  <span style={{ fontSize: theme.fontSize.micro, color: theme.up }}>{rs.label ?? "resolved"}</span>
                )}
                {rs.status === "error" && (
                  <span id={errId} role="alert" style={{ fontSize: theme.fontSize.micro, color: theme.down }}>
                    {rs.error}
                  </span>
                )}
              </div>
            );
          })}
          {!signed && (
            <button
              type="button"
              onClick={addEvidenceRow}
              style={{ alignSelf: "flex-start", background: "transparent", color: theme.accent, border: `1px solid ${theme.accent}`, borderRadius: 4, padding: "2px 8px", fontSize: theme.fontSize.micro, cursor: "pointer" }}
            >
              + add evidence
            </button>
          )}
        </div>

        {!signed && (
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <button
              type="button"
              disabled={saving}
              onClick={() => void save()}
              style={{ padding: "6px 14px", background: theme.panelHeader, color: theme.accent, border: `1px solid ${theme.accent}`, borderRadius: 4, cursor: saving ? "default" : "pointer", fontSize: theme.fontSize.micro }}
            >
              {saving ? "SAVING…" : slug ? "SAVE" : "SAVE DRAFT"}
            </button>
            {slug && !confirmingSign && (
              <button
                type="button"
                onClick={() => setConfirmingSign(true)}
                style={{ padding: "6px 14px", background: "transparent", color: theme.up, border: `1px solid ${theme.up}`, borderRadius: 4, cursor: "pointer", fontSize: theme.fontSize.micro }}
              >
                SIGN
              </button>
            )}
            {confirmingSign && (
              <>
                <span role="alert" style={{ fontSize: theme.fontSize.micro, color: theme.warn }}>
                  Signing is one-way — confirm?
                </span>
                <button
                  type="button"
                  disabled={signing}
                  onClick={() => void doSign()}
                  style={{ padding: "6px 14px", background: theme.up, color: theme.bg, border: `1px solid ${theme.up}`, borderRadius: 4, cursor: "pointer", fontSize: theme.fontSize.micro }}
                >
                  {signing ? "SIGNING…" : "CONFIRM SIGN"}
                </button>
                <button
                  type="button"
                  onClick={() => setConfirmingSign(false)}
                  style={{ padding: "6px 14px", background: "transparent", color: theme.muted, border: `1px solid ${theme.border}`, borderRadius: 4, cursor: "pointer", fontSize: theme.fontSize.micro }}
                >
                  CANCEL
                </button>
              </>
            )}
          </div>
        )}
        {saveErr && (
          <div role="alert" style={{ fontSize: theme.fontSize.micro, color: theme.down, background: "rgba(248,81,73,0.08)", border: `1px solid ${theme.down}`, borderRadius: 3, padding: "4px 8px" }}>
            {saveErr}
          </div>
        )}
      </div>
    </div>
  );
}

registerWidget({
  id: "note-editor",
  title: "Note Editor",
  component: NoteEditor,
  defaultSize: { w: 5, h: 20 },
  minSize: { w: 4, h: 10 },
  defaultConfig: {},
  category: "Journal",
  help:
    "Draft a research note or thesis as a five-step ladder: one sentence, why it matters, action, risk/invalidation, then the long-form body. Evidence refs (kind + id) resolve against the record as you add them once the note is saved — an unresolvable one shows its error right on that row. Signing is one-way, so it asks you to confirm.",
  description: "The ladder — one sentence to evidence — draft, cite, sign",
});
