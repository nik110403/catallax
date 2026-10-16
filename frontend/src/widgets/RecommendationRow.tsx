// Recommendation rows — a row COMPONENT used inside
// ReportView's "what to do next" section. Deliberately NOT a registered
// widget: a weekly report is one document with one reading
// order, and a floating "Recommendations" panel duplicating half of it
// invites the two copies to drift.
//
// Four row kinds, each rendered distinctly (never mixed into one plain list):
//   - `action:"refuse"` rows (the cash-floor rule refusing to size itself
//     because clearing accounts don't sum to zero) are an ALARM, not a list
//     item — a full-width banner.
//   - `status:"proposed"` rows are actionable: Accept/Override, a REQUIRED
//     reason captured before the POST (B2), ≤4 items visible at rest with
//     the ~15 tax/snapshot keys behind a one-click disclosure.
//   - decided rows (`status:"accepted"`/`"overridden"`) — sourced from
//     `where_we_stand.rule_evaluations`, NOT from session-local state (fix
//     round P2a: the prior session-local-only tracking made a decision
//     vanish on reload since the backend's own `proposed`/`refused` arrays
//     never include an already-decided row). Rendered as a plain leaf: no
//     buttons, just the decision badge + rule note + whatever `arithmetic`
//     survived the stripped `rule_evaluations` projection.
//   - near-miss rows (`status:"evaluated"` with `arithmetic.material ===
//     false` — committed fde86ce/82759ab) render muted, no buttons, below
//     the proposals, with their "would-fire-at" figure carrying its
//     comparison at micro size, never a hero (items 19–20). The row's own
//     `note` always renders verbatim regardless of which `arithmetic` keys
//     are present — nothing here is ever re-derived client-side.
import { useRef, useState } from "react";

import { ApiError, api } from "../api/client";
import type { DecisionRow, Recommendation, RuleEvaluation } from "../api/types";
import { theme } from "../theme";
import { PanelState } from "./PanelState";
import {
  ACTION_COLOR,
  ArithmeticGrid,
  StatusBadge,
  decisionJoinKey,
  decisionSymbol,
  eur,
  evalSymbol,
  localDatetimeInputDefault,
  localDatetimeInputToISOString,
} from "./reportsShared";
import { InlineError } from "./InlineError";

// Phase 18: widened from {"accepted","overridden"} to the full set of
// outcomes a card can now record locally. `"rejected"` is a CLIENT-ONLY tag —
// the server-persisted status for a reject is `"overridden"` (F1, the frozen
// cooldown loop), so `applyLocalOutcome` below maps it to that status while
// also stamping `input_snapshot.human_action = "reject"` so the card's own
// chip-label logic (same `human_action` check `humanNote` already uses) can
// tell a reject apart from a genuine override.
export type LocalOutcome = "accepted" | "overridden" | "modified" | "rejected" | "executed";

const LOCAL_OUTCOME_STATUS: Record<LocalOutcome, string> = {
  accepted: "accepted",
  overridden: "overridden",
  modified: "modified",
  rejected: "overridden",
  executed: "executed",
};

function applyLocalOutcome(row: Recommendation, outcome: LocalOutcome | undefined): Recommendation {
  if (!outcome) return row;
  const status = LOCAL_OUTCOME_STATUS[outcome];
  if (outcome !== "rejected") return { ...row, status };
  return { ...row, status, input_snapshot: { ...row.input_snapshot, human_action: "reject" } };
}

const btnBase: React.CSSProperties = {
  minHeight: 24,
  minWidth: 24,
  borderRadius: 4,
  fontSize: theme.fontSize.micro,
  fontWeight: 700,
  cursor: "pointer",
  padding: "6px 14px",
};

// ── alarm banner: action:"refuse" is never a plain list item ───────────
function RefusalBanner({ row }: { row: Recommendation }) {
  return (
    <div
      role="alert"
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 4,
        background: "rgba(248,81,73,0.10)",
        border: `1px solid ${theme.down}`,
        borderRadius: 4,
        padding: "8px 10px",
        marginBottom: 8,
      }}
    >
      <span style={{ fontSize: theme.fontSize.sm, fontWeight: 700, color: theme.down, textTransform: "uppercase", letterSpacing: "0.03em" }}>
        {row.rule_id} refused to act
      </span>
      {row.note && <span style={{ fontSize: theme.fontSize.micro, color: theme.text }}>{row.note}</span>}
      <ArithmeticGrid data={row.input_snapshot} />
    </div>
  );
}

