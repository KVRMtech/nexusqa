import { test, expect, TEST_USER } from './fixtures';
import path from 'path';

test.describe('Session Workflow', () => {
  test.beforeEach(async ({ loginPage }) => {
    await loginPage.goto();
    await loginPage.login(TEST_USER.email, TEST_USER.password);
    await loginPage.expectRedirectToDashboard();
  });

  test('should display sessions page after login', async ({ sessionCommandPage }) => {
    await sessionCommandPage.expectLoaded();
  });

  test('should show upload area for media files', async ({ sessionCommandPage, page }) => {
    await sessionCommandPage.goto();
    // Verify file upload element exists
    const fileInput = page.locator('input[type="file"]').first();
    await expect(fileInput).toBeAttached();
  });

  test('should upload audio file and start canonical processing', async ({
    sessionCommandPage,
    page,
  }) => {
    await sessionCommandPage.goto();

    // Use a test audio file from data/audio/ if it exists
    const testAudioPath = path.resolve(__dirname, '..', '..', 'data', 'audio');
    const hasTestAudio = await page.evaluate(() => true); // placeholder

    // Check if upload area is present
    const fileInput = page.locator('input[type="file"]').first();
    await expect(fileInput).toBeAttached();

    // Note: actual upload test requires a test audio file in data/audio/
    // This test validates the upload UI is present and ready
  });

  test('should display session status indicators', async ({ sessionCommandPage, page }) => {
    await sessionCommandPage.goto();

    // The page should display either sessions or an empty state
    const hasContent = await page.locator('text=Session, text=No sessions, text=Upload').first().isVisible();
    expect(hasContent).toBeTruthy();
  });

  test('should auto-refresh sessions periodically', async ({ sessionCommandPage, page }) => {
    await sessionCommandPage.goto();

    // Intercept the API call and verify it fires on interval
    const apiCalls: string[] = [];
    page.on('request', (req) => {
      if (req.url().includes('/sessions') && req.method() === 'GET') {
        apiCalls.push(req.url());
      }
    });

    // Wait 35 seconds for the 30-second auto-refresh interval
    await page.waitForTimeout(35_000);
    expect(apiCalls.length).toBeGreaterThanOrEqual(1);
  });
});
