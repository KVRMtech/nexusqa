import { Page, Locator, expect } from '@playwright/test';

/**
 * Page Object: Session Command Page (Module 1)
 *
 * Route: /sessions
 * The main dashboard — lists KT sessions, handles media upload,
 * tracks canonical processing progress.
 */
export class SessionCommandPage {
  readonly page: Page;
  readonly heading: Locator;
  readonly uploadArea: Locator;
  readonly fileInput: Locator;
  readonly sessionList: Locator;
  readonly searchInput: Locator;
  readonly emptyState: Locator;
  readonly processingBanner: Locator;

  constructor(page: Page) {
    this.page = page;
    this.heading = page.locator('text=Session Command');
    this.uploadArea = page.locator('[class*="border-dashed"]').first();
    this.fileInput = page.locator('input[type="file"]').first();
    this.sessionList = page.locator('[class*="session"]').first();
    this.searchInput = page.locator('input[placeholder*="Search"], input[placeholder*="search"]').first();
    this.emptyState = page.locator('text=No sessions');
    this.processingBanner = page.locator('text=Processing');
  }

  async goto() {
    await this.page.goto('/sessions');
    await this.page.waitForLoadState('networkidle');
  }

  async expectLoaded() {
    // The page should show Session Command heading or sessions list
    await expect(this.page).toHaveURL(/\/sessions/);
  }

  async getSessionCount(): Promise<number> {
    // Count session cards/rows visible on the page
    const sessions = this.page.locator('[data-status], [class*="session-card"]');
    return sessions.count();
  }

  async uploadAudioFile(filePath: string) {
    await this.fileInput.setInputFiles(filePath);
  }

  async clickSession(index: number) {
    const sessions = this.page.locator('[data-status], [class*="session-card"]');
    await sessions.nth(index).click();
  }

  async waitForProcessingComplete(timeout = 120_000) {
    // Wait for a session status to change to completed or needs_review
    await this.page.locator('text=Completed, text=EXCELLENT, text=GOOD, text=FAIR').first().waitFor({
      state: 'visible',
      timeout,
    });
  }
}
