import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// Dev proxy target. Override with JARVIS_BACKEND=http://127.0.0.1:9021 to hit `npm run mock`.
const BACKEND = process.env.JARVIS_BACKEND ?? 'http://127.0.0.1:9020';

export default defineConfig({
  base: '/',
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
