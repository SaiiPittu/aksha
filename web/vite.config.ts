import path from "node:path"
import tailwindcss from "@tailwindcss/vite"
import react from "@vitejs/plugin-react"
import { defineConfig } from "vite"

// The API server: `aksha serve --port 8787`. The dev server proxies /api to it, so the
// session cookie is same-origin in development exactly as it is in production.
const API_TARGET = process.env.AKSHA_API ?? "http://127.0.0.1:8787"

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: { alias: { "@": path.resolve(import.meta.dirname, "./src") } },
  server: {
    proxy: { "/api": { target: API_TARGET, changeOrigin: false } },
  },
  build: { outDir: "dist", emptyOutDir: true },
})