// ── actionable / already-decided card ───────────────────────────────────
// Phase 18: gained Modify (its own sizing + note sub-form, toggled by
// `mode`) and Reject (shares the Accept/Override reason field — same
// required-reason law, B2) alongside the original Accept/Override, plus a
// SEPARATE Execute sub-form that appears once the row is no longer
// `proposed` (`accepted`/`modified` only — the lifecycle service's own
// legal-transition table). Execute gets its own note field: it fires in a
// different lifecycle stage than the other four, on a card whose `note`
// state may already hold a stale reason from the earlier accept/modify step.
function RecommendationCard({
  row,
  demoMode,
  onActed,
  onRefetch,
  justRecorded,
}: {
  row: Recommendation;
  demoMode: boolean;
  onActed: (id: number, outcome: LocalOutcome) => void;
  onRefetch: () => void;
  // P1: true while `row`'s status reflects a
  // session-local optimistic override still awaiting the refetch that
  // confirms it (see the `[report]`-keyed clear in ReportView.tsx) — the
  // ONLY window where the buttons above have vanished (status is no longer
  // "proposed") but nothing on screen yet says the action succeeded.
  justRecorded: boolean;
}) {
  const [note, setNote] = useState("");
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState<"accept" | "override" | "modify" | "reject" | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [mode, setMode] = useState<"actions" | "modify">("actions");
  const [sizingOverride, setSizingOverride] = useState("");
  // P2: Modify's own per-field touched —
  // separate from `touched` above (which governs the Accept/Override/Reject
  // reason field, a genuine single-field form). Modify has TWO fields
  // (sizing + reason) sharing one flag reddened both borders on either
  // field's blur, including whichever one the user hadn't touched yet.
  const [touchedSizing, setTouchedSizing] = useState(false);
  const [touchedReason, setTouchedReason] = useState(false);
  // P3: Modify's reason field used to share
  // `note` with the Accept/Override/Reject field above — the same state
  // variable backing two different forms on the same card. Toggling into
  // Modify after typing (and not submitting) an Accept/Override reason
  // pre-filled Modify's box with it, and vice versa on Cancel. A dedicated
  // `modifyNote` buffer means a reason typed in one form can never leak into
  // the other.
  const [modifyNote, setModifyNote] = useState("");
  const modifyReasonEmpty = modifyNote.trim().length === 0;

  const actionable = row.status === "proposed";
  const reasonEmpty = note.trim().length === 0;
  const reasonInvalid = touched && reasonEmpty;
  const errId = `reason-error-${row.id}`;
  const humanNote = typeof row.input_snapshot?.human_note === "string" ? (row.input_snapshot.human_note as string) : null;
  // The chip must read "Rejected" for a human reject even though the
  // PERSISTED status is `overridden` — the one place that string ever
  // diverges from `row.status` itself.
  const humanAction = typeof row.input_snapshot?.human_action === "string" ? (row.input_snapshot.human_action as string) : null;
  const statusLabel = row.status === "overridden" ? (humanAction === "reject" ? "Rejected" : "Overridden") : undefined;

  const act = async (kind: "accept" | "override" | "reject") => {
    const reason = note.trim();
    if (!reason) {
      // P2: don't
      // also set `err` here — `reasonInvalid` (keyed on `touched`) already
      // renders this exact sentence below; this branch is dead in practice
      // (the buttons are `disabled` while `reasonEmpty`) but kept as a
      // defensive guard, so it must not duplicate the message when it does
      // run — same reasoning `submitModify` below already applies.
      setTouched(true);
      return;
    }
    setBusy(kind);
    setErr(null);
    try {
      if (kind === "accept") await api.acceptRecommendation(row.id, reason);
      else if (kind === "override") await api.overrideRecommendation(row.id, reason);
      else await api.rejectRecommendation(row.id, reason);
      // Instant local feedback (this row flips to its new status right
      // away) PLUS a real refetch (fix round P2a) — the append-only backend
      // never updates this row again, so the durable source of truth for
      // "is this decided" is the next `where_we_stand.rule_evaluations`
      // read, not client memory. Without the refetch, a reload made the
      // decision look like it never happened.
      onActed(row.id, kind === "accept" ? "accepted" : kind === "override" ? "overridden" : "rejected");
      onRefetch();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "action failed — please try again");
    } finally {
      setBusy(null);
    }
  };

  const sizingNum = Number(sizingOverride);
  const sizingInvalid = sizingOverride.trim().length === 0 || !Number.isFinite(sizingNum);
  const submitModify = async () => {
    const reason = modifyNote.trim();
    if (!reason || sizingInvalid) {
      // P2: don't also set `err` here — the
      // inline validation span just below (keyed on `touchedSizing`/
      // `touchedReason`) already renders this exact sentence; setting `err`
      // too rendered it TWICE (Modify's submit button, unlike Accept/
      // Override/Reject's, isn't `disabled` while invalid, so this branch
      // is genuinely reachable). Submit marks BOTH fields touched (unlike a
      // blur, which marks only the one field left) so a submit attempt
      // still surfaces every remaining problem at once.
      setTouchedSizing(true);
      setTouchedReason(true);
      return;
    }
    setBusy("modify");
    setErr(null);
    try {
      await api.modifyRecommendation(row.id, reason, sizingNum);
      onActed(row.id, "modified");
      onRefetch();
      setMode("actions");
      setModifyNote("");
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "modify failed — please try again");
    } finally {
      setBusy(null);
    }
  };

  return (
    <div style={{ borderTop: `1px solid ${theme.border}`, padding: "8px 0", display: "flex", flexDirection: "column", gap: 6 }}>
      {/* At-rest header: rule, action, sizing, status — ≤4 items. */}
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <span style={{ fontSize: theme.fontSize.sm, fontWeight: 600, color: theme.text }}>{row.rule_id}</span>
        <span className="num" style={{ fontSize: theme.fontSize.micro, color: ACTION_COLOR[row.action] ?? theme.muted, textTransform: "uppercase" }}>
          {row.action}
        </span>
        {row.sizing !== null && <span className="num" style={{ fontSize: theme.fontSize.micro, color: theme.text }}>{eur(row.sizing)}</span>}
        <StatusBadge status={row.status} label={statusLabel} />
      </div>

      {/* Rule note vs. human note — labeled distinctly, never merged. */}
      {row.note && (
        <div style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>
          <span style={{ color: theme.muted, textTransform: "uppercase", letterSpacing: "0.03em" }}>rule: </span>
          {row.note}
        </div>
      )}
      {humanNote && (
        <div style={{ fontSize: theme.fontSize.micro, color: theme.text }}>
          <span style={{ color: theme.muted, textTransform: "uppercase", letterSpacing: "0.03em" }}>your reason: </span>
          {humanNote}
        </div>
      )}

      {/* Progressive disclosure: the ~15 tax/snapshot keys are NOT visible at rest. */}
      {(row.tax_cost_arithmetic || row.input_snapshot) && (
        <details>
          <summary style={{ fontSize: theme.fontSize.micro, color: theme.muted, cursor: "pointer" }}>arithmetic</summary>
          <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 6 }}>
            {row.tax_cost_arithmetic && (
              <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                <span style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.04em" }}>TAX / COST</span>
                <ArithmeticGrid data={row.tax_cost_arithmetic} />
              </div>
            )}
            {row.input_snapshot && (
              <div style={{ display: "flex", flexDirection: "column", gap: 3 }}>
                <span style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.04em" }}>INPUT SNAPSHOT</span>
                <ArithmeticGrid data={row.input_snapshot} exclude={["human_note"]} />
              </div>
            )}
          </div>
        </details>
      )}

      {actionable && (
        <div style={{ display: "flex", flexDirection: "column", gap: 4, marginTop: 2 }}>
          {demoMode ? (
            <span style={{ fontSize: theme.fontSize.micro, color: theme.accent }}>
              Demo mode — accept/override is disabled (read-only sample data).
            </span>
          ) : mode === "modify" ? (
            <>
              <div style={{ display: "flex", gap: 6, alignItems: "flex-start", flexWrap: "wrap" }}>
                <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
                  <label htmlFor={`sizing-${row.id}`} style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>
                    New sizing (EUR, required)
                  </label>
                  <input
                    id={`sizing-${row.id}`}
                    type="number"
                    value={sizingOverride}
                    onChange={(e) => setSizingOverride(e.target.value)}
                    onBlur={() => setTouchedSizing(true)}
                    aria-required="true"
                    aria-invalid={touchedSizing && sizingInvalid}
                    style={{
                      width: 120,
                      background: theme.bg,
                      color: theme.text,
                      border: `1px solid ${touchedSizing && sizingInvalid ? theme.down : theme.border}`,
                      borderRadius: 4,
                      padding: "5px 8px",
                      minHeight: 24,
                      fontSize: theme.fontSize.micro,
                    }}
                  />
                </div>
                {/* P2: a real visible
                    <label>, matching the sizing field beside it — the prior
                    aria-label + placeholder-as-label pattern (already fixed
                    on the Execute form) survived here, one sub-form over. */}
                <div style={{ display: "flex", flexDirection: "column", gap: 2, flex: "1 1 160px", minWidth: 120 }}>
                  <label htmlFor={`modify-reason-${row.id}`} style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>
                    Reason (required)
                  </label>
                  <input
                    id={`modify-reason-${row.id}`}
                    value={modifyNote}
                    onChange={(e) => setModifyNote(e.target.value)}
                    onBlur={() => setTouchedReason(true)}
                    aria-required="true"
                    aria-invalid={touchedReason && modifyReasonEmpty}
                    style={{
                      background: theme.bg,
                      color: theme.text,
                      border: `1px solid ${touchedReason && modifyReasonEmpty ? theme.down : theme.border}`,
                      borderRadius: 4,
                      padding: "5px 8px",
                      minHeight: 24,
                      fontSize: theme.fontSize.micro,
                    }}
                  />
                </div>
                <button
                  type="button"
                  disabled={busy !== null}
                  onClick={() => void submitModify()}
                  style={{ ...btnBase, background: theme.accent, color: theme.bg, border: `1px solid ${theme.accent}`, cursor: busy ? "not-allowed" : "pointer", alignSelf: "flex-end" }}
                >
                  {busy === "modify" ? "SAVING…" : "CONFIRM MODIFY"}
                </button>
                <button
                  type="button"
                  disabled={busy !== null}
                  onClick={() => {
                    setMode("actions");
                    // Buffer is Modify's own (see `modifyNote` above) — clear
                    // it on Cancel so reopening Modify never shows a stale
                    // reason from an abandoned edit.
                    setModifyNote("");
                    setTouchedReason(false);
                    setTouchedSizing(false);
                  }}
                  style={{ ...btnBase, background: "transparent", color: theme.muted, border: `1px solid ${theme.border}`, cursor: busy ? "not-allowed" : "pointer", alignSelf: "flex-end" }}
                >
                  CANCEL
                </button>
              </div>
              {(touchedSizing || touchedReason) && (modifyReasonEmpty || sizingInvalid) && (
                <span role="alert" style={{ fontSize: theme.fontSize.micro, color: theme.down }}>
                  A reason and a valid sizing override are both required.
                </span>
              )}
            </>
          ) : (
            <>
              <label htmlFor={`reason-${row.id}`} style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>
                Reason (required)
              </label>
              <div style={{ display: "flex", gap: 6, alignItems: "flex-start", flexWrap: "wrap" }}>
                <input
                  id={`reason-${row.id}`}
                  value={note}
                  onChange={(e) => setNote(e.target.value)}
                  onBlur={() => setTouched(true)}
                  aria-required="true"
                  aria-invalid={reasonInvalid}
                  aria-describedby={reasonInvalid ? errId : undefined}
                  style={{
                    flex: "1 1 160px",
                    minWidth: 120,
                    background: theme.bg,
                    color: theme.text,
                    border: `1px solid ${reasonInvalid ? theme.down : theme.border}`,
                    borderRadius: 4,
                    padding: "5px 8px",
                    minHeight: 24,
                    fontSize: theme.fontSize.micro,
                  }}
                />
                {/* Primary/secondary hierarchy: Accept is the
                    filled, primary action; Override/Modify/Reject are
                    quieter, outlined secondary ones. Every one is a real
                    <button> — no link styling, underline or chevron (item
                    25) — and ≥24×24, unlike the house .icon-btn
                    breach. */}
                <button
                  type="button"
                  disabled={busy !== null || reasonEmpty}
                  onClick={() => void act("accept")}
                  style={{
                    ...btnBase,
                    background: theme.up,
                    color: theme.bg,
                    border: `1px solid ${theme.up}`,
                    opacity: reasonEmpty ? 0.5 : 1,
                    cursor: busy || reasonEmpty ? "not-allowed" : "pointer",
                  }}
                >
                  {busy === "accept" ? "ACCEPTING…" : "ACCEPT"}
                </button>
                <button
                  type="button"
                  disabled={busy !== null || reasonEmpty}
                  onClick={() => void act("override")}
                  style={{
                    ...btnBase,
                    background: "transparent",
                    color: theme.muted,
                    border: `1px solid ${theme.border}`,
                    opacity: reasonEmpty ? 0.5 : 1,
                    cursor: busy || reasonEmpty ? "not-allowed" : "pointer",
                  }}
                >
                  {busy === "override" ? "OVERRIDING…" : "OVERRIDE"}
                </button>
                <button
                  type="button"
                  disabled={busy !== null}
                  onClick={() => setMode("modify")}
                  style={{ ...btnBase, background: "transparent", color: theme.accent, border: `1px solid ${theme.accent}`, cursor: busy ? "not-allowed" : "pointer" }}
                >
                  MODIFY
                </button>
                <button
                  type="button"
                  disabled={busy !== null || reasonEmpty}
                  onClick={() => void act("reject")}
                  style={{
                    ...btnBase,
                    background: "transparent",
                    color: theme.down,
                    border: `1px solid ${theme.down}`,
                    opacity: reasonEmpty ? 0.5 : 1,
                    cursor: busy || reasonEmpty ? "not-allowed" : "pointer",
                  }}
                >
                  {busy === "reject" ? "REJECTING…" : "REJECT"}
                </button>
              </div>
              {reasonInvalid && (
                <span id={errId} role="alert" style={{ fontSize: theme.fontSize.micro, color: theme.down }}>
                  A reason is required before you can act on this recommendation.
                </span>
              )}
              <span style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>
                Recorded permanently — this cannot be undone (append-only decision log).
              </span>
            </>
          )}
        </div>
      )}

      {/* P1: the only window where the
          buttons above have vanished (a local override already flipped
          `row.status` away from "proposed") but the real refetch hasn't
          landed yet — an explicit confirmation instead of the row just
          silently going quiet. Clears itself the moment `report` (not
          `refreshKey`) actually arrives, see ReportView.tsx. */}
      {!actionable && justRecorded && (
        <div role="status" style={{ fontSize: theme.fontSize.micro, color: theme.up }}>
          Recorded — confirming on next refresh…
        </div>
      )}

      {/* Execute used to live here, gated on `row.status === "accepted" ||
          "modified"` — but `RecommendationCard` only ever renders for
          `proposed` rows (`RecommendationList` maps `proposed.map(...)` to
          it exclusively; an accepted/modified row moves to `DecidedRow` via
          `where_we_stand.rule_evaluations`), so that gate could never be
          true. Design review: Execute now lives on
          `DecidedRow` below (`ExecuteForm`), fed the current LEAF
          recommendation id from the `/decisions` projection — the one thing
          the FROZEN `advisor_report.py` projection this card also reads
          never carried. */}
      {err && <InlineError msg={err} variant="text" />}
    </div>
  );
}

