// Provenance dot. Small source + freshness badge for envelope-backed
// widgets: green = live & fresh, grey = served from cache, red = stale (live
// source failed → last-good shown). Hover shows source + any warnings (e.g. a
// missing FRED key). Reused across the macro widgets.
import { formatDateTime } from "../format";
import { theme } from "../theme";

export function Provenance({
  source,
  cacheHit,
  stale,
  warnings,
  fetchedAt,
}: {
  source: string;
  cacheHit?: boolean;
  stale?: boolean;
  warnings?: string[];
  fetchedAt?: string;
}) {
  const color = stale ? theme.down : cacheHit ? theme.muted : theme.up;
  const bits = [
    `source: ${source}`,
    stale ? "stale (last good)" : cacheHit ? "cached" : "live",
    ...(fetchedAt ? [`as of ${formatDateTime(fetchedAt)}`] : []),
    ...(warnings ?? []),
  ];
  return (
    <span
      title={bits.join("\n")}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        fontSize: theme.fontSize.micro,
        color: theme.muted,
        letterSpacing: "0.04em",
        whiteSpace: "nowrap",
      }}
    >
      <span style={{ width: 7, height: 7, borderRadius: "50%", background: color, flexShrink: 0 }} />
      {source.toUpperCase()}
    </span>
  );
}
