import { defineConfig } from 'vite';
import vue from '@vitejs/plugin-vue';

export default defineConfig({
  plugins: [vue()],
  base: './',
  server: {
    proxy: {
      '/api': 'http://127.0.0.1:8765',
      '/media': 'http://127.0.0.1:8765',
    },
  },
});
