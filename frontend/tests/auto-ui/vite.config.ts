import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { fileURLToPath } from 'node:url'

// Exercise the actual application. Only deterministic tests substitute the detector.
export default defineConfig(({ mode }) => ({
  plugins: [react()],
  publicDir: mode === 'real-model' ? 'public' : false,
  resolve: {
    alias: {
      ...(mode === 'real-model' ? {} : { '@mediapipe/tasks-vision': fileURLToPath(new URL('./vision.ts', import.meta.url)) }),
    },
  },
  build: { outDir: 'node_modules/.cache/front-capture-test', emptyOutDir: true },
}))
