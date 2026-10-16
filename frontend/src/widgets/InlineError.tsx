import type { CSSProperties } from "react";
import { READ_ONLY_DEMO_MESSAGE } from "../api/client";
import { theme } from "../theme";

// One inline error line for every widget. The hosted read-only demo's write
// rejection is expected, not a fault: it renders as a quiet muted line
// (role=status, no fill, no border). Anything else keeps the danger styling —
// "box" (tinted, bordered) or "text" (red text only).
export function InlineError({
  msg,
  variant = "box",
  style,
}: {
  msg: string;
  variant?: "box" | "text";
  style?: CSSProperties;
}) {
  if (msg === READ_ONLY_DEMO_MESSAGE) {
    return (
      <div role="status" style={{ fontSize: theme.fontSize.micro, color: theme.muted, ...style }}>
        {msg}
      </div>
    );
  }
  const box: CSSProperties =
    variant === "box"
      ? { background: "rgba(248,81,73,0.08)", border: `1px solid ${theme.down}`, borderRadius: 3, padding: "4px 8px" }
      : {};
  return (
    <div role="alert" style={{ fontSize: theme.fontSize.micro, color: theme.down, ...box, ...style }}>
      {msg}
    </div>
  );
}
