import { readFileSync } from 'node:fs';
import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// The SPA's own version, baked in at build time. The version the UI shows otherwise comes from
// the core's /api/health, so a web-only release was invisible in the running app.
const pkg = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8')) as { version?: string };
const WEB_VERSION = pkg.version ?? 'dev';

// Dev proxy target. Override with JARVIS_BACKEND=http://127.0.0.1:9021 to hit `npm run mock`.
const BACKEND = process.env.JARVIS_BACKEND ?? 'http://127.0.0.1:9020';

export default defineConfig({
  base: '/',
  define: { __WEB_VERSION__: JSON.stringify(WEB_VERSION) },
  plugins: [react()],
  server: {
    port: 5173,
    strictPort: false,
    proxy: {
      '/api': { target: BACKEND, changeOrigin: true },
      '/ws': { target: BACKEND, ws: true, changeOrigin: true },
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
    target: 'es2022',
  },
  test: {
    environment: 'node',
    include: ['src/**/*.test.ts'],
  },
});
