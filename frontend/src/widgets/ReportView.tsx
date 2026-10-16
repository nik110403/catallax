// Weekly Advisor report view — the document. A weekly report
// is a DOCUMENT with a reading order, not four loose panels
//: this widget owns all four
// sections (What happened / Where we stand / What to do next / Scorecard)
// in order, one scroll. "What to do next" and "Scorecard" reuse the SAME
// presentational components the registered AdvisorScorecard widget uses
// (`ScorecardBody`) and the row-component module `RecommendationRow.tsx`
// exports (`RecommendationList`) — one render path for the arithmetic and
// honesty labels, never a second copy that could drift.
import { useEffect, useState } from "react";

import { api, getSession, reportPdfUrl } from "../api/client";
import { isPackAbsent } from "../api/types";
import type { RuleEvaluation } from "../api/types";
import { theme } from "../theme";
import { ScorecardBody } from "./AdvisorScorecard";
import { controlStyle } from "./chartTheme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import { RecommendationList } from "./RecommendationRow";
import type { LocalOutcome } from "./RecommendationRow";
import { ReportPackChapters } from "./ReportPackChapters";
import type { WidgetProps } from "./registry";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";
import { ACTION_COLOR, DEFAULT_REPORT_CONFIG, StatusBadge, WeekControl, eur, noReportCopy, qty, type ReportWidgetConfig, useDecisions, usePack, useReport } from "./reportsShared";

// A "passing" evaluation: an in-band hold with NO `arithmetic.material` key
// at all (the rule never even computed a trim candidate for it) — collapsed
// to a single count line (fix round P1a) rather than a long run of near-identical
// "band OK" rows burying the week's one real event. Anything else (a
// breach, a refusal, a decided row, or a hold whose `arithmetic.material`
// is present at all — near-miss or gate/cooldown-blocked) is an exception
// and gets its own row.
function isPassingHold(r: RuleEvaluation): boolean {
  return r.action === "hold" && r.status === "evaluated" && r.arithmetic?.material === undefined;
}

function SectionHeading({ children }: { children: React.ReactNode }) {
  return (
    <h3
      style={{
        margin: "0 0 6px",
        fontSize: theme.fontSize.sm,
        color: theme.text,
        letterSpacing: "0.04em",
        textTransform: "uppercase",
        borderBottom: `1px solid ${theme.border}`,
        paddingBottom: 4,
      }}
    >
      {children}
    </h3>
  );
}

function RuleEvalRow({ row }: { row: RuleEvaluation }) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 8,
        padding: "2px 0",
        borderBottom: `1px solid ${theme.grid}`,
        fontSize: theme.fontSize.micro,
      }}
    >
      <span style={{ width: 130, flexShrink: 0, color: theme.text, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
        {row.rule_id}
      </span>
      <span className="num" style={{ width: 48, flexShrink: 0, color: ACTION_COLOR[row.action] ?? theme.muted, textTransform: "uppercase" }}>
        {row.action}
      </span>
      <span style={{ flexShrink: 0 }}>
        <StatusBadge status={row.status} />
      </span>
      <span style={{ color: theme.muted, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }} title={row.note ?? undefined}>
        {row.note}
      </span>
    </div>
  );
}

// Narration is preamble, never the body — a report is complete
// without it. Absent narration renders as ONE compact agate line,
// never a full empty-state block, and the two absence reasons render
// DIFFERENTLY: "not configured" is a stable product state
// (muted, no hint); a failed probe is a transient fault (warn colour + a
// "retries automatically" hint). The raw backend string never reaches the
// screen — "see backend logs" is rewritten for a reader.
function narrationAbsence(reason: string | null): { message: string; hint?: string; tone: "muted" | "warn" } {
  const r = (reason ?? "").toLowerCase();
  if (r.includes("probe failed")) {
    return { message: "AI narration failed this run", hint: "transient — retries automatically on next week's report", tone: "warn" };
  }
  if (r.includes("not configured")) {
    return { message: "AI narration isn't enabled for this deployment", tone: "muted" };
  }
  return { message: "No narration was recorded for this report", tone: "muted" };
}

