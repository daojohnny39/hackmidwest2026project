import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// /api goes to the pipeline's search API (`python -m pipeline serve`). Same-origin requests keep the
// page's connect-src 'self'. `vite preview` uses the same proxy.
const api = process.env.CYBERPI_API_URL ?? 'http://127.0.0.1:8000'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  server: { proxy: { '/api': api } },
})
