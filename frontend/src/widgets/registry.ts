// Widget registry. Light widgets self-register eagerly via registerWidget(...) at
// import time; heavy ones (charts, maps) register metadata eagerly but ship their
// component behind registerLazyWidget(...) so the module — and its dependencies —
// only load when a panel mounts. The grid renders only what the registry knows
// about. See skills/react-widget-system.md.
import { lazy } from "react";
import type React from "react";

export interface WidgetProps<C = unknown> {
  config: C;
  onConfigChange: (next: C) => void;
}

export interface WidgetDefinition<C = unknown> {
  id: string; // unique, kebab-case: "price-chart"
  title: string;
  component: React.ComponentType<WidgetProps<C>>;
  defaultSize: { w: number; h: number };
  minSize?: { w: number; h: number };
  defaultConfig: C;
  configSchema?: unknown;
  // Catalog grouping (Personal / Markets / Research / …). Optional: widgets
  // without one fall back to a central map in WidgetCatalog, else "General".
  category?: string;
  // One-line catalog blurb. Optional; falls back to the title.
  description?: string;
  // Multi-line "what it shows + how to read it" text for the panel's ⓘ help
  // popover. Optional; widgets without one fall back to the central map in
  // widgets/help.ts (keyed by id). Plain text; blank lines separate paragraphs.
  help?: string;
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
const registry = new Map<string, WidgetDefinition<any>>();

export function registerWidget<C>(def: WidgetDefinition<C>): void {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  registry.set(def.id, def as WidgetDefinition<any>);
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function getWidget(id: string): WidgetDefinition<any> | undefined {
  return registry.get(id);
}

// eslint-disable-next-line @typescript-eslint/no-explicit-any
export function listWidgets(): WidgetDefinition<any>[] {
  return [...registry.values()];
}

// Everything the catalog / palette / seed need without importing the component.
// eslint-disable-next-line @typescript-eslint/no-explicit-any
export type WidgetMeta = Omit<WidgetDefinition<any>, "component">;
export type WidgetLoader = () => Promise<{
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  default: React.ComponentType<WidgetProps<any>>;
}>;

// Register a code-split widget: metadata is available immediately; the component
// (and its heavy deps) load on first mount via React.lazy. Render sites must be
// inside a <Suspense> (Dashboard wraps every panel).
export function registerLazyWidget(meta: WidgetMeta, loader: WidgetLoader): void {
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  registry.set(meta.id, { ...meta, component: lazy(loader) } as unknown as WidgetDefinition<any>);
}
