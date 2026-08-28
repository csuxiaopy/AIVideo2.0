import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

// 开发环境下代理后端地址，可通过环境变量覆盖（Docker 容器内指向 app:8100）
const apiTarget = process.env.VITE_API_TARGET || 'http://127.0.0.1:8100'

export default defineConfig({
  plugins: [vue()],
  server: {
    host: true,
    port: 5173,
    proxy: {
      '/api': apiTarget,
      '/health': apiTarget,
      '/evidence': apiTarget,
      '/ws': { target: apiTarget.replace('http', 'ws'), ws: true }
    }
  }
})

