import { defineConfig } from 'vite';
export default defineConfig({
  base: './',
  server: { proxy: { '/v1': 'http://127.0.0.1:8845', '/api/auth': 'http://127.0.0.1:8845' } },
  build: { outDir: '../series', emptyOutDir: false },
});
