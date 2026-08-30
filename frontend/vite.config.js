import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: {
    // Proxy /api -> the FastAPI backend so frontend/src/api.js's relative
    // paths (single-origin mode, see backend/main.py) work under `npm run
    // dev` too - the browser only ever talks to the Vite origin, so CORS
    // never enters into it even when the two are on different hosts.
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
    // Off (loopback-only) by default, matching Vite's own default. Set
    // VITE_HOST=true (or a specific bind address) to reach the dev server
    // itself from another machine on the tailnet - e.g.
    // `VITE_HOST=0.0.0.0 npm run dev` - independent of the `--host` CLI flag,
    // which also still works.
    host: process.env.VITE_HOST || false,
  },
})
