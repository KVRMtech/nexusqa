import { test, expect, TEST_USER } from './fixtures';

test.describe('Navigation', () => {
  test.beforeEach(async ({ loginPage }) => {
    await loginPage.goto();
    await loginPage.login(TEST_USER.email, TEST_USER.password);
    await loginPage.expectRedirectToDashboard();
  });

  test('should display sidebar navigation', async ({ appLayout }) => {
    await appLayout.expectSidebarVisible();
  });

  test('should navigate to Sessions page via sidebar', async ({ appLayout, page }) => {
    await appLayout.navigateTo('Session Command');
    await expect(page).toHaveURL(/\/sessions/);
  });

  test('should show 404 page for unknown route', async ({ page }) => {
    await page.goto('/some-nonexistent-page');
    await expect(page.locator('text=/not found|404/i')).toBeVisible({ timeout: 10_000 });
  });

  test('sidebar nav links should be accessible', async ({ appLayout }) => {
    const links = await appLayout.getNavLinks();
    expect(links.length).toBeGreaterThan(0);
  });
});

test.describe('Navigation — mobile viewport', () => {
  test.use({ viewport: { width: 375, height: 812 } });

  test.beforeEach(async ({ loginPage }) => {
    await loginPage.goto();
    await loginPage.login(TEST_USER.email, TEST_USER.password);
    await loginPage.expectRedirectToDashboard();
  });

  test('should open mobile menu drawer', async ({ appLayout }) => {
    await appLayout.mobileMenuButton.click();
    await appLayout.expectSidebarVisible();
  });
});
