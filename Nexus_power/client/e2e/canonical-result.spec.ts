import { test, expect, TEST_USER } from './fixtures';

test.describe('Canonical Result Page', () => {
  // These tests require a completed session with an artifact.
  // If no session exists, they will be skipped gracefully.

  test.beforeEach(async ({ loginPage }) => {
    await loginPage.goto();
    await loginPage.login(TEST_USER.email, TEST_USER.password);
    await loginPage.expectRedirectToDashboard();
  });

  test('should navigate to canonical result from completed session', async ({ page }) => {
    await page.goto('/sessions');
    await page.waitForLoadState('networkidle');

    // Look for a completed session with a "View" or canonical link
    const completedSession = page.locator('[data-status="completed"], :has-text("Completed")').first();
    const isVisible = await completedSession.isVisible().catch(() => false);

    if (!isVisible) {
      test.skip(true, 'No completed sessions available for canonical result test');
      return;
    }

    // Click into the completed session
    await completedSession.click();
    await page.waitForLoadState('networkidle');

    // Should show canonical result content
    await expect(page).toHaveURL(/\/canonical|\/sessions\//);
  });

  test('should display quality badge on canonical result page', async ({
    canonicalResultPage,
    page,
  }) => {
    // Navigate to a known session (if exists) — use first completed session
    await page.goto('/sessions');
    await page.waitForLoadState('networkidle');

    const viewLink = page.locator('a[href*="canonical"]').first();
    const hasLink = await viewLink.isVisible().catch(() => false);

    if (!hasLink) {
      test.skip(true, 'No canonical result links available');
      return;
    }

    await viewLink.click();
    await canonicalResultPage.expectQualityGradeVisible();
  });
});
