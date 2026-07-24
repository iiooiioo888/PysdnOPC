import { defineConfig, devices } from '@playwright/test'

/**
 * Playwright Visual Snapshot Testing Configuration
 *
 * Runs visual regression tests against the Office UI frontend.
 * Uses Vite dev server with mocked WebSocket for deterministic rendering.
 *
 * Usage:
 *   npm run test:visual          — run visual tests (compare against baselines)
 *   npm run test:visual:update   — update baseline snapshots
 */
export default defineConfig({
  testDir: './visual-tests',
  outputDir: './visual-tests/test-results',
  snapshotDir: './visual-tests/snapshots',
  snapshotPathTemplate: '{snapshotDir}/{testFilePath}/{arg}{ext}',

  fullyParallel: false,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,
  reporter: process.env.CI ? 'github' : 'html',

  expect: {
    toHaveScreenshot: {
      maxDiffPixelRatio: 0.01,
      animations: 'disabled',
      caret: 'hide',
    },
  },

  use: {
    baseURL: 'http://localhost:5199',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
  },

  projects: [
    {
      name: 'desktop-chrome',
      use: {
        ...devices['Desktop Chrome'],
        viewport: { width: 1440, height: 900 },
      },
    },
  ],

  webServer: {
    command: 'npx vite --port 5199 --strictPort',
    port: 5199,
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
})
