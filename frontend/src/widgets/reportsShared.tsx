// Shared plumbing for the Weekly Advisor report surface:
// ReportView (the registered document widget), AdvisorScorecard and
// ReportArchive (registered), and RecommendationRow (a row COMPONENT used
// inside ReportView — deliberately NOT a registered widget; a weekly report
// is a document with one reading order, not four loose panels. One fetch hook
// + one set of formatters so every consumer reads the SAME report shape the
// same way instead of drifting independently.
import { useEffect, useId, useState } from "react";

import { api } from "../api/client";
import type { DecisionRow, Recommendation, RuleEvaluation, ScorecardPerformance } from "../api/types";
import { LOCALE } from "../format";
import { theme } from "../theme";
import { controlStyle } from "./chartTheme";
import { PanelState } from "./PanelState";
import { useAsync } from "./useAsync";

// Every report-consuming widget's config is exactly this — a report-week
// selector, "latest" being the sentinel for the newest available week
// (mirrors reports.py's own /latest route rather than re-deriving "latest"
// client-side from /reports/weeks).
export interface ReportWidgetConfig {
  reportWeek: string;
}
export const DEFAULT_REPORT_CONFIG: ReportWidgetConfig = { reportWeek: "latest" };

// `refreshKey` lets a caller force a refetch (e.g. right after an accept/
// override transition) without re-plumbing useAsync's dep array per call site.
export function useReport(reportWeek: string, refreshKey = 0) {
  const week = reportWeek?.trim() || "latest";
  return useAsync(() => (week === "latest" ? api.reportLatest() : api.report(week)), [week, refreshKey]);
}

// The report PACK — a separate fetch from `useReport` above
// (different route, and a 404 here is an expected "no pack for this week"
// state rather than "no report at all"). `reportWeek` must already be
// resolved to a real week (not "latest") by the caller when it wants a pack
// pinned to the same week the plain report resolved to — `latest` IS still
// accepted (the pack route mirrors the report route's own "latest" sentinel)
// but the two independent "latest" resolutions are not guaranteed to land on
// the same week if a new report lands between the two fetches; callers that
// care pin to `report.report_week` once the plain report has resolved.
export function usePack(reportWeek: string, refreshKey = 0) {
  const week = reportWeek?.trim() || "latest";
  return useAsync(() => api.reportPack(week), [week, refreshKey]);
}

// The decisions projection (Phase 18 route) — a separate
// fetch from `useReport`/`usePack` above, same "latest" sentinel convention
// and same "404 is an expected pre-Phase-18 state" contract as `usePack`.
// This is the only source that carries the current LEAF recommendation
// `id` for an already-decided (`accepted`/`modified`) row — the FROZEN
// `advisor_report.py` `rule_evaluations` projection never did (design review
// fix).
export function useDecisions(reportWeek: string, refreshKey = 0) {
  const week = reportWeek?.trim() || "latest";
  return useAsync(() => api.reportDecisions(week), [week, refreshKey]);
}

// ── formatting ───────────────────────────────────────────────────────────
export const eur = (v: number | null | undefined, dp = 2): string =>
  v === null || v === undefined
    ? "—"
    : `€${v.toLocaleString(LOCALE, { minimumFractionDigits: dp, maximumFractionDigits: dp })}`;

// TWR figures are stored as fractions (0.0123 = 1.23%) — the ONE place that
// convention gets scaled for display.
export const pctFrac = (v: number | null | undefined, dp = 2): string =>
  v === null || v === undefined
    ? "—"
    : `${(v * 100).toLocaleString(LOCALE, { minimumFractionDigits: dp, maximumFractionDigits: dp })}%`;

// Position quantities (0.5 BTC, 1000.12345678 ABC, …) — capped to a
// fixed 6 decimals with thousands separators (fix round P2b) so a column of
// them right-aligns cleanly instead of ragged raw-float strings of varying
// length.
// Fixed (not just capped) fraction digits — the one thing that makes
// right-aligned decimal quantities actually line up at the decimal point
// rather than at the ones digit (fix round 2, item 5): a bare
// `maximumFractionDigits` still lets "0.5" and "1,000.12345" ragged
// past each other since their fractional lengths differ.
export const qty = (v: number | null | undefined, dp = 6): string =>
  v === null || v === undefined ? "—" : v.toLocaleString(LOCALE, { minimumFractionDigits: dp, maximumFractionDigits: dp });

