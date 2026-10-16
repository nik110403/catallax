// Recession signals (Phase 8, Macro). A small composite read from keyless rates
// data: 10y-2y and 10y-3m curve inversion (the canonical leading indicator) plus
// the recent trend in the 10y-3m spread. Not investment advice — a dashboard tile.
import { api } from "../api/client";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import { PanelState } from "./PanelState";
import { Provenance } from "./Provenance";
import { registerWidget } from "./registry";
import { useAsync } from "./useAsync";

function Signal({ label, value, bad, detail }: { label: string; value: string; bad: boolean; detail?: string }) {
  return (
    <div
      style={{
        display: "flex",
        justifyContent: "space-between",
        alignItems: "baseline",
        padding: "5px 8px",
        borderBottom: `1px solid ${theme.border}`,
      }}
    >
      <span style={{ display: "flex", flexDirection: "column" }}>
        <span style={{ fontSize: theme.fontSize.micro }}>{label}</span>
        {detail && <span style={{ fontSize: theme.fontSize.micro, color: theme.muted }}>{detail}</span>}
      </span>
      <span className="num" style={{ color: bad ? theme.down : theme.up, fontWeight: 600 }}>
        {value}
      </span>
    </div>
  );
}

const bps = (v: number | null | undefined) =>
  v === null || v === undefined ? "—" : `${v >= 0 ? "+" : ""}${(v * 100).toFixed(0)}bp`;

function RecessionSignals() {
  const yc = useAsync(() => api.macroYieldCurve("US"), [], 3_600_000);
  const rt = useAsync(() => api.macroRates(), [], 3_600_000);
  if ((yc.loading && !yc.data) || (rt.loading && !rt.data)) return <PanelState kind="loading" />;
  const disabled = yc.errorCode === "SIDECAR_NOT_ENABLED" || rt.errorCode === "SIDECAR_NOT_ENABLED";
  const errored = !disabled && !!yc.error;

  const curve = yc.data?.data;
  const spreads = rt.data?.data.spreads ?? [];
  const last = spreads[spreads.length - 1];
  const prior = spreads.length > 22 ? spreads[spreads.length - 22] : spreads[0]; // ~1 month ago
  const s10_3m = last?.t10y_3m ?? curve?.spread_10y_3m ?? null;
  const s10_2y = curve?.spread_10y_2y ?? last?.t10y_2y ?? null;
  const trend = last && prior && last.t10y_3m !== null && prior.t10y_3m !== null ? last.t10y_3m - prior.t10y_3m : null;

  // composite: count inverted spreads
  const inversions = [s10_2y, s10_3m].filter((v) => v !== null && v < 0).length;
  const verdict = inversions >= 2 ? "Elevated" : inversions === 1 ? "Watch" : "Low";
  const verdictColor = inversions >= 2 ? theme.down : inversions === 1 ? theme.warn : theme.up;
  const hasData = !disabled && !errored;

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead
        // Qualitative hero: this widget's whole point is the verdict, so it
        // gets the PanelHead figure slot (design review P1, 2026-08-15:
        // "the widget whose whole point is a verdict has no headline").
        // figureColor carries the severity — genuinely semantic here, not
        // decoration — so it no longer only lives in the (now-removed) agate
        // line below it. Omitted (not just blanked) on disabled/error — this
        // section no longer early-returns before reaching PanelHead, so
        // `verdict` is now always computed on whatever spreads happen to be
        // in scope (all null pre-fetch) and would otherwise show a fabricated
        // "Low risk" during the disabled/error states it belongs to.
        figure={hasData ? verdict : undefined}
        figureColor={verdictColor}
        unit="recession risk"
        deck={deckLine("recession-signals")}
        agate={
          rt.data && (
            <Provenance source={rt.data.source} cacheHit={rt.data.cache_hit} stale={rt.data.stale} warnings={rt.data.warnings} fetchedAt={rt.data.fetched_at} />
          )
        }
      />
      {/* keep the signal rows + footer mounted but hidden whenever a state
          should own the body instead — same shape as MacroHeatmap's table
          guard: test disabled/error, not just data, since a disabled fetch
          never resolves a body but these values default to non-null "—"
          placeholders that would otherwise render as if data existed. */}
      <div tabIndex={0} style={{ flex: 1, minHeight: 0, overflow: "auto", display: hasData ? "block" : "none" }}>
        <Signal label="10Y–2Y curve" value={bps(s10_2y)} bad={(s10_2y ?? 0) < 0} detail={(s10_2y ?? 0) < 0 ? "inverted" : "positive"} />
        <Signal label="10Y–3M curve" value={bps(s10_3m)} bad={(s10_3m ?? 0) < 0} detail={(s10_3m ?? 0) < 0 ? "inverted" : "positive"} />
        <Signal
          label="10Y–3M, 1-month change"
          value={bps(trend)}
          bad={(trend ?? 0) < 0}
          detail={trend === null ? undefined : (trend < 0 ? "flattening" : "steepening")}
        />
      </div>
      {hasData && (
        <div style={{ padding: "4px 8px", borderTop: `1px solid ${theme.border}`, fontSize: theme.fontSize.micro, color: theme.muted }}>
          Curve inversion has preceded US recessions; a heuristic tile, not advice.
        </div>
      )}
      {/* Short form, not the full disabled explainer — see MacroSeries.tsx
          for why: Indicator Heatmap (same tab, same "macro" profile) carries
          the full instruction so the Macro tab reads as one situation
          instead of three identical failures (design review
          2026-08-15). */}
      {disabled && (
        <div
          className="panel-state"
          role="status"
          style={{ flex: 1, display: "flex", alignItems: "center", justifyContent: "center", textAlign: "center", padding: "0 12px" }}
        >
          {/* One child, not bare text + a span — see MacroSeries.tsx for why:
              a flex container wraps multiple direct text/element children in
              separate anonymous flex items and trims the whitespace between
              them. */}
          <span>
            Part of <span style={{ fontFamily: theme.fontMono }}>Macro</span> (off) — see Indicator Heatmap.
          </span>
        </div>
      )}
      {errored && <PanelState kind="error" message={yc.error!} fill />}
    </div>
  );
}

registerWidget({
  id: "recession-signals",
  title: "Recession Signals",
  component: RecessionSignals,
  defaultSize: { w: 4, h: 7 },
  minSize: { w: 3, h: 5 },
  defaultConfig: {},
  category: "Macro",
  description: "Yield-curve inversion composite (10y-2y / 10y-3m)",
});
