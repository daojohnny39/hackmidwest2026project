import { defineConfig } from '@playwright/test'
import application from '../../playwright.config'
export default defineConfig({
  testDir: '.', testMatch: ['capture.spec.ts', 'studio.spec.ts'], timeout: 30_000, workers: 1,
  outputDir: '../../node_modules/.cache/auto-ui-results',
  use: { baseURL: 'http://127.0.0.1:4174', screenshot: 'only-on-failure' },
  projects: application.projects,
  webServer: {
    command: 'npx vite build --config tests/auto-ui/vite.config.ts && npx vite preview --config tests/auto-ui/vite.config.ts --host 127.0.0.1 --port 4174 --strictPort',
    cwd: new URL('../../', import.meta.url).pathname,
    url: 'http://127.0.0.1:4174', reuseExistingServer: false,
  },
})
