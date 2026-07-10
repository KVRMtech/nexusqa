import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import path from 'path';

// VKPower Verdict — SEPARATE production frontend for the QE-Central plane.
// Distinct app, distinct port (5273) from Nexus_power/client (3000).
// The API base URL is env-driven (VITE_QEC_API_URL); in dev we proxy the
// qe-central routes (/api, /health, /webhooks) to the service on :8093 so the
// portal runs against a live backend without CORS. In mock mode
// (VITE_QEC_MOCK=1) no network is used at all.
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, './src'),
    },
  },
  server: {
    port: 5273,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8093', changeOrigin: true },
      '/health': { target: 'http://127.0.0.1:8093', changeOrigin: true },
      '/webhooks': { target: 'http://127.0.0.1:8093', changeOrigin: true },
    },
  },
  preview: {
    port: 5273,
  },
  build: {
    outDir: 'dist',
    sourcemap: true,
  },
  test: {
    globals: true,
    environment: 'jsdom',
    css: true,
    include: ['src/**/*.{test,spec}.{ts,tsx}'],
  },
});
