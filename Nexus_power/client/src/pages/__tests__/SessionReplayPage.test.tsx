import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { renderWithRouter } from '../../test/test-utils';

import SessionReplayPage from '../SessionReplayPage';

describe('SessionReplayPage', () => {
  it('renders breadcrumb and timeline', async () => {
    renderWithRouter(<SessionReplayPage />);
    expect(screen.getByText(/Back to Sessions/i)).toBeInTheDocument();
  });

  it('shows empty state when no events loaded', async () => {
    renderWithRouter(<SessionReplayPage />);
    await waitFor(() => {
      expect(screen.getByText(/No Intelligence Events/i)).toBeInTheDocument();
    });
  });
});
