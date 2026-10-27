import vue from '@vitejs/plugin-vue'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      // 把 /api 转发到后端（uvicorn api:app --port 8000），开发期免去 CORS 配置。
      '/api': {
        target: 'http://127.0.0.1:8000',
        changeOrigin: true,
        // 后端路由本身带 /api 前缀，因此不做路径重写。
        ws: false,
      },
    },
  },
})
