import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// Dev: `npm run dev` on the host, backend from `docker compose up` on :8000.
// Prod: the built `dist/` is served by FastAPI from the same origin, so no proxy is needed.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    // Fixtures are imported from ../contracts (see src/viewer/fixtures.ts).
    fs: { allow: [".."] },
    proxy: {
      "/api": { target: process.env.VITE_API_TARGET ?? "http://localhost:8000", changeOrigin: true },
    },
  },
});
