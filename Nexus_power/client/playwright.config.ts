import { defineConfig, devices } from '@playwright/test';

/**
 * Playwright E2E Test Configuration for NexusQA.
 *
 * Prerequisites:
 *   1. Backend services running (python scripts/start_all.py)
 *   2. Vite dev server running (cd client && npm run dev)
 *
 * Run:
 *   npx playwright test                   # headless
 *   npx playwright test --ui              # interactive UI mode
 *   npx playwright test --headed          # watch the browser
 *   npx playwright test --project=mobile  # mobile viewport
 */
export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,           // serialize — sessions share state
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 1 : 0,
  workers: 1,                     // single worker for predictable ordering
  reporter: [
    ['html', { open: 'never' }],
    ['list'],
  ],
  timeout: 60_000,                // 60s per test (some pages lazy-load)
  expect: {
    timeout: 10_000,
  },

  use: {
    baseURL: 'http://localhost:3000',
    trace: 'on-first-retry',
    screenshot: 'only-on-failure',
    video: 'retain-on-failure',
    actionTimeout: 10_000,
  },

  projects: [
    {
      name: 'desktop',
      use: { ...devices['Desktop Chrome'] },
    },
    {
      name: 'mobile',
      use: { ...devices['Pixel 5'] },
    },
  ],

  /* Start Vite dev server before tests if not already running */
  webServer: {
    command: 'npm run dev',
    url: 'http://localhost:3000',
    reuseExistingServer: true,
    timeout: 30_000,
  },
});
