import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { renderWithRouter } from '../../test/test-utils';

// ── Module 1: Knowledge Processing ────────────────────────
// The page's heading is "Knowledge Processing" (PageHeader, SessionCommandPage
// line ~1043). Every test in this file used to assert the string
// "Session Command Center", which is the page's FORMER name — the component was
// retitled and its tests were not, so all three failed on a stale literal while
// the page itself was fine. The file name and the store keep the old identifier;
// only the displayed title changed.
import SessionCommandPage from '../SessionCommandPage';

describe('SessionCommandPage', () => {
  it('renders the page title and empty state', async () => {
    renderWithRouter(<SessionCommandPage />);
    expect(screen.getByText('Knowledge Processing')).toBeInTheDocument();
    // With no data, empty state is shown
    await waitFor(() => {
      expect(screen.getByText(/No sessions yet/i)).toBeInTheDocument();
    });
  });

  // Previously this only re-asserted the page title, so it passed without ever
  // touching a metric. It now asserts the four StatCards the section actually
  // renders, which is what its name claims.
  it('shows summary metrics section', async () => {
    renderWithRouter(<SessionCommandPage />);
    await waitFor(() => {
      expect(screen.getByText('Knowledge Processing')).toBeInTheDocument();
    });
    expect(screen.getByText('Live Now')).toBeInTheDocument();
    expect(screen.getByText('Processing')).toBeInTheDocument();
    expect(screen.getByText('Completed')).toBeInTheDocument();
    expect(screen.getByText('Insights Extracted')).toBeInTheDocument();
  });

  // Likewise vacuous before. Reads the value out of each card's own container
  // rather than counting "0" across the whole document, so an unrelated zero
  // elsewhere on the page cannot make this pass.
  it('displays zero metrics when no data', async () => {
    renderWithRouter(<SessionCommandPage />);
    await waitFor(() => {
      expect(screen.getByText('Knowledge Processing')).toBeInTheDocument();
    });

    for (const label of ['Live Now', 'Processing', 'Completed', 'Insights Extracted']) {
      const card = screen.getByText(label).closest('.stat-card');
      expect(card).not.toBeNull();
      // The value lives in its own span (StatCard uses `tabular-nums` on it).
      // Read that rather than the card's textContent, which concatenates to
      // "Live Now0" and would need a fuzzy match to pass.
      expect(card?.querySelector('.tabular-nums')).toHaveTextContent('0');
    }
  });
});
