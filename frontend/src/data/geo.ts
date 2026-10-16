// Static geo layers for the Markets geo-map. Curated, hand-maintained
// GeoJSON — not live data. The live "natural events" layer (earthquakes) comes
// from the backend `/markets/quakes` proxy instead.
import type { GeoFeatureCollection } from "../api/types";

const point = (
  lon: number,
  lat: number,
  props: Record<string, unknown>,
): GeoFeatureCollection["features"][number] => ({
  type: "Feature",
  geometry: { type: "Point", coordinates: [lon, lat] },
  properties: props,
});

const line = (
  coords: Array<[number, number]>,
  props: Record<string, unknown>,
): GeoFeatureCollection["features"][number] => ({
  type: "Feature",
  geometry: { type: "LineString", coordinates: coords },
  properties: props,
});

// Major financial centres (the "economic" layer).
export const FINANCIAL_HUBS: GeoFeatureCollection = {
  type: "FeatureCollection",
  features: [
    point(-74.006, 40.7128, { name: "New York" }),
    point(-0.1276, 51.5074, { name: "London" }),
    point(139.6917, 35.6895, { name: "Tokyo" }),
    point(114.1694, 22.3193, { name: "Hong Kong" }),
    point(103.8198, 1.3521, { name: "Singapore" }),
    point(8.6821, 50.1109, { name: "Frankfurt" }),
    point(121.4737, 31.2304, { name: "Shanghai" }),
    point(55.2708, 25.2048, { name: "Dubai" }),
    point(-118.2437, 34.0522, { name: "Los Angeles" }),
    point(151.2093, -33.8688, { name: "Sydney" }),
  ],
};

// Broad-sanctions jurisdictions (illustrative; the "sanctions" layer).
export const SANCTIONS: GeoFeatureCollection = {
  type: "FeatureCollection",
  features: [
    point(37.6173, 55.7558, { name: "Russia" }),
    point(51.389, 35.6892, { name: "Iran" }),
    point(125.7625, 39.0392, { name: "North Korea" }),
    point(36.2765, 33.5138, { name: "Syria" }),
    point(-66.9036, 10.4806, { name: "Venezuela" }),
    point(-82.3666, 23.1136, { name: "Cuba" }),
    point(27.5615, 53.9006, { name: "Belarus" }),
  ],
};

// Major maritime trade routes (a few waypoints each; the "trade routes" layer).
export const TRADE_ROUTES: GeoFeatureCollection = {
  type: "FeatureCollection",
  features: [
    line(
      [
        [121.4737, 31.2304], // Shanghai
        [103.8198, 1.3521], // Singapore (Malacca)
        [43.0, 12.5], // Bab-el-Mandeb
        [32.55, 29.95], // Suez
        [5.0, 36.0], // Mediterranean
        [-5.6, 36.0], // Gibraltar
        [4.4777, 51.9244], // Rotterdam
      ],
      { name: "Asia–Europe (Suez)" },
    ),
    line(
      [
        [121.4737, 31.2304], // Shanghai
        [139.6917, 35.6895], // Tokyo
        [-150.0, 40.0], // mid-Pacific
        [-118.2437, 33.74], // Los Angeles
      ],
      { name: "Trans-Pacific" },
    ),
    line(
      [
        [-74.006, 40.7128], // New York
        [-40.0, 47.0], // mid-Atlantic
        [-0.1276, 51.5074], // London
        [4.4777, 51.9244], // Rotterdam
      ],
      { name: "Trans-Atlantic" },
    ),
    line(
      [
        [55.2708, 25.2048], // Dubai
        [43.0, 12.5], // Bab-el-Mandeb
        [32.55, 29.95], // Suez
        [14.5, 35.9], // Malta
        [-5.6, 36.0], // Gibraltar
      ],
      { name: "Gulf–Mediterranean" },
    ),
    line(
      [
        [103.8198, 1.3521], // Singapore
        [80.0, 6.0], // off Sri Lanka
        [55.2708, 25.2048], // Dubai
      ],
      { name: "Indian Ocean" },
    ),
  ],
};
