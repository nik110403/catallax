// AI News. RSS headlines enriched by the off-API ai-runner worker:
// per-story sentiment, country tag, topic labels, a grounded/ungrounded summary
// dot, and a Claude-written grounded summary. Registered lazily in
// widgets/index.ts (registerLazyWidget).
import { useState } from "react";

import { api } from "../api/client";
import type { IntelNewsItem } from "../api/types";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import { useAsync } from "./useAsync";

// RSS links are untrusted: only allow http(s) so a feed can't inject a
// javascript:/data: URL that runs on click. Returns null (render plain text,
// no href) for anything else.
function safeHref(link: string): string | null {
  return /^https?:\/\//i.test(link) ? link : null;
}

const SENT_COLOR: Record<string, string> = {
  positive: theme.up,
  negative: theme.down,
  neutral: theme.muted,
};

function Story({ it }: { it: IntelNewsItem }) {
  const href = safeHref(it.link);
  return (
    <div style={{ borderBottom: `1px solid ${theme.grid}`, paddingBottom: 6, display: "flex", flexDirection: "column", gap: 3 }}>
      <div style={{ display: "flex", gap: 6, alignItems: "center" }}>
        {it.sentiment && (
          <span
            style={{
              fontSize: theme.fontSize.micro,
              color: SENT_COLOR[it.sentiment] ?? theme.muted,
              border: "1px solid currentColor",
              borderRadius: 3,
              padding: "0 4px",
            }}
          >
            {it.sentiment}
          </span>
        )}
        {it.country && <span style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>{it.country}</span>}
        <span
          title={it.grounding}
          style={{
            width: 6,
            height: 6,
            borderRadius: 3,
            background: it.grounding === "grounded" ? theme.up : theme.muted,
          }}
        />
      </div>
      {href ? (
        <a href={href} target="_blank" rel="noreferrer" style={{ color: theme.text, textDecoration: "none", fontWeight: 600, fontSize: theme.fontSize.sm }}>
          {it.title}
        </a>
      ) : (
        <span style={{ fontWeight: 600, fontSize: theme.fontSize.sm }}>{it.title}</span>
      )}
      {it.summary && <div style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>{it.summary}</div>}
      <div style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>
        {it.source}
        {it.topics.length ? ` · ${it.topics.join(" · ")}` : ""}
      </div>
    </div>
  );
}

export function AINews() {
  // Bumping retryKey re-runs useAsync's fetch — it's already part of the
  // hook's public contract (any deps change re-triggers the effect), so
  // Retry needs no change to useAsync.ts itself.
  const [retryKey, setRetryKey] = useState(0);
  const { data, loading, error } = useAsync(() => api.intelNews(60), [retryKey], 120_000);
  if (loading && !data) return <PanelState kind="loading" />;
  if (error) return <PanelState kind="error" message={error} onRetry={() => setRetryKey((k) => k + 1)} />;
  const items = data?.items ?? [];
  if (!items.length) return <PanelState kind="empty" message='No enriched news yet — run "Enrich now" (Intel Runner).' />;
  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead deck={deckLine("ai-news")} />
      <div tabIndex={0} style={{ flex: 1, minHeight: 0, overflowY: "auto", padding: "8px 10px", display: "flex", flexDirection: "column", gap: 8 }}>
        {items.map((it) => (
          <Story key={it.link} it={it} />
        ))}
      </div>
    </div>
  );
}
