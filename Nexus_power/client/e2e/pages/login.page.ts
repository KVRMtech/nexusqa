import { Page, Locator, expect } from '@playwright/test';

/**
 * Page Object: Login Page
 *
 * Route: /login
 * Elements: email input, password input, submit button, error banner
 */
export class LoginPage {
  readonly page: Page;
  readonly emailInput: Locator;
  readonly passwordInput: Locator;
  readonly submitButton: Locator;
  readonly errorBanner: Locator;
  readonly registerLink: Locator;
  readonly engineStatusBar: Locator;

  constructor(page: Page) {
    this.page = page;
    this.emailInput = page.locator('input#email');
    this.passwordInput = page.locator('input#password');
    this.submitButton = page.locator('button[type="submit"]');
    this.errorBanner = page.locator('.bg-red-500\\/10');
    this.registerLink = page.locator('a:has-text("Register here")');
    this.engineStatusBar = page.locator('text=Engines Ready');
  }

  async goto() {
    await this.page.goto('/login');
    await this.emailInput.waitFor({ state: 'visible' });
  }

  async login(email: string, password: string) {
    await this.emailInput.fill(email);
    await this.passwordInput.fill(password);
    await this.submitButton.click();
  }

  async expectError(message?: string) {
    await expect(this.errorBanner).toBeVisible();
    if (message) {
      await expect(this.errorBanner).toContainText(message);
    }
  }

  async expectRedirectToDashboard() {
    await this.page.waitForURL('**/sessions', { timeout: 15_000 });
  }
}