// Raw arithmetic disclosure, deliberately un-opinionated: `input_snapshot`/
// `tax_cost_arithmetic` mix 0–1 fractions and already-scaled 0–100 percents
// under near-identical `*_pct` names across the three rules (weight bands
// are fractions; the sell-gate's CVaR figures are already 0–100) — there is
// no reliable naming convention to auto-detect which is which, so this
// prints the number exactly as computed rather than guessing a `×100`.
export function fmtValue(v: unknown): string {
  if (v === null || v === undefined) return "—";
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "number") return v.toLocaleString(LOCALE, { maximumFractionDigits: 4 });
  if (Array.isArray(v)) {
    if (v.length === 0) return "—";
    if (v.every((x) => x && typeof x === "object" && "symbol" in x && "trim_value_eur" in x)) {
      return (v as { symbol: string; trim_value_eur: number }[])
        .map((leg) => `${leg.symbol} ${eur(leg.trim_value_eur)}`)
        .join(", ");
    }
    if (v.every((x) => typeof x !== "object")) return v.join(", ");
    return JSON.stringify(v);
  }
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

const labelOf = (key: string): string => key.replace(/_/g, " ");

// Only `fires` is a pass/fail ALARM boolean (red = didn't fire, green =
// fired) — every other boolean here is a disclosure flag (`material`,
// `cost_basis_available`, …), and colouring a plain "no" red misreads a
// normal disclosure as an error (fix round P3a). Callers can widen this for
// a specific alarm key if a future field needs it; default is `fires` only.
const DEFAULT_ALARM_BOOL_KEYS = new Set(["fires"]);

// Renders every key of a rule's `tax_cost_arithmetic` / `input_snapshot`
// JSONB blob as a label/value grid — the near-miss "would-fire-at"
// arithmetic (`benefit_eur` vs `threshold_eur`, `material`, `fires`, …) is
// whatever keys THAT rule happened to write, so this stays fully generic
// rather than hard-coding one rule's field set. `_eur`-suffixed numeric
// fields are the one case worth a currency-formatted exception — every
// `*_eur` field in this codebase is unambiguously EUR.
export function ArithmeticGrid({
  data,
  exclude,
  alarmKeys = DEFAULT_ALARM_BOOL_KEYS,
}: {
  data: Record<string, unknown> | null | undefined;
  exclude?: string[];
  alarmKeys?: Set<string>;
}) {
  if (!data) return null;
  const skip = new Set(exclude ?? []);
  const entries = Object.entries(data).filter(([k, v]) => !skip.has(k) && v !== null && v !== undefined);
  if (entries.length === 0) return null;
  return (
    <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fill, minmax(150px, 1fr))", gap: "4px 12px" }}>
      {entries.map(([k, v]) => {
        const isEur = k.endsWith("_eur") && typeof v === "number";
        const isBool = typeof v === "boolean";
        const boolColor = isBool && alarmKeys.has(k) ? ((v as boolean) ? theme.up : theme.down) : undefined;
        return (
          <div key={k} style={{ minWidth: 0 }}>
            <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, textTransform: "uppercase", letterSpacing: "0.03em" }}>
              {labelOf(k)}
            </div>
            <div
              className={isEur || typeof v === "number" ? "num" : undefined}
              style={{ fontSize: theme.fontSize.micro, color: boolColor ?? theme.text, wordBreak: "break-word" }}
              title={typeof v === "string" ? v : undefined}
            >
              {isEur ? eur(v as number) : fmtValue(v)}
            </div>
          </div>
        );
      })}
    </div>
  );
}

