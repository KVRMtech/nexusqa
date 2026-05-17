import { test as base, expect } from '@playwright/test';
import { LoginPage } from './pages/login.page';
import { SessionCommandPage } from './pages/session-command.page';
import { CanonicalResultPage } from './pages/canonical-result.page';
import { AppLayout } from './pages/app-layout.page';

/** Test credentials — override via env vars for real backend */
export const TEST_USER = {
  email: process.env.E2E_EMAIL || 'admin@company.com',
  password: process.env.E2E_PASSWORD || 'admin123',
};

/** Extended test fixtures with page objects */
type Fixtures = {
  loginPage: LoginPage;
  sessionCommandPage: SessionCommandPage;
  canonicalResultPage: CanonicalResultPage;
  appLayout: AppLayout;
};

export const test = base.extend<Fixtures>({
  loginPage: async ({ page }, use) => {
    await use(new LoginPage(page));
  },
  sessionCommandPage: async ({ page }, use) => {
    await use(new SessionCommandPage(page));
  },
  canonicalResultPage: async ({ page }, use) => {
    await use(new CanonicalResultPage(page));
  },
  appLayout: async ({ page }, use) => {
    await use(new AppLayout(page));
  },
});

export { expect };