// ── near-miss: muted, no buttons, below the proposals, different styling.
// `row.arithmetic` is the backend's committed shape (fde86ce/82759ab); this
// week's live report may legitimately have zero of these (a plain in-band
// hold never stamps `material` at all) — the row's own `note` always
// renders verbatim regardless of which `arithmetic` keys are present.
function NearMissRow({ row }: { row: RuleEvaluation }) {
  const a = row.arithmetic;
  const hasFigure = typeof a?.benefit_eur === "number" && typeof a?.threshold_eur === "number";
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2, padding: "4px 0", borderBottom: `1px solid ${theme.grid}` }}>
      <div style={{ display: "flex", gap: 8, alignItems: "baseline" }}>
        <span style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>
          {row.rule_id}
          {a?.symbol ? ` · ${a.symbol}` : ""}
        </span>
        {hasFigure && (
          <span className="num" style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>
            {eur(a?.benefit_eur)} · fires at {eur(a?.threshold_eur)}
          </span>
        )}
      </div>
      {row.note && <span style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>{row.note}</span>}
    </div>
  );
}

// ── join key between `where_we_stand.rule_evaluations` (RuleEvaluation, no
// id — the FROZEN advisor_report.py projection) and the `/decisions`
// projection's rows (DecisionRow, id = the current LEAF recommendation id).
// `decisionSymbol`/`evalSymbol`/`decisionJoinKey` now live in
// reportsShared.tsx: this file's own
// prior version inferred the symbol from `funding_source`'s SHAPE
// (`kind==="proceeds" && symbols.length===1`), which a single-leg
// `cash_floor_band` trim satisfies identically to `weight_band_trim` —
// silently reproducing the exact P0 (Execute unreachable) this whole join
// exists to fix. The corrected version gates on `rule_id`, matching
// `advisor_report.py::_load_overrides` exactly (symbol is part of the key
// ONLY for `rule_id === "weight_band_trim"`); see reportsShared.tsx and its
// unit test for the three covered cases.