// Preferred source is the PACK's exec-summary narration:
// `report.narration` is fed only from the advisor-report path, which is
// un-built by design (the backend runs the noop provider there), so a
// deployment with the pack's narration live and narrated saw the absence
// line ("AI narration isn't enabled for this deployment") beside real
// narrated prose in the very same document. Fallback order is pack →
// advisor report → the absence line — never skip straight past a live pack
// narration to the un-built advisor path. The pack branch gets its own
// micro caption ("From the weekly report pack") rather than reusing the
// advisor branch's grounding/model line — this is provenance honesty, not
// decoration: the two are different fields off different code paths and
// must never look interchangeable.
// P3b: the pack-narration and advisor-narration
// branches below rendered an IDENTICAL six-property inline style object
// (family/size/color/line-height/whitespace/margin) — one shared const so
// the two can never silently drift apart.
const NARRATION_BODY_STYLE: React.CSSProperties = {
  fontFamily: theme.fontSerif,
  fontSize: theme.fontSize.base,
  color: theme.text,
  lineHeight: 1.5,
  whiteSpace: "pre-line",
  marginBottom: 4,
};

// P2e: while the pack fetch is still in flight,
// `NarrationPreamble` renders this placeholder instead of `null` — a
// reserved, empty box roughly the footprint of the absence line it may
// resolve to, so "What happened" below doesn't visibly jump once the fetch
// settles. `marginBottom: 4` matches every real branch's own trailing
// margin so the reserved slot's total height matches too.
const PREAMBLE_PLACEHOLDER_STYLE: React.CSSProperties = {
  minHeight: Math.ceil(theme.fontSize.micro * 1.4),
  marginBottom: 4,
};

// P2f: a small-caps provenance LABEL, distinct
// from the advisor branch's plain grounding/model caption below — the two
// come off different code paths (pack vs. advisor narration) and must read
// as visibly different kinds of caption, not interchangeable. No `num`
// class here: that class is for tabular-numeral alignment, and this
// caption never contains a digit.
const PACK_PROVENANCE_LABEL_STYLE: React.CSSProperties = {
  fontSize: theme.fontSize.micro,
  color: theme.muted,
  marginTop: 4,
  textTransform: "uppercase",
  letterSpacing: "0.06em",
};

function NarrationPreamble({ narrated, narration, narrationAbsentReason, packNarration, packSettled }: {
  narrated: boolean;
  narration: { text: string; grounding: string; model: string } | null;
  narrationAbsentReason: string | null;
  packNarration: string | null;
  packSettled: boolean;
}) {
  if (packNarration) {
    return (
      <div style={NARRATION_BODY_STYLE}>
        {packNarration}
        <div style={PACK_PROVENANCE_LABEL_STYLE}>From the weekly report pack</div>
      </div>
    );
  }
  // P2d: moved ABOVE the advisor-narration branch.
  // While the pack fetch is still in flight we don't yet know whether it
  // will supply narration, which outranks the advisor's (see the module
  // comment above `NarrationPreamble` on fallback order) — rendering the
  // advisor narration now and then swapping to the pack's the instant it
  // resolves is a worse reading experience than a brief, deliberate wait.
  if (!packSettled) return <div style={PREAMBLE_PLACEHOLDER_STYLE} />;
  if (narrated && narration) {
    return (
      <div style={NARRATION_BODY_STYLE}>
        {narration.text}
        <div className="num" style={{ fontSize: theme.fontSize.micro, color: theme.muted, marginTop: 4 }}>
          {narration.grounding} · {narration.model}
        </div>
      </div>
    );
  }
  // Neither source has narration to show, and the pack has definitely
  // settled (the guard above already returned for the in-flight case).
  const abs = narrationAbsence(narrationAbsentReason);
  const color = abs.tone === "warn" ? theme.warn : theme.muted;
  return (
    <div style={{ fontSize: theme.fontSize.micro, color, marginBottom: 4 }}>
      {abs.message}
      {abs.hint ? ` — ${abs.hint}` : ""}
    </div>
  );
}

