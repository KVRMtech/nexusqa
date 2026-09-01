import { test, expect, TEST_USER } from './fixtures';

test.describe('Authentication', () => {
  test('should display the login page correctly', async ({ loginPage }) => {
    await loginPage.goto();

    await expect(loginPage.emailInput).toBeVisible();
    await expect(loginPage.passwordInput).toBeVisible();
    await expect(loginPage.submitButton).toBeVisible();
    await expect(loginPage.submitButton).toHaveText(/Sign in/);
    await expect(loginPage.registerLink).toBeVisible();
  });

  test('should show error on invalid credentials', async ({ loginPage }) => {
    await loginPage.goto();
    await loginPage.login('wrong@email.com', 'wrongpassword');
    await loginPage.expectError();
  });

  test('should redirect unauthenticated users to /login', async ({ page }) => {
    await page.goto('/sessions');
    await page.waitForURL('**/login', { timeout: 10_000 });
    await expect(page).toHaveURL(/\/login/);
  });

  test('should login successfully with valid credentials', async ({ loginPage }) => {
    await loginPage.goto();
    await loginPage.login(TEST_USER.email, TEST_USER.password);
    await loginPage.expectRedirectToDashboard();
  });

  test('should persist auth across page reload', async ({ loginPage, page }) => {
    await loginPage.goto();
    await loginPage.login(TEST_USER.email, TEST_USER.password);
    await loginPage.expectRedirectToDashboard();

    // Reload and expect to stay on sessions
    await page.reload();
    await page.waitForLoadState('networkidle');
    await expect(page).toHaveURL(/\/sessions/);
  });

  test('should logout and redirect to login', async ({ loginPage, appLayout, page }) => {
    await loginPage.goto();
    await loginPage.login(TEST_USER.email, TEST_USER.password);
    await loginPage.expectRedirectToDashboard();

    await appLayout.signOut();
    await expect(page).toHaveURL(/\/login/);
  });

  test('should navigate to register page', async ({ loginPage, page }) => {
    await loginPage.goto();
    await loginPage.registerLink.click();
    await expect(page).toHaveURL(/\/register/);
  });
});
