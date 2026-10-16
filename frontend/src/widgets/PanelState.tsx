// Shared loading / empty / error / not-enabled surface for every panel (Phase 14e).
// One component so copy, spacing and the live-region semantics are fixed in one
// place — previously this markup was duplicated inline in 39 widgets.
import { theme } from "../theme";

// `fill` centers the message and lets it own the whole panel body — for a state
// that replaces a chart/canvas which would otherwise leave a blank frame (the
// caller keeps its own element mounted but hidden, not unmounted, so effects
// that need the node survive).
const fillStyle = {
  flex: 1,
  display: "flex",
  flexDirection: "column" as const,
  alignItems: "center",
  justifyContent: "center",
  textAlign: "center" as const,
};

// Same as fillStyle but stretched full-width — a skeleton is a shape, not a
// centered caption, so it shouldn't shrink to its own content width.
const fillSkeletonStyle = { ...fillStyle, alignItems: "stretch" as const, textAlign: "left" as const };

// Visually-hidden but screen-reader-visible — used to keep a real loading
// announcement on the `role="status"` region when the visible content is a
// decorative skeleton with no text of its own.
const srOnlyStyle: React.CSSProperties = {
  position: "absolute",
  width: 1,
  height: 1,
  padding: 0,
  margin: -1,
  overflow: "hidden",
  clip: "rect(0,0,0,0)",
  whiteSpace: "nowrap",
  border: 0,
};

// Shimmer keyframes + the reduced-motion override, scoped here (not
// index.css) since this file owns the skeleton feature end to end. Rules are
// idempotent, so more than one loading skeleton mounted at once just repeats
// identical CSS text — harmless, no dedup needed for this component count.
const skeletonCss = `
@keyframes panel-skeleton-shimmer {
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
}
.panel-skeleton__bar {
  background: linear-gradient(90deg, ${theme.grid} 25%, ${theme.panelHeader} 50%, ${theme.grid} 75%);
  background-size: 400% 100%;
  animation: panel-skeleton-shimmer 1.6s ease-in-out infinite;
}
@media (prefers-reduced-motion: reduce) {
  .panel-skeleton__bar {
    animation: none;
    background: ${theme.grid};
  }
}
`;

function SkeletonBar({ width, height = 10 }: { width: string | number; height?: number }) {
  return <div aria-hidden="true" className="panel-skeleton__bar" style={{ width, height, borderRadius: 3 }} />;
}

// Mimics a header row + a few data rows — for table/list-shaped panels.
function TableSkeleton() {
  const widths = ["34%", "18%", "18%", "16%"];
  return (
    <div aria-hidden="true" style={{ width: "100%", padding: "4px 2px", display: "flex", flexDirection: "column", gap: 8 }}>
      <div style={{ display: "flex", gap: 8 }}>
        {widths.map((w, i) => (
          <SkeletonBar key={i} width={w} height={8} />
        ))}
      </div>
      {[0, 1, 2, 3, 4].map((row) => (
        <div key={row} style={{ display: "flex", gap: 8 }}>
          {widths.map((w, i) => (
            <SkeletonBar key={i} width={w} />
          ))}
        </div>
      ))}
    </div>
  );
}

// Mimics a plotted area plus its axis labels — for chart-shaped panels.
function ChartSkeleton() {
  return (
    <div aria-hidden="true" style={{ width: "100%", padding: "4px 2px", display: "flex", flexDirection: "column", gap: 8 }}>
      <SkeletonBar width="100%" height={120} />
      <div style={{ display: "flex", gap: 8 }}>
        <SkeletonBar width="20%" height={8} />
        <SkeletonBar width="20%" height={8} />
        <SkeletonBar width="20%" height={8} />
      </div>
    </div>
  );
}

type PanelStateProps =
  | { kind: "loading"; message?: string; fill?: boolean; skeleton?: "table" | "chart" }
  | { kind: "empty"; message: string; hint?: string; guidance?: string; fill?: boolean; children?: React.ReactNode }
  | { kind: "error"; message: string; onRetry?: () => void; fill?: boolean; children?: React.ReactNode }
  | { kind: "disabled"; profile: string; fill?: boolean };

export function PanelState(props: PanelStateProps) {
  if (props.kind === "loading") {
    if (props.skeleton) {
      return (
        <div className="panel-state" role="status" aria-live="polite" style={props.fill ? fillSkeletonStyle : undefined}>
          <span style={srOnlyStyle}>{props.message ?? "loading…"}</span>
          <style>{skeletonCss}</style>
          {props.skeleton === "table" ? <TableSkeleton /> : <ChartSkeleton />}
        </div>
      );
    }
    return (
      <div className="panel-state" role="status" aria-live="polite" style={props.fill ? fillStyle : undefined}>
        {props.message ?? "loading…"}
      </div>
    );
  }

  if (props.kind === "empty") {
    return (
      <div className="panel-state" role="status" style={props.fill ? fillStyle : undefined}>
        {props.message}
        {props.guidance && (
          <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, marginTop: 4 }}>{props.guidance}</div>
        )}
        {props.hint && (
          <div style={{ fontSize: theme.fontSize.micro, color: theme.muted, marginTop: 4 }}>{props.hint}</div>
        )}
        {props.children}
      </div>
    );
  }

  if (props.kind === "error") {
    return (
      <div className="panel-state error" role="alert" style={props.fill ? fillStyle : undefined}>
        {props.message}
        {props.onRetry && (
          <button
            onClick={props.onRetry}
            style={{
              marginLeft: 8,
              padding: "1px 8px",
              background: "transparent",
              border: `1px solid ${theme.down}`,
              color: theme.down,
              borderRadius: 3,
              fontSize: theme.fontSize.micro,
              cursor: "pointer",
            }}
          >
            Retry
          </button>
        )}
        {props.children}
      </div>
    );
  }

  // kind === "disabled" — replaces an infinite spinner for a profile-gated
  // service (e.g. algo/backtester) that isn't running in this compose stack.
  return (
    <div className="panel-state" role="status" style={props.fill ? fillStyle : undefined}>
      This needs the "{props.profile}" service. Start it with:{" "}
      <span style={{ fontFamily: theme.fontMono }}>{`docker compose --profile ${props.profile} up -d`}</span>
    </div>
  );
}
