import { defineConfig } from 'vite';
import { catalogTiers } from './src/i18n/catalog-tiers.js';
export default defineConfig({
  base: './',
  plugins: [catalogTiers()],
  server: { proxy: { '/v1': 'http://127.0.0.1:8845', '/api/auth': 'http://127.0.0.1:8845' } },
  build: { outDir: '../series', emptyOutDir: false },
});
