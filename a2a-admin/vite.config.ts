import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  base: './',
  build: {
    outDir: '../data/web',
    assetsDir: 'assets',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/v2': {
        target: 'http://localhost:8321',
        changeOrigin: true,
        ws: true,
      },
      '/health': {
        target: 'http://localhost:8321',
        changeOrigin: true,
      },
      '/api': {
        target: 'http://localhost:8321',
        changeOrigin: true,
      },
      '/admin': {
        target: 'http://localhost:8321',
        changeOrigin: true,
      },
    },
  },
})