// ── status / action colour law (shared across the report widgets) ───────
export const STATUS_COLOR: Record<string, string> = {
  proposed: theme.accent,
  accepted: theme.up,
  overridden: theme.warn,
  modified: theme.accent,
  executed: theme.up,
  evaluated: theme.muted,
  // Phase 18: rotation-out states, rendered muted like `evaluated`
  // rather than left to the `?? theme.muted` fallback — explicit here so a
  // reader of this table doesn't have to re-derive the rule from the code.
  expired: theme.muted,
  superseded: theme.muted,
};
export const ACTION_COLOR: Record<string, string> = {
  trim: theme.accent,
  buy: theme.accent,
  sell: theme.accent,
  refuse: theme.down,
  hold: theme.muted,
};

// `label` (Phase 18, optional): overrides the DISPLAYED text while the
// colour still keys off the real `status` — needed for `/reject`, which
// persists as `overridden` (F1, the frozen cooldown loop reads exactly that
// string) but must read "Rejected" to the human, never the raw status a
// plain override also produces. Every other caller is unaffected (no
// `label` passed, falls back to `status` exactly as before).
export function StatusBadge({ status, label }: { status: string; label?: string }) {
  const color = STATUS_COLOR[status] ?? theme.muted;
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
      {label ?? status}
    </span>
  );
}

// ── decision join key (Phase 18 fix-first / RE-CRITIQUE, P1) — binds
// `where_we_stand.rule_evaluations` (RuleEvaluation, no id — the FROZEN
// `advisor_report.py` projection) to the `/decisions` projection's rows
// (DecisionRow, id = the current LEAF recommendation id), the only source
// that carries the id `RecommendationRow.tsx`'s `ExecuteForm` needs. MUST
// mirror the backend's OWN dedup convention EXACTLY
// (`advisor_report.py::_load_overrides`): a symbol is part of the key ONLY
// for `rule_id === "weight_band_trim"` — never inferred from
// `funding_source`'s SHAPE. `cash_floor_band`'s `funding_source` is ALSO
// `{kind:"proceeds", symbols:[...]}` (`decisions.py::_funding_source`), and
// a single-leg trim produces a one-element `symbols` array indistinguishable
// by shape alone from `weight_band_trim`'s — a shape-based guess
// (`kind==="proceeds" && symbols.length===1`, no rule_id check) was exactly
// a design-review finding: it silently reproduced the
// P0 (Execute unreachable) the whole join exists to fix, on a path with no
// test and no live data to catch it. Gate on `rule_id`, never on shape. ───
export function decisionSymbol(row: DecisionRow): string | null {
  if (row.rule_id !== "weight_band_trim") return null;
  const fs = row.funding_source;
  return fs.kind === "proceeds" && fs.symbols.length === 1 ? fs.symbols[0] : null;
}

// Mirror on the `rule_evaluations` side: `advisor_report.py::
// _rule_arithmetic()`'s own docstring states `cash_floor_band` has no
// top-level `symbol` in `input_snapshot` — only `weight_band_trim` rows ever
// carry `arithmetic.symbol`, so this already agrees with `decisionSymbol`
// above without needing its own rule_id gate.
export function evalSymbol(row: RuleEvaluation): string | null {
  return row.arithmetic?.symbol ?? null;
}

export function decisionJoinKey(ruleId: string, symbol: string | null): string {
  return symbol ? `${ruleId}:${symbol}` : ruleId;
}

