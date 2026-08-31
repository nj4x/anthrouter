import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

export default defineConfig({
  plugins: [react()],
  base: '/ui/',
  build: { outDir: 'dist' },
  server: {
    proxy: { '/admin': 'http://127.0.0.1:8083' },
  },
})
