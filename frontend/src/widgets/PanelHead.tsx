// Shared headline/deck/agate hero block for a widget's body (Phase 14e).
// Newspaper terms, deliberately: HEADLINE — one mono tabular hero figure, the
// number that answers "what is this" at a glance. DECK — one serif sentence
// under it, the "what it shows" (pulled from widgets/help.ts via `deckLine()`
// so the same copy backs this and the panel's ⓘ popover — one source, not two
// to keep in sync). AGATE — an optional small qualifying line/row (scope tags,
// colour-coded chips, an "as of" stamp) at fontSize.micro, the same token the
// dense grid uses. Sits at the top of a widget's body, above its chart/table;
// `children` renders the widget's own controls (selects, toggles) at the
// headline's right edge.
import type { ReactNode } from "react";

import { theme } from "../theme";

export function PanelHead({
  figure,
  figureColor,
  unit,
  deck,
  agate,
  children,
}: {
  // Hero number, already formatted by the caller. Optional: most of the
  // remaining widgets (news, calendars, blotters, maps…) have no natural
  // headline figure — design review flagged the earlier required version as
  // a rollout blocker (2026-08-14). When omitted, only the controls row
  // (if any) plus deck/agate render.
  figure?: string;
  // Optional colour override for the hero figure. Omitted (the default)
  // inherits the page's normal text colour — every existing call site is
  // unaffected. Set this only where the figure's sign or severity IS the
  // information (a signed P&L, a qualitative verdict) and the app already
  // has an established colour law for it elsewhere on the same widget —
  // never for decoration. design review (2026-08-15) flagged the figure as
  // ignoring the green/red colour law the rest of the app follows.
  figureColor?: string;
  unit?: string; // currency/unit suffix shown small beside the figure
  deck: string; // one sentence, serif — usually deckLine(widgetId) from help.ts
  agate?: ReactNode; // small qualifying line under the deck — plain text or e.g. colour chips
  children?: ReactNode; // right-aligned controls beside the figure (selects, toggles)
}) {
  return (
    <div style={{ padding: "6px 8px", borderBottom: `1px solid ${theme.border}` }}>
      {figure ? (
        // Figure grammar: hero number on the left, controls (if any) pinned to
        // the right edge — the two-part hero row the figured panels use.
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", gap: 8 }}>
          <span style={{ display: "flex", alignItems: "baseline", gap: 4, flexWrap: "nowrap", minWidth: 0 }}>
            <span className="num" style={{ fontSize: theme.fontSize.xl, fontWeight: 700, color: figureColor }}>
              {figure}
            </span>
            {unit && (
              <span style={{ fontSize: theme.fontSize.micro, color: theme.muted, whiteSpace: "nowrap" }}>{unit}</span>
            )}
          </span>
          {children && <span style={{ display: "flex", gap: 4, flexShrink: 0 }}>{children}</span>}
        </div>
      ) : (
        children && (
          // Control-only grammar: no figure means
          // no hero row to be half of. Controls sit compact and left-aligned —
          // a toolbar under the title, not floated opposite empty space where a
          // figure would have been.
          <div style={{ display: "flex", gap: 4 }}>{children}</div>
        )
      )}
      <div
        title={deck}
        style={{
          // Quietest line in the block by design (design review P1,
          // 2026-08-15): the hero figure is the brightest thing here, live
          // agate data outranks static prose, and the deck — a caption, not
          // a headline — sits below both at theme.muted/micro. Family stays
          // serif (Phase 14c's prose-layer ruling still holds; agate below
          // is unstyled and inherits the page's mono, so the two lines read
          // as different KINDS of information — prose vs. data — even at
          // equal color/size weight, not as two identical noise lines).
          fontFamily: theme.fontSerif,
          fontSize: theme.fontSize.micro,
          color: theme.muted,
          marginTop: 3,
          lineHeight: 1.3,
          // Cap at 1 line — a caption doesn't wrap. The native `title`
          // above is the fallback for the clipped tail.
          display: "-webkit-box",
          WebkitLineClamp: 1,
          WebkitBoxOrient: "vertical" as const,
          overflow: "hidden",
        }}
      >
        {deck}
      </div>
      {agate && (
        <div style={{ display: "flex", gap: 12, flexWrap: "wrap", fontSize: theme.fontSize.micro, color: theme.muted, marginTop: 4 }}>
          {agate}
        </div>
      )}
    </div>
  );
}
