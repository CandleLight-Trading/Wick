import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// The dev server proxies /api and /ws to the FastAPI backend so the browser only ever
// talks to one origin and there is no CORS configuration anywhere.
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    proxy: {
      // Object form on purpose: the string shorthand sets changeOrigin, which rewrites the
      // Host header and makes the backend's same-origin check refuse every POST.
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: false },
      "/ws": { target: "ws://127.0.0.1:8000", ws: true },
    },
  },
});
