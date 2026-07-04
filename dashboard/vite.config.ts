import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In production the reverse proxy serves the API under /api and strips the
// prefix. The dev server mirrors that so local development matches production.
export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      "/api": {
        target: "http://localhost:8000",
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ""),
      },
    },
  },
});