// ── Execute — moved here from `RecommendationCard` (a design-review fix): that card only ever renders `status:"proposed"` rows, so
// its own `executable` gate (`status === "accepted" || "modified"`) could
// never fire. `DecidedRow` below is the one place an accepted/modified row
// actually lives, and the `/decisions` projection (NOT the frozen
// `advisor_report.py` one this file also reads) is the one place its
// current LEAF recommendation id is available — `recommendationId` is
// always that id, never `row.id` off a `RuleEvaluation` (which has none).
function ExecuteForm({
  recommendationId,
  demoMode,
  onActed,
  onRefetch,
  onCancel,
}: {
  recommendationId: number;
  demoMode: boolean;
  onActed: (id: number, outcome: LocalOutcome) => void;
  onRefetch: () => void;
  // P2: collapses this form back to the
  // "Record execution" disclosure it opened from (DecidedRow, below) —
  // unmounting resets every field for free, matching Modify's own CANCEL.
  onCancel: () => void;
}) {
  const [executedAt, setExecutedAt] = useState(() => localDatetimeInputDefault());
  const [fillPrice, setFillPrice] = useState("");
  const [quantity, setQuantity] = useState("");
  const [venue, setVenue] = useState("");
  const [executeNote, setExecuteNote] = useState("");
  // P2: one touched flag PER FIELD, not one
  // for the whole form — blurring Quantity used to redden all five borders
  // (and announce all five as invalid to a screen reader), including fields
  // never visited. A failed submit still marks every field touched (see
  // `submit` below) so the user sees every remaining problem at once.
  const [touched, setTouched] = useState({ at: false, price: false, qty: false, venue: false, note: false });
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const executedAtRef = useRef<HTMLInputElement>(null);
  const fillPriceRef = useRef<HTMLInputElement>(null);
  const quantityRef = useRef<HTMLInputElement>(null);
  const venueRef = useRef<HTMLInputElement>(null);
  const noteRef = useRef<HTMLInputElement>(null);

  // P1: a plain ISO-8601-shaped text field, not
  // `type="datetime-local"` — that native control's VALUE is ISO but its
  // rendered widget UI follows the browser/OS locale (en-US Chrome shows
  // mm/dd/yyyy, hh:mm AM/PM), which is a US date sitting on a document
  // where every other timestamp (as_of, review_at, expires_at, lifecycle
  // `created_at`) is printed ISO. A validated text field keeps the DISPLAY
  // ISO too, not just the value.
  //
  // P1: the value is LOCAL time, not UTC —
  // the prior default (`new Date().toISOString().slice(0,16)`) read the
  // clock in UTC while `submit` re-parsed the same offset-less string as
  // LOCAL (ECMA-262 date-time grammar: no offset = local), so accepting the
  // default unedited recorded a fill hours early in a non-UTC zone (e.g. UTC+2). Both
  // ends now agree: `localDatetimeInputDefault` builds the default from
  // LOCAL date components, and `localDatetimeInputToISOString` (submit,
  // below) reads that same string back as local — the interpretation it was
  // always going to get. See reportsShared.tsx for both and their test.
  const ISO_DATETIME_RE = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$/;
  const executedAtInvalid = !ISO_DATETIME_RE.test(executedAt);
  const fillPriceNum = Number(fillPrice);
  const fillPriceInvalid = fillPrice.trim().length === 0 || !Number.isFinite(fillPriceNum);
  const quantityNum = Number(quantity);
  const quantityInvalid = quantity.trim().length === 0 || !Number.isFinite(quantityNum);
  const venueInvalid = venue.trim().length === 0;
  const noteInvalid = executeNote.trim().length === 0;
  const invalid = executedAtInvalid || fillPriceInvalid || quantityInvalid || venueInvalid || noteInvalid;

  const idBase = `execute-${recommendationId}`;
  const errorId = `${idBase}-error`;

  const submit = async () => {
    if (invalid) {
      setTouched({ at: true, price: true, qty: true, venue: true, note: true });
      // P1: focus the FIRST invalid field, in form order — previously an
      // invalid submit produced one generic error sentence with no
      // indication of which of the 5 fields it was about.
      const firstInvalidRef = executedAtInvalid
        ? executedAtRef
        : fillPriceInvalid
          ? fillPriceRef
          : quantityInvalid
            ? quantityRef
            : venueInvalid
              ? venueRef
              : noteRef;
      firstInvalidRef.current?.focus();
      return;
    }
    setBusy(true);
    setErr(null);
    try {
      await api.executeRecommendation(recommendationId, {
        note: executeNote.trim(),
        executed_at: localDatetimeInputToISOString(executedAt),
        fill_price: fillPriceNum,
        quantity: quantityNum,
        venue: venue.trim(),
      });
      onActed(recommendationId, "executed");
      onRefetch();
    } catch (e) {
      setErr(e instanceof ApiError ? e.message : "execute failed — please try again");
    } finally {
      setBusy(false);
    }
  };

  if (demoMode) {
    return (
      <span style={{ fontSize: theme.fontSize.micro, color: theme.accent }}>
        Demo mode — execute is disabled (read-only sample data).
      </span>
    );
  }

  const fieldStyle = (isTouched: boolean, isInvalid: boolean): React.CSSProperties => ({
    background: theme.bg,
    color: theme.text,
    border: `1px solid ${isTouched && isInvalid ? theme.down : theme.border}`,
    borderRadius: 4,
    padding: "5px 8px",
    minHeight: 24,
    fontSize: theme.fontSize.micro,
  });
  const labelStyle: React.CSSProperties = { fontSize: theme.fontSize.micro, color: theme.muted };
  // P2: each field's OWN short hint/error —
  // previously all five pointed `aria-describedby` at one generic sentence
  // ("Executed-at ... are all required"), so a screen-reader user on
  // Quantity heard every other field's requirement too. The `role=alert`
  // summary below is unchanged and still announces on its own (that ARIA
  // role is itself a live region, no `aria-describedby` wiring needed for
  // it to be heard).
  const hintStyle = (isTouched: boolean, isInvalid: boolean): React.CSSProperties => ({
    fontSize: theme.fontSize.micro,
    color: isTouched && isInvalid ? theme.down : theme.muted,
  });

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 6, marginTop: 2, borderTop: `1px dashed ${theme.border}`, paddingTop: 6 }}>
      <span style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.04em", textTransform: "uppercase" }}>
        Record execution
      </span>
      <div style={{ display: "flex", gap: 6, alignItems: "flex-start", flexWrap: "wrap" }}>
        <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          <label htmlFor={`${idBase}-at`} style={labelStyle}>Executed at (local time, ISO)</label>
          <input
            id={`${idBase}-at`}
            ref={executedAtRef}
            placeholder="YYYY-MM-DDTHH:MM"
            value={executedAt}
            onChange={(e) => setExecutedAt(e.target.value)}
            onBlur={() => setTouched((t) => ({ ...t, at: true }))}
            aria-required="true"
            aria-invalid={touched.at && executedAtInvalid}
            aria-describedby={`${idBase}-at-hint`}
            style={{ ...fieldStyle(touched.at, executedAtInvalid), width: 150 }}
          />
          <span id={`${idBase}-at-hint`} style={hintStyle(touched.at, executedAtInvalid)}>
            {touched.at && executedAtInvalid ? "Enter local time as YYYY-MM-DDTHH:MM" : "Local time, YYYY-MM-DDTHH:MM"}
          </span>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          <label htmlFor={`${idBase}-price`} style={labelStyle}>Fill price (EUR, required)</label>
          <input
            id={`${idBase}-price`}
            ref={fillPriceRef}
            type="number"
            value={fillPrice}
            onChange={(e) => setFillPrice(e.target.value)}
            onBlur={() => setTouched((t) => ({ ...t, price: true }))}
            aria-required="true"
            aria-invalid={touched.price && fillPriceInvalid}
            aria-describedby={`${idBase}-price-hint`}
            style={{ ...fieldStyle(touched.price, fillPriceInvalid), width: 100 }}
          />
          <span id={`${idBase}-price-hint`} style={hintStyle(touched.price, fillPriceInvalid)}>
            {touched.price && fillPriceInvalid ? "Enter the fill price in EUR" : "EUR"}
          </span>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          <label htmlFor={`${idBase}-qty`} style={labelStyle}>Quantity (required)</label>
          <input
            id={`${idBase}-qty`}
            ref={quantityRef}
            type="number"
            value={quantity}
            onChange={(e) => setQuantity(e.target.value)}
            onBlur={() => setTouched((t) => ({ ...t, qty: true }))}
            aria-required="true"
            aria-invalid={touched.qty && quantityInvalid}
            aria-describedby={`${idBase}-qty-hint`}
            style={{ ...fieldStyle(touched.qty, quantityInvalid), width: 100 }}
          />
          <span id={`${idBase}-qty-hint`} style={hintStyle(touched.qty, quantityInvalid)}>
            {touched.qty && quantityInvalid ? "Enter the quantity" : "Units"}
          </span>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          <label htmlFor={`${idBase}-venue`} style={labelStyle}>Venue (required)</label>
          <input
            id={`${idBase}-venue`}
            ref={venueRef}
            value={venue}
            onChange={(e) => setVenue(e.target.value)}
            onBlur={() => setTouched((t) => ({ ...t, venue: true }))}
            aria-required="true"
            aria-invalid={touched.venue && venueInvalid}
            aria-describedby={`${idBase}-venue-hint`}
            style={{ ...fieldStyle(touched.venue, venueInvalid), width: 120 }}
          />
          <span id={`${idBase}-venue-hint`} style={hintStyle(touched.venue, venueInvalid)}>
            {touched.venue && venueInvalid ? "Enter the execution venue" : "Where the fill executed"}
          </span>
        </div>
        <div style={{ display: "flex", flexDirection: "column", gap: 2, flex: "1 1 160px", minWidth: 140 }}>
          <label htmlFor={`${idBase}-note`} style={labelStyle}>Note (required)</label>
          <input
            id={`${idBase}-note`}
            ref={noteRef}
            value={executeNote}
            onChange={(e) => setExecuteNote(e.target.value)}
            onBlur={() => setTouched((t) => ({ ...t, note: true }))}
            aria-required="true"
            aria-invalid={touched.note && noteInvalid}
            aria-describedby={`${idBase}-note-hint`}
            style={fieldStyle(touched.note, noteInvalid)}
          />
          <span id={`${idBase}-note-hint`} style={hintStyle(touched.note, noteInvalid)}>
            {touched.note && noteInvalid ? "Enter a note" : "Why this execution is being recorded"}
          </span>
        </div>
        <button
          type="button"
          disabled={busy}
          onClick={() => void submit()}
          style={{ ...btnBase, background: theme.up, color: theme.bg, border: `1px solid ${theme.up}`, cursor: busy ? "not-allowed" : "pointer", alignSelf: "flex-end" }}
        >
          {busy ? "RECORDING…" : "EXECUTE"}
        </button>
        {/* P2: a CANCEL, matching Modify's —
            collapses this form back to the "Record execution" disclosure
            (DecidedRow, below) via `onCancel`, resetting every field for
            free since the component simply unmounts. */}
        <button
          type="button"
          disabled={busy}
          onClick={onCancel}
          style={{ ...btnBase, background: "transparent", color: theme.muted, border: `1px solid ${theme.border}`, cursor: busy ? "not-allowed" : "pointer", alignSelf: "flex-end" }}
        >
          CANCEL
        </button>
      </div>
      {/* P2: ONE error sentence, not two — this is the only invalid-state
          message for this form (no duplicate `err`-state copy). Shown once
          at least one field has been touched (blur or a failed submit,
          which touches all five) and something is still invalid. */}
      {invalid && (touched.at || touched.price || touched.qty || touched.venue || touched.note) && (
        <span id={errorId} role="alert" style={{ fontSize: theme.fontSize.micro, color: theme.down }}>
          Executed-at (local time, ISO), fill price, quantity, venue and a note are all required.
        </span>
      )}
      <span style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>
        Recorded permanently — this cannot be undone (append-only decision log).
      </span>
      {err && <InlineError msg={err} variant="text" />}
    </div>
  );
}