// ── local-time Execute field — the
// pre-existing default read the clock in UTC (`new Date().toISOString()
// .slice(0,16)`) while `ExecuteForm`'s submit re-parsed the SAME offset-less
// string as LOCAL time (ECMA-262 date-time string grammar: no offset =
// local), so accepting the default unedited recorded a fill hours early in
// a non-UTC zone (e.g. UTC+2). Both directions now agree: the default is built
// from LOCAL date components (below), and submit's conversion reads that
// same string back as local — the interpretation it was always going to
// get regardless of what this code intended.
export function localDatetimeInputDefault(d: Date = new Date()): string {
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function localDatetimeInputToISOString(value: string): string {
  return new Date(value).toISOString();
}

// Header "jump to week" control (ScoreCard/PriceChart's symbol-input
// pattern): type a week ("2026-W33") and press Enter, or leave blank for
// the newest report. Lives inside each widget's OWN PanelHead — there is no
// cross-panel drive in this app. A visible
// "WEEK" label (not just a placeholder/aria-label) plus a datalist of every
// real report week fed from `/reports/weeks` (fix round P3c) — discoverable
// rather than requiring the exact "YYYY-Www" format to be guessed. Uses
// `useId()` so the label/datalist ids never collide when this control is
// mounted more than once on the same page (ReportView + AdvisorScorecard).
export function WeekControl({ reportWeek, onChange }: { reportWeek: string; onChange: (week: string) => void }) {
  const [input, setInput] = useState(reportWeek);
  useEffect(() => setInput(reportWeek), [reportWeek]);
  const { data } = useAsync(() => api.reportWeeks(), []);
  const weeks = data?.weeks ?? [];
  const uid = useId();
  const inputId = `report-week-${uid}`;
  const listId = `report-week-options-${uid}`;
  return (
    <form
      style={{ display: "flex", alignItems: "center", gap: 5 }}
      onSubmit={(e) => {
        e.preventDefault();
        onChange(input.trim() || "latest");
      }}
    >
      <label htmlFor={inputId} style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.04em" }}>
        WEEK
      </label>
      <input
        id={inputId}
        list={listId}
        value={input}
        onChange={(e) => setInput(e.target.value)}
        placeholder="latest"
        title='Report week, e.g. "2026-W33" — blank (or "latest") jumps to the newest report'
        style={{ ...controlStyle, width: 96, minHeight: 24 }}
      />
      <datalist id={listId}>
        <option value="latest" />
        {weeks.map((w) => (
          <option key={w} value={w} />
        ))}
      </datalist>
    </form>
  );
}

// ── "no report" copy — demo mode gets its own coherent explanation instead
// of a generic 404; a real, empty deployment
// gets a next-action line instead of a bare "no data". ─────────
export function noReportCopy(demoMode: boolean, reportWeek: string): { message: string; hint: string } {
  if (demoMode) {
    return {
      message: "Not available in demo mode",
      hint: "Weekly Advisor reports run against a live portfolio and aren't part of the sample data shown elsewhere in this demo.",
    };
  }
  return {
    message: reportWeek === "latest" || !reportWeek ? "No report has been generated yet" : `No report for week ${reportWeek}`,
    hint: "The weekly advisor tick generates one report per ISO week — check back after the next run, or pick a week from the archive.",
  };
}

// Extracts the CONVENTION string's external-cash-flow consequence clause
// (scorecard.py labels it "consequence:" verbatim, a stable anchor since
// that word is also how the module's own docstring refers to it) so it can
// render in the ALWAYS-VISIBLE agate line —
// only the FULL convention paragraph sits behind a one-click disclosure.
// Falls back to a hand-written equivalent if the anchor ever moves, rather
// than silently dropping the requirement.
export function externalFlowsClause(convention: string | null | undefined): string {
  const fallback =
    "any external contribution or withdrawal in a week is indistinguishable from investment return in these figures (ADVISOR_DCA_BASE is dormant at 0)";
  if (!convention) return fallback;
  const m = convention.match(/consequence:\s*([^)]*)\)/i);
  return m ? m[1].trim() : fallback;
}

