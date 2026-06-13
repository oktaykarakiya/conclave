import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";
import { defineConfig } from "vite";

// Build output lands directly in the daemon's static dir so it serves the SPA.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  build: {
    outDir: "../src/conclave/web/static",
    emptyOutDir: true,
  },
  server: {
    proxy: {
      "/api": "http://127.0.0.1:8700",
      "/ws": { target: "ws://127.0.0.1:8700", ws: true },
    },
  },
});