// ── decided: a chain leaf (accepted/overridden/modified/…), sourced from
// `rule_evaluations` — the durable, reload-proof source of truth (fix round
// P2a). No accept/override/modify buttons (there's nothing left to decide
// on that axis); whatever `arithmetic` survived the stripped projection
// renders in the same generic grid. `decision` (optional — undefined on a
// pre-Phase-18 week or a failed projection build) is this row's matching
// `/decisions` projection entry, joined by `decisionJoinKey` above; ONLY
// its presence, with `status` "accepted"/"modified", unlocks Execute.
function DecidedRow({
  row,
  decision,
  demoMode,
  onActed,
  onRefetch,
  justRecorded,
}: {
  row: RuleEvaluation;
  decision?: DecisionRow;
  demoMode: boolean;
  onActed: (id: number, outcome: LocalOutcome) => void;
  onRefetch: () => void;
  justRecorded: boolean;
}) {
  // fix-first #15: gated on `decision.status` alone, `/decisions`
  // (fetched independently of the advisor_report `report` that drives
  // `row.status`) could still say "accepted"/"modified" for a beat after
  // `report` itself had already advanced this row to "executed" — RECORD
  // EXECUTION briefly re-offered on a row that was already done. Require
  // BOTH the decision projection AND the report's own row status to agree
  // the row is still open to execute.
  const executable =
    !!decision &&
    (decision.status === "accepted" || decision.status === "modified") &&
    (row.status === "accepted" || row.status === "modified");
  // P2: Execute used to render as a
  // permanently open five-field form on EVERY executable row — three
  // accepted rows in a week meant 15 always-visible inputs under "DECIDED
  // THIS WEEK". Now behind its own "Record execution" disclosure, matching
  // Modify's collapsed-by-default pattern elsewhere on this card.
  const [recording, setRecording] = useState(false);
  // Optimistic: while `justRecorded` (a real POST landed, refetch pending),
  // show "executed" immediately rather than the stale accepted/modified
  // badge — same instant-feedback-plus-real-refetch contract `act()` above
  // already uses for accept/override/reject.
  const displayStatus = justRecorded ? "executed" : row.status;
  return (
    <div style={{ borderTop: `1px solid ${theme.grid}`, padding: "6px 0", display: "flex", flexDirection: "column", gap: 4 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        <span style={{ fontSize: theme.fontSize.sm, color: theme.text }}>{row.rule_id}</span>
        <span className="num" style={{ fontSize: theme.fontSize.micro, color: ACTION_COLOR[row.action] ?? theme.muted, textTransform: "uppercase" }}>
          {row.action}
        </span>
        <StatusBadge status={displayStatus} />
      </div>
      {row.note && <span style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>{row.note}</span>}
      {row.arithmetic && Object.keys(row.arithmetic).length > 0 && (
        <ArithmeticGrid data={row.arithmetic as unknown as Record<string, unknown>} />
      )}
      {justRecorded && (
        <div role="status" style={{ fontSize: theme.fontSize.micro, color: theme.up }}>
          Recorded — confirming on next refresh…
        </div>
      )}
      {executable && !justRecorded && (
        recording ? (
          <ExecuteForm
            recommendationId={decision!.id}
            demoMode={demoMode}
            onActed={(id, outcome) => {
              // Success path: a fill was recorded, `justRecorded` will
              // hide this whole disclosure on the next render anyway, but
              // `recording` itself was never cleared — so a later
              // re-render (row rehydrated with a fresh, un-executed
              // decision after ReportView clears the `locally` entry)
              // reopened this ExecuteForm empty by itself. Failure path
              // (thrown before `onActed` runs, see `submit` above) is
              // unaffected — the form stays open with the API error.
              onActed(id, outcome);
              setRecording(false);
            }}
            onRefetch={onRefetch}
            onCancel={() => setRecording(false)}
          />
        ) : (
          <button
            type="button"
            onClick={() => setRecording(true)}
            style={{ ...btnBase, background: "transparent", color: theme.accent, border: `1px solid ${theme.accent}`, cursor: "pointer", alignSelf: "flex-start" }}
          >
            RECORD EXECUTION
          </button>
        )
      )}
    </div>
  );
}

export function RecommendationList({
  proposed,
  refused,
  decided,
  nearMiss,
  decisions,
  demoMode,
  onActed,
  onRefetch,
  locally,
}: {
  proposed: Recommendation[];
  refused: Recommendation[];
  decided: RuleEvaluation[];
  nearMiss: RuleEvaluation[];
  // The `/decisions` projection's rows for this same week (a design-review fix) — additive: an empty array (pre-Phase-18 week, or the
  // projection failed to build) degrades to "no Execute form on any decided
  // row", never a crash.
  decisions: DecisionRow[];
  demoMode: boolean;
  onActed: (id: number, outcome: LocalOutcome) => void;
  onRefetch: () => void;
  locally: Record<number, LocalOutcome>;
}) {
  const applyLocal = (r: Recommendation): Recommendation => applyLocalOutcome(r, locally[r.id]);
  const decisionByKey = new Map<string, DecisionRow>();
  for (const d of decisions) {
    decisionByKey.set(decisionJoinKey(d.rule_id, decisionSymbol(d)), d);
  }
  // "No open recommendations" means no PENDING decision — it must print
  // whenever proposed+refused is empty, even when decided/near-miss rows
  // exist below (fix round 2, P3): a reader scanning this section needs to
  // know at a glance there's nothing left to act on this week, not have
  // that fact suppressed just because history has entries.
  const noOpen = proposed.length === 0 && refused.length === 0;
  // One constant, not two verbatim-duplicated copies (fix round 3, item 3),
  // and a hint that BRANCHES on whether a decision already exists this week
  // (fix round 3, item 2) — "or the advisor hasn't run yet" reads as a
  // contradiction sitting right above an ACCEPTED row.
  const noOpenState = (
    <PanelState
      kind="empty"
      message="No open recommendations this week"
      hint={
        decided.length > 0
          ? "Nothing left to act on — this week's decision is recorded below."
          : "Every rule evaluated to a hold, or the advisor hasn't run yet — see “Where we stand” for the full audit trail."
      }
    />
  );

  if (noOpen && decided.length === 0 && nearMiss.length === 0) {
    return noOpenState;
  }

  return (
    <div>
      {refused.map((r) => (
        <RefusalBanner key={r.id} row={applyLocal(r)} />
      ))}
      {proposed.map((r) => (
        <RecommendationCard
          key={r.id}
          row={applyLocal(r)}
          demoMode={demoMode}
          onActed={onActed}
          onRefetch={onRefetch}
          justRecorded={locally[r.id] !== undefined}
        />
      ))}
      {noOpen && noOpenState}
      {decided.length > 0 && (
        <>
          <div style={{ marginTop: 10, fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.05em" }}>
            DECIDED THIS WEEK
          </div>
          {decided.map((r, i) => {
            const decision = decisionByKey.get(decisionJoinKey(r.rule_id, evalSymbol(r)));
            return (
              <DecidedRow
                key={i}
                row={r}
                decision={decision}
                demoMode={demoMode}
                onActed={onActed}
                onRefetch={onRefetch}
                justRecorded={decision !== undefined && locally[decision.id] !== undefined}
              />
            );
          })}
        </>
      )}
      {nearMiss.length > 0 && (
        <>
          <div style={{ marginTop: 10, fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.05em" }}>
            NEAR MISS — evaluated, did not fire
          </div>
          {nearMiss.map((r, i) => (
            <NearMissRow key={i} row={r} />
          ))}
        </>
      )}
    </div>
  );
}
