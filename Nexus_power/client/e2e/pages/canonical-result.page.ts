import { Page, Locator, expect } from '@playwright/test';

/**
 * Page Object: Canonical Result Page
 *
 * Route: /sessions/:sessionId/canonical
 * Displays quality gate results, provenance, timeline, and action cards.
 */
export class CanonicalResultPage {
  readonly page: Page;
  readonly qualityBadge: Locator;
  readonly provenanceBadge: Locator;
  readonly overallScore: Locator;
  readonly timelineSection: Locator;
  readonly actionCards: Locator;
  readonly transcriptSection: Locator;
  readonly visualSection: Locator;
  readonly shareButton: Locator;
  readonly exportButton: Locator;

  constructor(page: Page) {
    this.page = page;
    this.qualityBadge = page.locator('[data-testid="quality-badge"]').or(
      page.locator('text=EXCELLENT, text=GOOD, text=FAIR, text=NEEDS REVIEW, text=FAILED').first()
    );
    this.provenanceBadge = page.locator('[data-testid="provenance-badge"]').or(
      page.locator('text=Fresh, text=Cached').first()
    );
    this.overallScore = page.locator('text=/\\d+%/').first();
    this.timelineSection = page.locator('[data-testid="processing-timeline"]').or(
      page.locator('text=Processing Timeline, text=Pipeline').first()
    );
    this.actionCards = page.locator('[data-testid="action-card"]');
    this.transcriptSection = page.locator('text=Transcript, text=transcript').first();
    this.visualSection = page.locator('text=Visual, text=visual, text=Screens').first();
    this.shareButton = page.locator('button:has-text("Share")');
    this.exportButton = page.locator('button:has-text("Export")');
  }

  async goto(sessionId: string) {
    await this.page.goto(`/sessions/${sessionId}/canonical`);
    await this.page.waitForLoadState('networkidle');
  }

  async expectQualityGradeVisible() {
    await expect(this.qualityBadge).toBeVisible({ timeout: 15_000 });
  }

  async getQualityGrade(): Promise<string> {
    const text = await this.qualityBadge.textContent();
    return text?.trim() || '';
  }

  async expectTimelineVisible() {
    await expect(this.timelineSection).toBeVisible();
  }
}
