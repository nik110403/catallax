import maplibregl from "maplibre-gl";
import "maplibre-gl/dist/maplibre-gl.css";
import { useEffect, useRef } from "react";

import { api } from "../api/client";
import type { IntelNewsItem } from "../api/types";
import { FINANCIAL_HUBS, SANCTIONS, TRADE_ROUTES } from "../data/geo";
import { theme } from "../theme";
import { deckLine } from "./help";
import { PanelHead } from "./PanelHead";
import type { WidgetProps } from "./registry";
import { useAsync } from "./useAsync";

// worldmonitor-inspired geopolitical map. MapLibre GL on a free CARTO
// dark base. Layers: economic (financial hubs) · sanctions · trade routes (static
// curated GeoJSON) + natural events (live USGS earthquakes via the backend proxy)
// + AI events (geo-tagged enriched news via /intel/events, Phase 12).
interface GeoConfig {
  layers: {
    economic: boolean;
    sanctions: boolean;
    trade: boolean;
    natural: boolean;
    ai_events: boolean;
  };
}

const STYLE = "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json";

// layer id -> config flag, so visibility toggling is a simple loop.
const LAYER_FLAGS: Record<string, keyof GeoConfig["layers"]> = {
  "trade-routes": "trade",
  "financial-hubs": "economic",
  sanctions: "sanctions",
  quakes: "natural",
  ai_events_layer: "ai_events",
};

const EMPTY = { type: "FeatureCollection", features: [] };

// AI-events popup fields (title/summary/source) originate from arbitrary,
// untrusted RSS feeds ingested/enriched by the backend — escape before
// interpolating into popup HTML (setHTML sets innerHTML with no sanitization).
const escapeHtml = (s: string): string =>
  s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");

// Our loose GeoFeatureCollection (geometry.type: string) → the strict GeoJSON
// type MapLibre's source/setData APIs expect. Validated by the backend/static data.
type MapData = maplibregl.GeoJSONSourceSpecification["data"];
const asGeo = (d: unknown): MapData => d as MapData;

// /intel/events items -> a point FeatureCollection, skipping any still missing
// lat/lon (the backend already filters these, but stay defensive here too).
const eventsToGeo = (items: IntelNewsItem[] | undefined): MapData =>
  asGeo({
    type: "FeatureCollection",
    features: (items ?? [])
      .filter((e): e is IntelNewsItem & { lat: number; lon: number } => e.lat != null && e.lon != null)
      .map((e) => ({
        type: "Feature",
        geometry: { type: "Point", coordinates: [e.lon, e.lat] },
        properties: {
          title: e.title,
          summary: e.summary,
          source: e.source,
          sentiment: e.sentiment,
        },
      })),
  });

const LAYER_UI: Array<[keyof GeoConfig["layers"], string, string]> = [
  ["economic", "Economic", theme.accent],
  ["sanctions", "Sanctions", theme.down],
  ["trade", "Trade routes", theme.chart[0]],
  ["natural", "Quakes (live)", theme.chart[5]],
  ["ai_events", "AI Events", theme.chart[4]],
];

