import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8100',
      '/health': 'http://127.0.0.1:8100',
      '/evidence': 'http://127.0.0.1:8100',
      '/ws': { target: 'ws://127.0.0.1:8100', ws: true }
    }
  }
})

