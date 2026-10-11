import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './tests',
  testIgnore: ['**/unit/**', '**/auto-ui/**'],
  timeout: 90_000,
  workers: 1,
  use: {
    baseURL: 'http://127.0.0.1:4173',
    screenshot: 'only-on-failure',
  },
  projects: [
    {
      name: 'chrome',
      use: {
        browserName: 'chromium',
        channel: 'chrome',
        launchOptions: {
          args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'],
        },
      },
    },
    {
      name: 'firefox',
      use: {
        browserName: 'firefox',
        launchOptions: {
          executablePath: process.env.FIREFOX_EXECUTABLE_PATH,
          firefoxUserPrefs: {
            'media.navigator.streams.fake': true,
            'media.navigator.permission.disabled': true,
          },
        },
      },
    },
    // WebKit is Safari's engine; it is not the installed Safari application.
    { name: 'webkit', use: { browserName: 'webkit', permissions: ['camera'] } },
    {
      name: 'edge',
      use: {
        browserName: 'chromium',
        channel: 'msedge',
        launchOptions: {
          executablePath: process.env.EDGE_EXECUTABLE_PATH,
          args: ['--use-fake-device-for-media-stream', '--use-fake-ui-for-media-stream'],
        },
      },
    },
  ],
  webServer: {
    // Verify the shipped bundle and local model assets, not just Vite's dev mode.
    command: 'npm run build && npm run preview -- --host 127.0.0.1 --port 4173 --strictPort',
    url: 'http://127.0.0.1:4173',
    reuseExistingServer: false,
  },
})
