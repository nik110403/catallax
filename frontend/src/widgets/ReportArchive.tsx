// Report archive: the week list -> select surface. Lists
// every week that has a report (`GET /reports/weeks`, newest first —
// already sorted server-side); selecting one opens a `report-view` panel
// pinned to that week via the same command-palette nav path "GO <SYM>"
// already uses to open a price-chart (`requestNav({kind:"add-widget", ...})`)
// — this widget never reaches into another panel's config directly, there
// is no such cross-panel channel in this app.
import { useState } from "react";

import { api, getSession } from "../api/client";
import { requestNav } from "../navigation";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import type { WidgetProps } from "./registry";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";
import { noReportCopy } from "./reportsShared";

function ReportArchive(_props: WidgetProps<Record<string, never>>) {
  const [retryKey, setRetryKey] = useState(0);
  const [opened, setOpened] = useState<string | null>(null);
  const { data, loading, error } = useAsync(() => api.reportWeeks(), [retryKey]);
  const { data: session } = useAsync(() => getSession(), []);
  const weeks = data?.weeks ?? [];

  const open = (week: string) => {
    setOpened(week);
    requestNav({ kind: "add-widget", widgetId: "report-view", config: { reportWeek: week } });
  };

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead deck={deckLine("report-archive")} agate={weeks.length > 0 && <span>{weeks.length} report{weeks.length === 1 ? "" : "s"}</span>} />
      {loading && !data && <PanelState kind="loading" skeleton="table" fill />}
      {error && !data && <PanelState kind="error" message={error} onRetry={() => setRetryKey((k) => k + 1)} fill />}
      {data && weeks.length === 0 && <PanelState kind="empty" {...noReportCopy(session?.demo_mode ?? false, "latest")} fill />}
      {data && weeks.length > 0 && (
        <div tabIndex={0} style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "4px 8px" }}>
          {weeks.map((week, i) => {
            // Additive `availability` key — absent entirely on
            // an older backend that hasn't shipped it yet, so this reads
            // defensively (`data?.availability?.[week]`) rather than assuming
            // presence; a week with no entry renders with no pack/pdf chips,
            // identical to today.
            const avail = data.availability?.[week];
            return (
              <button
                key={week}
                type="button"
                onClick={() => open(week)}
                style={{
                  display: "flex",
                  justifyContent: "space-between",
                  alignItems: "center",
                  width: "100%",
                  textAlign: "left",
                  background: opened === week ? theme.panelHeader : "transparent",
                  color: theme.text,
                  border: "none",
                  borderBottom: `1px solid ${theme.grid}`,
                  padding: "6px 4px",
                  cursor: "pointer",
                  fontSize: theme.fontSize.sm,
                }}
              >
                <span className="num">{week}</span>
                <span style={{ display: "flex", gap: 6, alignItems: "center" }}>
                  {/* P3 fix: plain muted text, no border/pill
                      — the `stale`/`partial`/`latest` chips elsewhere are
                      real status badges; "pack"/"pdf" are just labels on
                      this row, not a second clickable target inside it. */}
                  {(avail?.pack || avail?.pdf) && (
                    <span style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>
                      {[avail?.pack && "pack", avail?.pdf && "pdf"].filter(Boolean).join(" · ")}
                    </span>
                  )}
                  {i === 0 && (
                    <span style={{ fontSize: theme.fontSize.micro, color: theme.up, border: `1px solid ${theme.up}`, borderRadius: 3, padding: "0 5px" }}>
                      latest
                    </span>
                  )}
                </span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}

registerWidget<Record<string, never>>({
  id: "report-archive",
  title: "Report Archive",
  component: ReportArchive,
  defaultSize: { w: 4, h: 22 },
  minSize: { w: 3, h: 6 },
  defaultConfig: {},
  category: "Reports",
  description: "Every past weekly report — click one to open it",
});
