import { defineConfig } from '@playwright/test'
import application from '../../playwright.config'
import harness from './playwright.config'

export default defineConfig({
  ...application,
  testDir: '.',
  testMatch: 'real-model.spec.ts',
  testIgnore: [],
  outputDir: '../../node_modules/.cache/auto-real-results',
  use: { ...application.use, baseURL: 'http://127.0.0.1:4174' },
  webServer: {
    ...harness.webServer,
    command: 'npm run prepare:vision && npx vite build --config tests/auto-ui/vite.config.ts --mode real-model && npx vite preview --config tests/auto-ui/vite.config.ts --mode real-model --host 127.0.0.1 --port 4174 --strictPort',
  },
})