// The two-arm (actual vs do-nothing) performance block — the bordered group
// of TWR figures + every honesty label that qualifies them (convention,
// n_effective, depth_weeks, p_floor_two_sided, arms_coincide), extracted
// from `AdvisorScorecard.tsx`'s `ScorecardBody` so the pack's portfolio
// chapter — whose `scorecard` field is the SAME `compute_scorecard()` output
// but without the surrounding `ReportScorecard` counters/rule_status —
// reuses this exact render path instead of a second copy that could drift.
// `ScorecardBody` itself now delegates to this for its own performance
// group.
export function PerformanceBlock({ perf }: { perf: ScorecardPerformance | null }) {
  return (
    <div style={{ border: `1px solid ${theme.border}`, borderRadius: 4, padding: 8, display: "flex", flexDirection: "column", gap: 6 }}>
      <span style={{ fontSize: theme.fontSize.micro, color: theme.muted, letterSpacing: "0.05em" }}>
        PERFORMANCE — ACTUAL vs. DO-NOTHING
      </span>
      {!perf ? (
        <PanelState kind="empty" message="No performance scorecard on this report" />
      ) : perf.actual_twr === null || perf.do_nothing_twr === null ? (
        <PanelState
          kind="empty"
          message={perf.empty ? "No portfolio history yet" : `Insufficient history (depth ${perf.depth_weeks} week${perf.depth_weeks === 1 ? "" : "s"})`}
          hint={
            (perf.note ?? "no note recorded") +
            (perf.empty ? " Check back once the weekly advisor tick has produced at least two snapshot weeks." : "")
          }
        />
      ) : perf.arms_coincide === true ? (
        // Both arms produced the same figure this depth — one figure, not
        // two identical stat blocks side by side.
        <>
          <div>
            <div style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>BOTH ARMS (TWR)</div>
            <div className="num" style={{ fontSize: theme.fontSize.lg, fontWeight: 700, color: perf.actual_twr >= 0 ? theme.up : theme.down }}>
              {pctFrac(perf.actual_twr)}
            </div>
          </div>
          {perf.note && <div style={{ fontSize: theme.fontSize.micro, color: theme.accent }}>{perf.note}</div>}
        </>
      ) : (
        <div style={{ display: "flex", gap: 16, flexWrap: "wrap" }}>
          <div>
            <div style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>ACTUAL TWR</div>
            <div className="num" style={{ fontSize: theme.fontSize.lg, fontWeight: 700, color: perf.actual_twr >= 0 ? theme.up : theme.down }}>
              {pctFrac(perf.actual_twr)}
            </div>
          </div>
          <div>
            <div style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>DO-NOTHING TWR</div>
            <div className="num" style={{ fontSize: theme.fontSize.lg, fontWeight: 700, color: perf.do_nothing_twr >= 0 ? theme.up : theme.down }}>
              {pctFrac(perf.do_nothing_twr)}
            </div>
          </div>
        </div>
      )}

      {/* The honesty labels — same visual group as the figures above, never
          a tooltip. */}
      {perf && (
        <div className="num" style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>
          n_effective = {perf.n_effective} · depth = {perf.depth_weeks}w · best possible p (floor, two-sided) ≥{" "}
          {perf.p_floor_two_sided === null
            ? "not established (n_effective = 0 — no sub-period has diverged yet)"
            : perf.p_floor_two_sided.toFixed(4)}
          {perf.arms_coincide !== null && <> · arms coincide: {perf.arms_coincide ? "yes" : "no"}</>}
        </div>
      )}
      {perf?.meta && (
        <div className="num" style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>
          n_actual = {perf.meta.n_actual} · n_donothing = {perf.meta.n_donothing}
        </div>
      )}
      {/* The single most important honesty copy on this panel — base size,
          not micro, so it reads as content rather than a footnote. */}
      {perf && (
        <div style={{ fontSize: theme.fontSize.base, color: theme.text, fontFamily: theme.fontSerif, lineHeight: 1.4 }}>
          {externalFlowsClause(perf.convention)}
        </div>
      )}
      {perf && (
        <details>
          <summary style={{ fontSize: theme.fontSize.micro, color: theme.muted, cursor: "pointer" }}>
            full return convention + base-composition assumption
          </summary>
          <div style={{ fontFamily: theme.fontSerif, fontSize: theme.fontSize.micro, color: theme.muted, marginTop: 4, lineHeight: 1.4 }}>
            {perf.convention}
            {perf.meta?.base_composition_assumption && (
              <div style={{ marginTop: 4 }}>{perf.meta.base_composition_assumption}</div>
            )}
          </div>
        </details>
      )}
    </div>
  );
}

export type { Recommendation };
