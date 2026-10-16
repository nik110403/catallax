// Journal Timeline. Reverse-chronological journal entries
// with a "Due for review" group pinned at the top (from `/api/v1/journal/
// due`). Machine drafts (`source:"pack_draft"`) render visually
// distinct with a "Review and sign" action that opens the SAME quick-entry
// form JournalQuickEntry.tsx renders (imported, not duplicated), pre-filled
// from the draft — signing submits it as a new entry (the documented
// path: "the UI submits the draft's fields as a new entry") and marks the
// original draft row `reviewed` via the already-merged review endpoint
// so it stops showing as due.
import { useState } from "react";

import { ApiError } from "../api/client";
import { theme } from "../theme";
import type { JournalEntry } from "./JournalQuickEntry";
import { JournalEntryForm, journalApi } from "./JournalQuickEntry";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";

const STATUS_COLOR: Record<string, string> = {
  open: theme.accent,
  reviewed: theme.up,
  superseded: theme.muted,
};

function StatusPill({ status, muted }: { status: string; muted?: boolean }) {
  const color = muted ? theme.muted : (STATUS_COLOR[status] ?? theme.muted);
  return (
    <span
      style={{
        fontSize: theme.fontSize.micro,
        color,
        border: `1px solid ${color}`,
        borderRadius: 3,
        padding: "0px 5px",
        textTransform: "uppercase",
        letterSpacing: "0.04em",
        whiteSpace: "nowrap",
      }}
    >
      {status}
    </span>
  );
}

