import { useState } from "react";

import { api } from "../api/client";
import { formatDateTime, formatTime } from "../format";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import type { WidgetProps } from "./registry";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";

// RSS links are untrusted: only allow http(s) so a feed can't inject a
// javascript:/data: URL that runs on click.
function safeHref(url: string): string {
  try {
    const u = new URL(url, location.origin);
    return u.protocol === "http:" || u.protocol === "https:" ? u.href : "#";
  } catch {
    return "#";
  }
}

function fmtWhen(iso: string): string {
  const d = new Date(iso);
  const sameDay = d.toDateString() === new Date().toDateString();
  return sameDay ? formatTime(d) : formatDateTime(d);
}

function NewsPanel(_: WidgetProps<Record<string, never>>) {
  // Bumping retryKey re-runs useAsync's fetch — it's already part of the
  // hook's public contract (any deps change re-triggers the effect), so
  // Retry needs no change to useAsync.ts itself.
  const [retryKey, setRetryKey] = useState(0);
  const { data, loading, error } = useAsync(() => api.news(), [retryKey], 120_000);
  const items = data ?? [];
  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      {/* PanelHead renders unconditionally so it doesn't appear and disappear
          as data lands;
          loading/error/empty occupy only the body area below it. */}
      <PanelHead deck={deckLine("news")} />
      {loading && !data ? (
        <PanelState kind="loading" fill />
      ) : error ? (
        <PanelState kind="error" message={error} onRetry={() => setRetryKey((k) => k + 1)} fill />
      ) : !items.length ? (
        <PanelState kind="empty" message="no news" fill />
      ) : (
        <div tabIndex={0} style={{ flex: 1, minHeight: 0, overflowY: "auto" }}>
          {items.map((n, i) => (
            <a className="news-item" key={i} href={safeHref(n.link)} target="_blank" rel="noreferrer">
              <div className="news-title">{n.title}</div>
              <div className="news-src">
                {n.source}
                {n.published ? ` · ${fmtWhen(n.published)}` : ""}
              </div>
            </a>
          ))}
        </div>
      )}
    </div>
  );
}

registerWidget<Record<string, never>>({
  id: "news",
  title: "Market News",
  component: NewsPanel,
  defaultSize: { w: 7, h: 9 },
  minSize: { w: 3, h: 4 },
  defaultConfig: {},
});
