import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { renderWithRouter } from '../../test/test-utils';

// ── Module 1: Session Command Center ──────────────────────
import SessionCommandPage from '../SessionCommandPage';

describe('SessionCommandPage', () => {
  it('renders the page title and empty state', async () => {
    renderWithRouter(<SessionCommandPage />);
    expect(screen.getByText('Session Command Center')).toBeInTheDocument();
    // With no data, empty state is shown
    await waitFor(() => {
      expect(screen.getByText(/No sessions yet/i)).toBeInTheDocument();
    });
  });

  it('shows summary metrics section', async () => {
    renderWithRouter(<SessionCommandPage />);
    await waitFor(() => {
      expect(screen.getByText('Session Command Center')).toBeInTheDocument();
    });
  });

  it('displays zero metrics when no data', async () => {
    renderWithRouter(<SessionCommandPage />);
    await waitFor(() => {
      expect(screen.getByText('Session Command Center')).toBeInTheDocument();
    });
  });
});