function EntryRow({ entry, onChanged }: { entry: JournalEntry; onChanged: () => void }) {
  const isDraft = entry.source === "pack_draft";
  const [reviewing, setReviewing] = useState(false);
  const [expanded, setExpanded] = useState(false);
  // A bare try/finally swallowed a failed review POST —
  // the new entry was created (JournalEntryForm's own emit already
  // succeeded), but closing the loop on THIS draft row silently failed and
  // the disclosure closed anyway, so the draft kept showing as due with no
  // indication anything went wrong.
  const [reviewError, setReviewError] = useState<string | null>(null);

  const onSigned = async (result: { entry: JournalEntry }) => {
    // Close the loop on the ORIGINAL draft row (distinct from the newly
    // created entry `result.entry` above it) so it drops out of "due" —
    // the draft itself never carries a forecast_id, only the new
    // entry created by JournalEntryForm does.
    setReviewError(null);
    try {
      await journalApi.review(entry.id, `Signed from draft — created entry #${result.entry.id}`, "reviewed");
      setReviewing(false);
      // Only refetch on SUCCESS. Calling this
      // unconditionally from `finally` used to trigger a refetch even after
      // a failed review call; combined with the loading-state swap below,
      // the refetch unmounted this row — destroying the inline
      // `reviewError` it had just set and closing the `reviewing`
      // disclosure — right as the user needed to read the error and retry.
      onChanged();
    } catch (e) {
      setReviewError(
        e instanceof ApiError
          ? `Entry was created, but marking this draft reviewed failed: ${e.message}. It may show as due again — safe to retry.`
          : "Entry was created, but marking this draft reviewed failed. It may show as due again — safe to retry.",
      );
    }
  };

  return (
    <div
      style={{
        borderTop: `1px solid ${theme.border}`,
        padding: "8px 0",
        display: "flex",
        flexDirection: "column",
        gap: 4,
        // Was `theme.accent` — fine for the one or two
        // rows under DUE's own collapsed "N drafts" disclosure, but the
        // same accent rail on every draft row rendered individually under
        // ALL ENTRIES read as a wall of accent down the whole list. Neutral
        // divider color: this rail marks provenance, not urgency.
        ...(isDraft ? { borderLeft: `2px dashed ${theme.border}`, paddingLeft: 8 } : {}),
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
        {/* was accent-bordered like every other CTA on
            the row — with several drafts due, the whole list read as a wall
            of equal-weight accent chips. Muted: this badge is metadata
            ("where this row came from"), not a call to action. */}
        {isDraft && (
          <span style={{ fontSize: theme.fontSize.micro, color: theme.muted, border: `1px solid ${theme.border}`, borderRadius: 3, padding: "0px 5px", textTransform: "uppercase", letterSpacing: "0.04em" }}>
            draft
          </span>
        )}
        {/* a draft's status pill was still full accent
            (STATUS_COLOR.open) — a draft is essentially always "open" until
            reviewed, so this read as the same accent-chip wall as the rail
            above. Muted for drafts; a human-authored entry's status pill
            keeps its real color. */}
        <StatusPill status={entry.status} muted={isDraft} />
        <span style={{ fontSize: theme.fontSize.sm, color: theme.text, fontWeight: 600 }}>{entry.subject}</span>
        <span className="num" style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>
          {Math.round(entry.probability * 100)}%
        </span>
        <span className="num" style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>
          review {entry.review_date}
        </span>
      </div>
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        style={{ alignSelf: "flex-start", background: "transparent", border: "none", color: theme.muted, fontSize: theme.fontSize.micro, cursor: "pointer", padding: 0, textAlign: "left" }}
      >
        {entry.title}
        {expanded ? " (less)" : " (more)"}
      </button>
      {expanded && (
        <div
          style={{
            fontFamily: theme.fontSerif,
            fontSize: theme.reading.fontSize,
            lineHeight: theme.reading.lineHeight,
            maxWidth: theme.reading.measure,
            color: theme.text,
          }}
        >
          {entry.view}
          {entry.thesis && (
            <>
              <br />
              <span style={{ color: theme.muted, fontSize: theme.fontSize.micro, fontFamily: theme.fontMono, textTransform: "uppercase", letterSpacing: "0.04em" }}>
                thesis
              </span>
              <br />
              {entry.thesis}
            </>
          )}
        </div>
      )}

      {/* secondary/muted, not accent — with N drafts due
          this was N equally-loud primary CTAs stacked on top of each other.
          The one genuinely primary action here is inside JournalEntryForm
          itself (WRITE ENTRY, once a draft is actually open for review). */}
      {isDraft && !reviewing && (
        <button
          type="button"
          onClick={() => setReviewing(true)}
          style={{ alignSelf: "flex-start", background: "transparent", color: theme.muted, border: `1px solid ${theme.border}`, borderRadius: 4, padding: "4px 10px", fontSize: theme.fontSize.micro, cursor: "pointer" }}
        >
          REVIEW AND SIGN
        </button>
      )}
      {reviewError && (
        <div role="alert" style={{ fontSize: theme.fontSize.micro, color: theme.down, background: "rgba(248,81,73,0.08)", border: `1px solid ${theme.down}`, borderRadius: 3, padding: "4px 8px" }}>
          {reviewError}
        </div>
      )}
      {isDraft && reviewing && (
        <div style={{ borderTop: `1px dashed ${theme.border}`, paddingTop: 8, marginTop: 2 }}>
          <JournalEntryForm
            autoExpand
            prefill={{
              subject: entry.subject,
              view: entry.view,
              probability: entry.probability,
              review_date: entry.review_date,
              thesis: entry.thesis,
              evidence: entry.evidence,
              invalidation: entry.invalidation,
              sizing_note: entry.sizing_note,
              confidence_note: entry.confidence_note,
            }}
            onEmitted={(result) => void onSigned(result)}
            onCancel={() => setReviewing(false)}
          />
        </div>
      )}
    </div>
  );
}

// Shared between DUE and ALL ENTRIES so a run of pack_draft
// rows always collapses into one summary disclosure instead of rendering as a
// wall of individual rows — the shape DUE already had, now also applied to
// ALL ENTRIES, which used to render its drafts individually and undid the
// muted rail/pill above by sheer repetition.
function DraftGroup({ drafts, keyPrefix, onChanged }: { drafts: JournalEntry[]; keyPrefix: string; onChanged: () => void }) {
  if (drafts.length === 0) return null;
  return (
    <details>
      <summary style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.04em", cursor: "pointer", padding: "4px 0" }}>
        {drafts.length} draft{drafts.length === 1 ? "" : "s"} to sign
      </summary>
      <div style={{ marginTop: 2 }}>
        {drafts.map((e) => (
          <EntryRow key={`${keyPrefix}-${e.id}`} entry={e} onChanged={onChanged} />
        ))}
      </div>
    </details>
  );
}

function JournalTimeline() {
  const [refreshKey, setRefreshKey] = useState(0);
  const refetch = () => setRefreshKey((k) => k + 1);
  const listState = useAsync(() => journalApi.list({ limit: 100 }), [refreshKey], 30_000);
  const dueState = useAsync(() => journalApi.due(), [refreshKey], 30_000);
  // Only show the full loading skeleton on the FIRST
  // load. A background refetch (30s poll, or `onChanged()` after a review)
  // used to swap the entire entries tree out for the skeleton the instant
  // `loading` flipped true, unmounting every `EntryRow` — losing its
  // `expanded`/`reviewing`/`reviewError` state — even though the previous
  // data was still perfectly valid to keep showing. Once data exists, a
  // refetch happens silently behind it.
  const initialLoading = (!listState.data && listState.loading) || (!dueState.data && dueState.loading);

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        figure={dueState.data ? String(dueState.data.count) : undefined}
        unit="due for review"
        deck="Every journal entry, most recent first — due reviews pinned above"
      />
      <div style={{ padding: "8px 10px", flex: 1, minHeight: 0, overflowY: "auto" }}>
        {initialLoading ? (
          <PanelState kind="loading" skeleton="table" />
        ) : listState.error && !listState.data ? (
          <PanelState kind="error" message={listState.error} onRetry={refetch} />
        ) : dueState.error && !dueState.data ? (
          <PanelState kind="error" message={dueState.error} onRetry={refetch} />
        ) : !listState.data || listState.data.entries.length === 0 ? (
          <PanelState kind="empty" message="No journal entries yet" hint="Write your first view with Journal Quick Entry." />
        ) : (
          <div>
            {dueState.data && dueState.data.entries.length > 0 && (
              <div style={{ marginBottom: 10 }}>
                <div style={{ fontSize: theme.fontSize.micro, color: theme.accent, letterSpacing: "0.05em", marginBottom: 2 }}>
                  DUE FOR REVIEW
                </div>
                {/* machine drafts (pack_draft) can arrive
                    several-per-week; each used to render as its own
                    full-weight row, "N drafts to sign" collapsed to a wall
                    of equal-priority CTAs. Human-authored due entries still
                    render individually (there's exactly one action per row:
                    expand); drafts collapse into one summary disclosure. */}
                {dueState.data.entries.filter((e) => e.source !== "pack_draft").map((e) => (
                  <EntryRow key={`due-${e.id}`} entry={e} onChanged={refetch} />
                ))}
                <DraftGroup
                  drafts={dueState.data.entries.filter((e) => e.source === "pack_draft")}
                  keyPrefix="due"
                  onChanged={refetch}
                />
              </div>
            )}
            {(() => {
              const dueIds = new Set((dueState.data?.entries ?? []).map((e) => e.id));
              const rest = listState.data.entries.filter((e) => !dueIds.has(e.id));
              return rest.length > 0 ? (
                <div>
                  {dueState.data && dueState.data.entries.length > 0 && (
                    <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.05em", marginBottom: 2 }}>
                      ALL ENTRIES
                    </div>
                  )}
                  {rest.filter((e) => e.source !== "pack_draft").map((e) => (
                    <EntryRow key={e.id} entry={e} onChanged={refetch} />
                  ))}
                  <DraftGroup drafts={rest.filter((e) => e.source === "pack_draft")} keyPrefix="rest" onChanged={refetch} />
                </div>
              ) : null;
            })()}
          </div>
        )}
      </div>
    </div>
  );
}

registerWidget({
  id: "journal-timeline",
  title: "Journal Timeline",
  component: JournalTimeline,
  defaultSize: { w: 6, h: 16 },
  minSize: { w: 4, h: 8 },
  defaultConfig: {},
  category: "Journal",
  help:
    "Every journal entry, reverse-chronological, with anything due for review pinned at the top. A machine-drafted entry (from the weekly pack) is marked draft and never counts as your words until you review and sign it — signing submits it as your own new entry and marks the draft reviewed.",
  description: "Reverse-chronological journal entries, due reviews pinned at top",
});
