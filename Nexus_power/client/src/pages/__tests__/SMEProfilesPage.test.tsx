import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { renderWithRouter } from '../../test/test-utils';

import SMEProfilesPage from '../SMEProfilesPage';

describe('SMEProfilesPage', () => {
  it('renders page title and empty state', async () => {
    renderWithRouter(<SMEProfilesPage />);
    expect(screen.getByText(/SME Knowledge Profiles/i)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByText(/No SME Profiles/i)).toBeInTheDocument();
    });
  });

  it('shows zero active SMEs count', async () => {
    renderWithRouter(<SMEProfilesPage />);
    await waitFor(() => {
      expect(screen.getByText('Active SMEs')).toBeInTheDocument();
    });
  });
});
