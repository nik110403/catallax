// AI Status. Provider health card for the Claude Code LLM adapter
// used by the ai-runner worker (the API process itself uses the no-op provider).
import { api } from "../api/client";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";

function AIStatus() {
  const { data: s, loading, error } = useAsync(() => api.intelStatus(), [], 60_000);
  const isNoop = s?.provider === "noop";
  const unreachable = !isNoop && (!!error || (s && !s.healthy));
  // Rewritten for a reader, not a maintainer:
  // the old body was "Provider: noop / Claude reachable: n/a" plus two lines
  // about "the ai-runner container" — a console note, not product copy. The
  // operational detail (container name, --profile ai, the no-op split) still
  // matters to the operator running this stack, so it moved to the ⓘ popover
  // (WIDGET_HELP["ai-status"] in help.ts) rather than being deleted.
  const statusWord = loading && !s ? undefined : isNoop ? "OFF" : unreachable ? "DOWN" : "ON";
  const statusColor = isNoop ? theme.muted : unreachable ? theme.down : theme.up;

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead figure={statusWord} figureColor={statusColor} unit="AI enrichment" deck={deckLine("ai-status")} />
      <div tabIndex={0} style={{ flex: 1, minHeight: 0, overflowY: "auto", display: "flex", flexDirection: "column", gap: 6, padding: "8px 10px" }}>
        <div style={{ fontSize: theme.fontSize.micro, color: theme.text, lineHeight: 1.5 }}>
          {isNoop
            ? "AI enrichment is off in this environment — news headlines show without sentiment, topic, or summary tags."
            : unreachable
              ? "AI enrichment is turned on but not responding right now — new headlines won't be tagged until it recovers."
              : "AI enrichment is on and reachable — new headlines are being tagged with sentiment, topic, and a grounded summary."}
        </div>
      </div>
    </div>
  );
}

registerWidget({
  id: "ai-status",
  title: "AI Status",
  component: AIStatus,
  defaultSize: { w: 4, h: 5 },
  minSize: { w: 3, h: 4 },
  defaultConfig: {},
  category: "AI",
  description: "Claude Code provider health.",
});
