import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// §7 约定：构建产物输出至 frontend/dist，由后端 FastAPI 静态托管
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target: 'http://127.0.0.1:8080', changeOrigin: true },
      '/ws': { target: 'ws://127.0.0.1:8080', ws: true },
    },
  },
  build: { outDir: 'dist', emptyOutDir: true },
})
