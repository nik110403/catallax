import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// No /api proxy here — nginx routes /api to the backend.
export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    strictPort: true,
  },
  build: {
    rollupOptions: {
      output: {
        // Peel the heavy charting/mapping libs into their own chunks so the
        // entry bundle stays lean. maplibre is reached only through the lazily
        // imported geo-map widget, so its chunk loads on demand (when a geo-map
        // panel mounts); lightweight-charts backs the always-present net-worth
        // widget, so its chunk loads with the page.
        manualChunks: {
          maplibre: ["maplibre-gl"],
          charts: ["lightweight-charts"],
        },
      },
    },
  },
});
