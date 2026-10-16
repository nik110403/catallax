// Shared navigation contract between the command palette, App (tab/workspace
// owner) and the active Dashboard. Kept in its own module so Dashboard never
// imports App (which would be a cycle).

// What the command palette asks the app to do.
export type PaletteCommand =
  // Switch to the tab on a given namespace, optionally scrolling a widget into view.
  | { kind: "nav"; namespace: string; focusWidgetId?: string }
  // Add a widget to the *current* tab (e.g. GO AAPL → a price-chart on AAPL).
  | { kind: "add-widget"; widgetId: string; config?: unknown };

// Handed to the active Dashboard to apply once it is mounted (a nav may remount
// it). `nonce` makes a repeated identical action retrigger the apply effect.
export interface PendingAction {
  nonce: number;
  focusWidgetId?: string;
  add?: { widgetId: string; config?: unknown };
}

// Widgets are rendered several layers below App (Dashboard → Panel → widget);
// rather than threading a nav callback through the WidgetProps contract for
// one widget (HomeIntro's "start here" row, Phase 14b), a widget can request
// the exact same nav the command palette drives through this tiny module-level
// hook — App registers its one `runCommand` handler (mirrors api/client.ts's
// onUnauthorized pattern). There is no second routing path: both the palette
// and a widget ultimately dispatch the same PaletteCommand shape.
let navHandler: ((cmd: PaletteCommand) => void) | null = null;
export function onNavigate(handler: (cmd: PaletteCommand) => void): void {
  navHandler = handler;
}
export function requestNav(cmd: PaletteCommand): void {
  navHandler?.(cmd);
}
