// Weekly Advisor scorecard (Phase 16; renamed from an earlier draft's
// "Scorecard.tsx" — collides with the existing research `ScoreCard.tsx`
// widget,. Two parts: the rule-evaluation counters
// (ONE agate line, not a tile row — "the named enemy: KPI-tile-itis", eight
// loose scalars must never become eight boxed stats) and the two-arm
// actual-vs-do-nothing performance scorecard (`services/scorecard`).
// Every honesty label on the latter — convention, n_effective, depth_weeks,
// p_floor_two_sided, arms_coincide — lives INSIDE the same bordered group as
// the TWR figures it qualifies: never a tooltip, never a separate widget,
// never a scroll boundary away from the number it explains.
import { useState } from "react";

import type { ReportScorecard } from "../api/types";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import type { WidgetProps } from "./registry";
import { registerWidget } from "./registry";
import {
  DEFAULT_REPORT_CONFIG,
  PerformanceBlock,
  WeekControl,
  noReportCopy,
  pctFrac,
  type ReportWidgetConfig,
  useReport,
} from "./reportsShared";
import { getSession } from "../api/client";
import { useAsync } from "./useAsync";

// Reused verbatim by ReportView's own Scorecard section — one render path
// for the honesty labels, not a second copy that could drift.
export function ScorecardBody({ scorecard }: { scorecard: ReportScorecard }) {
  const perf = scorecard.performance;
  const ruleEntries = Object.entries(scorecard.rule_status);

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 10 }}>
      {/* Counters: one agate line, deliberately not three boxed tiles. */}
      <div className="num" style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>
        {scorecard.n_evaluated} evaluated ·{" "}
        <span style={{ color: scorecard.n_proposed > 0 ? theme.accent : theme.muted }}>{scorecard.n_proposed} proposed</span> ·{" "}
        <span style={{ color: scorecard.n_refused > 0 ? theme.down : theme.muted }}>{scorecard.n_refused} refused</span>
      </div>
      <div style={{ display: "flex", flexWrap: "wrap", gap: "2px 14px" }}>
        {ruleEntries.map(([id, status]) => (
          <span key={id} style={{ fontSize: theme.fontSize.micro }}>
            <span style={{ color: theme.text }}>{id}</span>{" "}
            <span style={{ color: status === "active" ? theme.up : theme.muted }}>{status}</span>
          </span>
        ))}
      </div>

      {/* One bordered group: figures + their honesty qualifiers, together
          (extracted into the shared `PerformanceBlock` so the pack's
          portfolio chapter — same `compute_scorecard()` output, no
          surrounding `ReportScorecard` counters — renders it identically
          rather than forking a second copy). */}
      <PerformanceBlock perf={perf} />
    </div>
  );
}

function AdvisorScorecard({ config, onConfigChange }: WidgetProps<ReportWidgetConfig>) {
  const reportWeek = config.reportWeek ?? "latest";
  const [refreshKey, setRefreshKey] = useState(0);
  const { data: report, loading, error, errorCode } = useReport(reportWeek, refreshKey);
  const { data: session } = useAsync(() => getSession(), []);
  const perf = report?.scorecard.performance;
  const notFound = errorCode === "NOT_FOUND";

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        deck={deckLine("advisor-scorecard")}
        figure={perf?.actual_twr !== null && perf?.actual_twr !== undefined ? pctFrac(perf.actual_twr) : undefined}
        figureColor={perf?.actual_twr !== null && perf?.actual_twr !== undefined ? (perf.actual_twr >= 0 ? theme.up : theme.down) : undefined}
        unit={perf?.actual_twr !== null && perf?.actual_twr !== undefined ? "actual TWR" : undefined}
        agate={report && <span>report week {report.report_week}</span>}
      >
        <WeekControl reportWeek={reportWeek} onChange={(w) => { onConfigChange({ reportWeek: w }); setRefreshKey((k) => k + 1); }} />
      </PanelHead>
      {loading && !report && <PanelState kind="loading" fill />}
      {error && !report && !notFound && <PanelState kind="error" message={error} onRetry={() => setRefreshKey((k) => k + 1)} fill />}
      {error && !report && notFound && (
        <PanelState kind="empty" {...noReportCopy(session?.demo_mode ?? false, reportWeek)} fill />
      )}
      {report && (
        <div tabIndex={0} style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "8px" }}>
          <ScorecardBody scorecard={report.scorecard} />
        </div>
      )}
    </div>
  );
}

registerWidget<ReportWidgetConfig>({
  id: "advisor-scorecard",
  title: "Advisor Scorecard",
  component: AdvisorScorecard,
  defaultSize: { w: 6, h: 14 },
  minSize: { w: 4, h: 8 },
  defaultConfig: DEFAULT_REPORT_CONFIG,
  category: "Reports",
  description: "Actual vs. do-nothing performance, printed with its N, depth and p-floor",
});
