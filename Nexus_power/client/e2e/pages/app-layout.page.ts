import { Page, Locator, expect } from '@playwright/test';

/**
 * Page Object: App Layout (navigation sidebar + top bar)
 *
 * Present on all authenticated pages.
 */
export class AppLayout {
  readonly page: Page;
  readonly sidebar: Locator;
  readonly topBar: Locator;
  readonly mobileMenuButton: Locator;
  readonly userMenu: Locator;
  readonly signOutButton: Locator;
  readonly engineStatusIndicator: Locator;

  constructor(page: Page) {
    this.page = page;
    this.sidebar = page.locator('nav, [role="navigation"]').first();
    this.topBar = page.locator('header').first();
    this.mobileMenuButton = page.locator('[aria-label="Menu"], button:has-text("Menu")').first();
    this.userMenu = page.locator('[data-testid="user-menu"]').or(
      page.locator('button:has-text("Sign out"), button:has-text("Logout")').first()
    );
    this.signOutButton = page.locator('button:has-text("Sign out"), button:has-text("Logout")').first();
    this.engineStatusIndicator = page.locator('text=/\\d+.*Engine/i').first();
  }

  /** Navigate to a page via sidebar link */
  async navigateTo(label: string) {
    // On mobile, open sidebar first
    const viewport = this.page.viewportSize();
    if (viewport && viewport.width < 1024) {
      await this.mobileMenuButton.click();
      await this.page.waitForTimeout(300); // sidebar animation
    }
    await this.page.locator(`a:has-text("${label}"), [role="link"]:has-text("${label}")`).first().click();
    await this.page.waitForLoadState('networkidle');
  }

  async expectSidebarVisible() {
    await expect(this.sidebar).toBeVisible();
  }

  async signOut() {
    await this.signOutButton.click();
    await this.page.waitForURL('**/login');
  }

  /** Get all navigation link labels */
  async getNavLinks(): Promise<string[]> {
    const links = this.page.locator('nav a, [role="navigation"] a');
    const count = await links.count();
    const labels: string[] = [];
    for (let i = 0; i < count; i++) {
      const text = await links.nth(i).textContent();
      if (text?.trim()) labels.push(text.trim());
    }
    return labels;
  }
}
