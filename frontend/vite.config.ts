/// <reference types="vitest/config" />
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// En développement, Vite relaie /api et /health vers le backend FastAPI : le
// navigateur reste en same-origin et le backend n'a besoin d'aucun CORS.
// VITE_PROXY_TARGET vaut http://api:8000 dans Docker Compose.
const proxyTarget = process.env.VITE_PROXY_TARGET ?? 'http://localhost:8010'

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': { target: proxyTarget, changeOrigin: true },
      '/health': { target: proxyTarget, changeOrigin: true },
    },
  },
  test: {
    environment: 'jsdom',
    globals: false,
    setupFiles: ['./src/test/setup.ts'],
    css: false,
  },
})