function ReportView({ config, onConfigChange }: WidgetProps<ReportWidgetConfig>) {
  const reportWeek = config.reportWeek ?? "latest";
  const [refreshKey, setRefreshKey] = useState(0);
  const [locally, setLocally] = useState<Record<number, LocalOutcome>>({});
  const { data: report, loading, error, errorCode } = useReport(reportWeek, refreshKey);
  // The report PACK — an independent, additive fetch: a 404
  // here (pre-pack week) is expected and must leave today's advisor-only
  // rendering byte-identical, never an error state of its own.
  const { data: pack, loading: packLoading, error: packError, errorCode: packErrorCode } = usePack(reportWeek, refreshKey);
  const packNotFound = packErrorCode === "NOT_FOUND";
  // The decisions projection — additive like the pack fetch
  // above: a 404 here (pre-Phase-18 week) must leave "DECIDED THIS WEEK"
  // rendering exactly as it did before (no Execute sub-form, same as today)
  // rather than surfacing an error state of its own. This is the ONLY
  // source of the current LEAF recommendation id for an accepted/modified
  // row — the FROZEN advisor_report.py
  // `rule_evaluations` projection never carried one.
  const { data: decisions } = useDecisions(reportWeek, refreshKey);
  // Whether the pack fetch has settled to a definite state (success, error,
  // or a confirmed 404) — shared by the narration preamble's guard above and
  // the pack appendix's own loading/absent guard below, so the two never
  // disagree about what "still deciding" means.
  const packSettled = !!pack || !!packError || packNotFound;
  const packExecSummary = pack && !isPackAbsent(pack.sections.exec_summary) ? pack.sections.exec_summary : null;
  const packNarration =
    packExecSummary && typeof packExecSummary.narration === "string" && packExecSummary.narration.trim().length > 0
      ? packExecSummary.narration
      : null;
  // `/reports/weeks`' additive `availability` as an early, authoritative
  // hint for whether THIS week even has a pack — fix round (coordinator):
  // without it, a pre-pack week briefly painted a "Report Pack" heading +
  // loading skeleton before its own `/pack` 404 settled. Once we know
  // `availability[week].pack === false`, hide the section immediately
  // rather than waiting on the round-trip; when the hint is unknown (not
  // loaded yet, or this exact week isn't in the list), render NOTHING until
  // the pack fetch itself has settled — never a heading/skeleton on a guess.
  const { data: weeksData } = useAsync(() => api.reportWeeks(), []);
  const { data: session } = useAsync(() => getSession(), []);
  const notFound = errorCode === "NOT_FOUND";
  const refetch = () => setRefreshKey((k) => k + 1);
  // A refetch means the backend's own rule_evaluations is the freshest
  // truth available — drop any stale session-local status override so a
  // row never shows an outcome the last real read disagrees with. Keyed on
  // the ARRIVED `report` payload, not on
  // `refreshKey` — `refreshKey` increments the instant a refetch is
  // requested, well before the new data lands, so clearing on it wiped the
  // optimistic outcome immediately and the card reverted to its pre-action
  // state for the whole round-trip, then teleported to the final state the
  // moment the refetch resolved. Clearing on `report` instead means the
  // local override survives exactly until the real data that supersedes it
  // is in hand — no revert-then-teleport, and no window with neither.
  // fix-first #15: keyed on `[report, decisions]`, not `report` alone —
  // a refetch clears the optimistic local override the instant the
  // advisor_report projection lands, but `/decisions` (the ONLY source of
  // whether a row is still executable — see DecidedRow's own `executable`
  // gate below) can arrive on its own later tick. In that window `locally`
  // was already cleared while `decisions` still held the pre-refetch
  // accepted/modified row, so RECORD EXECUTION flashed back into view on an
  // already-executed row until `decisions` itself caught up.
  useEffect(() => setLocally({}), [report, decisions]);

  const allEvals = report?.where_we_stand.rule_evaluations ?? [];
  const passingCount = allEvals.filter(isPassingHold).length;
  // Phase 18: the lifecycle now reaches modified/executed/expired/
  // superseded too (`recommendation_lifecycle.TRANSITIONS`) — every one of
  // those is a chain leaf with nothing left to decide, same as
  // accepted/overridden, so it belongs in "DECIDED THIS WEEK" (DecidedRow
  // renders expired/superseded as muted chips via STATUS_COLOR's fallback).
  const DECIDED_STATUSES = new Set(["accepted", "overridden", "modified", "executed", "expired", "superseded"]);
  const isDecided = (r: RuleEvaluation): boolean => DECIDED_STATUSES.has(r.status);
  // Near-miss (committed fde86ce/82759ab): `arithmetic.material === false`
  // exactly — a hold that crossed its band but was too small to matter.
  // This week's report may legitimately have zero of these; a plain
  // in-band hold never stamps `material` at all, so it can't collide here.
  const isNearMiss = (r: RuleEvaluation): boolean => r.status === "evaluated" && r.arithmetic?.material === false;
  // Decided AND near-miss rows each get their OWN section below ("DECIDED
  // THIS WEEK" / "NEAR MISS", inside "What to do next") — excluding both
  // here stops the same row rendering twice (fix round 2, P2 + its latent
  // near-miss sibling): once as a "Where we stand" exception and again
  // under its own section.
  const exceptions = allEvals.filter((r) => !isPassingHold(r) && !isDecided(r) && !isNearMiss(r));
  const nearMiss = allEvals.filter(isNearMiss);
  // Decided (fix round P2a): a chain leaf reached via accept/override,
  // sourced from rule_evaluations — NOT from what_to_do_next.proposed
  // (which stops carrying a row the instant it's no longer "proposed").
  const decided = allEvals.filter(isDecided);

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead deck={deckLine("report-view")} agate={report && <span>report week {report.report_week}</span>}>
        <WeekControl reportWeek={reportWeek} onChange={(w) => { onConfigChange({ reportWeek: w }); refetch(); }} />
        {/* Report pack PDF download — hidden entirely when
            `pdf:false` (pre-pack week, no worker-rendered PDF yet, or demo
            mode where the route always 404s) rather than a disabled button. */}
        {/* Fix round 3 (small item): dropped the hardcoded 2,500ms
            `setTimeout` "Downloading…" state — it fired identically on
            success, a 404, and instant completion, fabricating a progress
            signal on a surface whose whole thesis is epistemic honesty, and
            left an uncleared timer on unmount. The `download` attribute
            alone is the honest version: it doesn't claim to know anything
            about the transfer's outcome. */}
        {pack?.pdf && (
          <a
            href={reportPdfUrl(report?.report_week ?? reportWeek)}
            download={`catallax-weekly-${report?.report_week ?? reportWeek}.pdf`}
            style={{
              ...controlStyle,
              minHeight: 24,
              display: "inline-flex",
              alignItems: "center",
              textDecoration: "none",
              cursor: "pointer",
            }}
          >
            Download PDF
          </a>
        )}
      </PanelHead>

      {loading && !report && <PanelState kind="loading" skeleton="table" fill />}
      {error && !report && !notFound && <PanelState kind="error" message={error} onRetry={refetch} fill />}
      {error && !report && notFound && <PanelState kind="empty" {...noReportCopy(session?.demo_mode ?? false, reportWeek)} fill />}

      {report && (
        // One scroll for the whole document
        // direction / P1b) — no nested scroll regions inside; a long list
        // (positions, rule audit rows) just makes the page longer.
        <div tabIndex={0} style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "10px", display: "flex", flexDirection: "column", gap: 16 }}>
          <NarrationPreamble
            narrated={report.narrated}
            narration={report.narration}
            narrationAbsentReason={report.narration_absent_reason}
            packNarration={packNarration}
            packSettled={packSettled}
          />

          {/* Phase 18: a one-line nudge into the pack's Forecasts
              chapter — visible as soon as the pack is loaded, well above the
              appendix itself (no "scroll down to find out if anything's
              open" state). Absent entirely when the chapter hasn't shipped
              (pre-Phase-18 pack, demo mode — no demo seam for the ledger, or
              a failed issuance) or has nothing pending. */}
          {(() => {
            const fc = pack?.sections.forecasts;
            const forecasts = fc && !isPackAbsent(fc) ? fc : null;
            if (!forecasts || forecasts.pending_total === 0) return null;
            return (
              // P2:
              // was an unframed 11px muted line sitting in the uniform
              // gap:16 stack — read as a caption, not the fact it actually
              // is. Framed with the same left rule + `sm` size the pack's
              // own Forecasts chapter hero uses for this exact figure
              // (ReportPackChapters.tsx's `ForecastsBody`), so the nudge and
              // the chapter it links to state pending consistently.
              <div
                style={{
                  fontSize: theme.fontSize.sm,
                  fontWeight: 700,
                  color: theme.accent,
                  borderLeft: `2px solid ${theme.border}`,
                  paddingLeft: 10,
                }}
              >
                <a
                  href="#pack-chapter-forecasts"
                  onClick={(e) => {
                    e.preventDefault();
                    const el = document.getElementById("pack-chapter-forecasts");
                    // P1: `preventDefault` means
                    // the browser's own hash-navigation focus move never
                    // happens, so a keyboard/screen-reader user who activates
                    // this link lands nowhere — focus the chapter's own
                    // `<summary>` (the thing that just became the visible
                    // target) once it's open, not just its scroll position.
                    if (el instanceof HTMLDetailsElement) {
                      el.open = true;
                      // P2: `focus()` defaults
                      // to `preventScroll: false`, so the browser jumped the
                      // summary into view BEFORE `scrollIntoView` below ran —
                      // the `smooth` branch then animated from an
                      // already-arrived position, so every user got the
                      // instant jump regardless of `prefers-reduced-motion`.
                      // `preventScroll: true` leaves the one deliberate move
                      // to `scrollIntoView`, which the reduced-motion gate
                      // actually governs.
                      el.querySelector("summary")?.focus({ preventScroll: true });
                    }
                    const reduceMotion =
                      typeof window !== "undefined" &&
                      typeof window.matchMedia === "function" &&
                      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
                    el?.scrollIntoView({ behavior: reduceMotion ? "auto" : "smooth", block: "start" });
                  }}
                  style={{ color: "inherit", textDecoration: "none" }}
                >
                  {forecasts.pending_total} forecast{forecasts.pending_total === 1 ? "" : "s"} pending
                  {forecasts.next_due ? ` · next due ${forecasts.next_due}` : ""} →
                </a>
              </div>
            );
          })()}

          <div>
            <SectionHeading>What happened</SectionHeading>
            {/* One agate line, not three hero tiles (fix round P2c) — and
                REGIME is omitted entirely when null, never a hero-sized
                bare dash. */}
            <div className="num" style={{ fontSize: theme.fontSize.sm, color: theme.text, marginBottom: 8 }}>
              NAV {eur(report.what_happened.nav_eur)} · CASH {eur(report.what_happened.cash_eur)}
              {report.what_happened.regime_state && <> · regime {report.what_happened.regime_state}</>}
            </div>
            {report.what_happened.positions.length === 0 ? (
              <PanelState kind="empty" message="No held positions this week" />
            ) : (
              // The full position inventory can be a long run of rows — enough to
              // own the entire first screen of a document whose job is
              // decisions, not a holdings ledger (fix round 2, P1). Section
              // ORDER stays fixed (What happened / Where we stand / What to
              // do next / Scorecard, per
              // direction), so the fix is rank+truncate here: the top 10 by
              // value render inline, the rest sit one click away.
              (() => {
                const sorted = [...report.what_happened.positions].sort((a, b) => b.value_eur - a.value_eur);
                const head = sorted.slice(0, 10);
                const rest = sorted.slice(10);
                const row = (p: (typeof sorted)[number]) => (
                  <div
                    key={p.symbol}
                    style={{
                      display: "grid",
                      gridTemplateColumns: "1fr 130px 100px",
                      gap: 8,
                      padding: "2px 8px",
                      fontSize: theme.fontSize.micro,
                      borderBottom: `1px solid ${theme.grid}`,
                    }}
                  >
                    <span style={{ color: theme.text }}>{p.symbol}</span>
                    <span className="num" style={{ color: theme.muted, textAlign: "right" }}>{qty(p.qty)}</span>
                    <span className="num" style={{ color: theme.text, textAlign: "right" }}>{eur(p.value_eur)}</span>
                  </div>
                );
                return (
                  <div style={{ border: `1px solid ${theme.grid}`, borderRadius: 4 }}>
                    {head.map(row)}
                    {rest.length > 0 && (
                      <details>
                        <summary style={{ padding: "4px 8px", fontSize: theme.fontSize.micro, color: theme.muted, cursor: "pointer" }}>
                          {rest.length} more position{rest.length === 1 ? "" : "s"} (by value)
                        </summary>
                        {rest.map(row)}
                      </details>
                    )}
                  </div>
                );
              })()
            )}
          </div>

          <div>
            <SectionHeading>Where we stand</SectionHeading>
            {allEvals.length === 0 ? (
              <PanelState kind="empty" message="No rule evaluations recorded for this week" />
            ) : (
              <div>
                {/* Passing holds collapse to one count line (fix round P1a)
                    — the week's real events (breaches, refusals, decisions,
                    near-misses) are what's worth a reader's attention, not
                    50-odd repeats of "band OK". Decided rows are counted
                    here too (fix round 2, P2) rather than left implicit —
                    their own row lives below, in "DECIDED THIS WEEK". */}
                {(passingCount > 0 || decided.length > 0) && (
                  <div style={{ padding: "4px 0", fontSize: theme.fontSize.micro, color: theme.muted }}>
                    {passingCount} within band{passingCount === 1 ? "" : "s"}
                    {decided.length > 0 && ` · ${decided.length} decided (below)`}
                  </div>
                )}
                {exceptions.map((r, i) => (
                  <RuleEvalRow key={i} row={r} />
                ))}
                {exceptions.length === 0 && passingCount === 0 && decided.length === 0 && (
                  <PanelState kind="empty" message="No rule evaluations recorded for this week" />
                )}
              </div>
            )}
          </div>

          <div>
            <SectionHeading>What to do next</SectionHeading>
            <RecommendationList
              proposed={report.what_to_do_next.proposed}
              refused={report.what_to_do_next.refused}
              decided={decided}
              nearMiss={nearMiss}
              decisions={decisions?.rows ?? []}
              demoMode={session?.demo_mode ?? false}
              onActed={(id, outcome) => setLocally((m) => ({ ...m, [id]: outcome }))}
              onRefetch={refetch}
              locally={locally}
            />
          </div>

          <div>
            <SectionHeading>Scorecard</SectionHeading>
            <ScorecardBody scorecard={report.scorecard} />
          </div>

          {/* Report pack — an additive appendix, never shown
              for a pre-pack week: today's advisor-only rendering above stays
              byte-identical in that case. `packDefinitelyAbsent` short-
              circuits the instant `availability` says so (no round-trip
              wait); otherwise nothing paints — no heading, no skeleton —
              until either the availability hint turns positive or the pack
              fetch itself has settled (fix round: previously painted the
              heading + a loading skeleton for every pre-pack week too,
              during the brief window before its own 404 resolved). */}
          {(() => {
            const availHint = report ? weeksData?.availability?.[report.report_week] : undefined;
            const packDefinitelyAbsent = packNotFound || availHint?.pack === false;
            if (packDefinitelyAbsent || !(packSettled || availHint?.pack === true)) return null;
            return (
              <div>
                <SectionHeading>Report Pack</SectionHeading>
                {packLoading && !pack && <PanelState kind="loading" skeleton="table" />}
                {packError && !pack && !packNotFound && <PanelState kind="error" message={packError} onRetry={refetch} />}
                {pack && <ReportPackChapters pack={pack} />}
              </div>
            );
          })()}
        </div>
      )}
    </div>
  );
}

registerWidget<ReportWidgetConfig>({
  id: "report-view",
  title: "Weekly Report",
  component: ReportView,
  defaultSize: { w: 8, h: 30 },
  minSize: { w: 6, h: 14 },
  defaultConfig: DEFAULT_REPORT_CONFIG,
  category: "Reports",
  description: "The full weekly advisor report — what happened, where we stand, what to do next, and the scorecard",
});