// Registered lazily in widgets/index.ts (registerLazyWidget) so maplibre-gl only
// loads when a geo-map panel mounts.
export function GeoMap({ config, onConfigChange }: WidgetProps<GeoConfig>) {
  const elRef = useRef<HTMLDivElement>(null);
  const mapRef = useRef<maplibregl.Map | null>(null);
  const readyRef = useRef(false);
  const { data: quakes } = useAsync(() => api.marketsQuakes(), [], 300_000);
  const { data: aiEvents } = useAsync(() => api.intelEvents(), [], 300_000);

  // ── init map once ────────────────────────────────────────
  useEffect(() => {
    if (!elRef.current) return;
    const map = new maplibregl.Map({
      container: elRef.current,
      style: STYLE,
      center: [10, 25],
      zoom: 1.3,
      attributionControl: { compact: true },
    });
    map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
    mapRef.current = map;

    map.on("load", () => {
      map.addSource("trade-routes", { type: "geojson", data: asGeo(TRADE_ROUTES) });
      map.addLayer({
        id: "trade-routes",
        type: "line",
        source: "trade-routes",
        paint: {
          "line-color": theme.chart[0],
          "line-width": 1.4,
          "line-opacity": 0.7,
          "line-dasharray": [2, 1.5],
        },
      });

      map.addSource("financial-hubs", { type: "geojson", data: asGeo(FINANCIAL_HUBS) });
      map.addLayer({
        id: "financial-hubs",
        type: "circle",
        source: "financial-hubs",
        paint: {
          "circle-radius": 4,
          "circle-color": theme.accent,
          "circle-stroke-color": theme.bg,
          "circle-stroke-width": 1,
        },
      });

      map.addSource("sanctions", { type: "geojson", data: asGeo(SANCTIONS) });
      map.addLayer({
        id: "sanctions",
        type: "circle",
        source: "sanctions",
        paint: {
          "circle-radius": 6,
          "circle-color": theme.down,
          "circle-opacity": 0.65,
          "circle-stroke-color": theme.down,
          "circle-stroke-width": 1,
        },
      });

      map.addSource("quakes", { type: "geojson", data: asGeo(EMPTY) });
      map.addLayer({
        id: "quakes",
        type: "circle",
        source: "quakes",
        paint: {
          "circle-radius": ["interpolate", ["linear"], ["get", "mag"], 2.5, 3, 7, 14],
          "circle-color": theme.chart[5],
          "circle-opacity": 0.55,
          "circle-stroke-color": theme.chart[5],
          "circle-stroke-width": 1,
        },
      });

      map.addSource("ai-events", { type: "geojson", data: asGeo(EMPTY) });
      map.addLayer({
        id: "ai_events_layer",
        type: "circle",
        source: "ai-events",
        paint: {
          "circle-radius": 5,
          "circle-color": [
            "match",
            ["get", "sentiment"],
            "negative",
            theme.down,
            "positive",
            theme.up,
            theme.muted,
          ],
          "circle-opacity": 0.75,
          "circle-stroke-color": theme.bg,
          "circle-stroke-width": 1,
        },
      });

      // Click popups: financial hubs / sanctions show `name`, quakes show place+mag,
      // AI events show title/summary/source.
      const popup = (label: string) => (e: maplibregl.MapLayerMouseEvent) => {
        const f = e.features?.[0];
        if (!f) return;
        const p = f.properties || {};
        const html =
          label === "quakes"
            ? `<b>M ${p.mag ?? "?"}</b><br>${p.place ?? ""}`
            : label === "ai_events_layer"
              ? `<b>${escapeHtml(String(p.title ?? ""))}</b><br>${escapeHtml(String(p.summary ?? ""))}<br><i>${escapeHtml(String(p.source ?? ""))}</i>`
              : `<b>${p.name ?? ""}</b>`;
        new maplibregl.Popup({ closeButton: false })
          .setLngLat(e.lngLat)
          .setHTML(`<div class="geo-popup">${html}</div>`)
          .addTo(map);
      };
      for (const id of ["financial-hubs", "sanctions", "quakes", "ai_events_layer"]) {
        map.on("click", id, popup(id));
        map.on("mouseenter", id, () => (map.getCanvas().style.cursor = "pointer"));
        map.on("mouseleave", id, () => (map.getCanvas().style.cursor = ""));
      }

      readyRef.current = true;
      applyVisibility(map, config.layers);
      if (quakesRef.current) {
        (map.getSource("quakes") as maplibregl.GeoJSONSource | undefined)?.setData(
          asGeo(quakesRef.current),
        );
      }
      if (aiEventsRef.current) {
        (map.getSource("ai-events") as maplibregl.GeoJSONSource | undefined)?.setData(
          eventsToGeo(aiEventsRef.current),
        );
      }
    });

    const ro = new ResizeObserver(() => map.resize());
    ro.observe(elRef.current);
    return () => {
      ro.disconnect();
      readyRef.current = false;
      map.remove();
      mapRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── push live quake data when it arrives ─────────────────
  const quakesRef = useRef<unknown>(null);
  useEffect(() => {
    quakesRef.current = quakes ?? null;
    const map = mapRef.current;
    if (!map || !readyRef.current || !quakes) return;
    (map.getSource("quakes") as maplibregl.GeoJSONSource | undefined)?.setData(asGeo(quakes));
  }, [quakes]);

  // ── push AI-events data when it arrives ───────────────────
  const aiEventsRef = useRef<IntelNewsItem[] | null>(null);
  useEffect(() => {
    aiEventsRef.current = aiEvents?.events ?? null;
    const map = mapRef.current;
    if (!map || !readyRef.current || !aiEvents) return;
    (map.getSource("ai-events") as maplibregl.GeoJSONSource | undefined)?.setData(
      eventsToGeo(aiEvents.events),
    );
  }, [aiEvents]);

  // ── layer visibility follows config ─────────────────────
  useEffect(() => {
    const map = mapRef.current;
    if (map && readyRef.current) applyVisibility(map, config.layers);
  }, [config.layers]);

  const toggle = (k: keyof GeoConfig["layers"]) =>
    onConfigChange({ ...config, layers: { ...config.layers, [k]: !config.layers[k] } });

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%" }}>
      <PanelHead deck={deckLine("geo-map")} />
      <div style={{ position: "relative", flex: 1, minHeight: 0 }}>
        <div ref={elRef} style={{ width: "100%", height: "100%" }} />
        <div className="geo-controls">
          {LAYER_UI.map(([k, label, color]) => (
            <label key={k} className="geo-toggle">
              <input type="checkbox" checked={!!config.layers[k]} onChange={() => toggle(k)} />
              <span style={{ width: 8, height: 8, borderRadius: 2, background: color }} />
              {label}
            </label>
          ))}
        </div>
      </div>
    </div>
  );
}

function applyVisibility(map: maplibregl.Map, flags: GeoConfig["layers"]) {
  for (const [layerId, flag] of Object.entries(LAYER_FLAGS)) {
    if (map.getLayer(layerId)) {
      map.setLayoutProperty(layerId, "visibility", flags[flag] ? "visible" : "none");
    }
  }
}

