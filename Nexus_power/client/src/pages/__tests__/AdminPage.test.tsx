import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { renderWithRouter } from '../../test/test-utils';

import AdminPage from '../AdminPage';

describe('AdminPage', () => {
  it('renders system administration header', () => {
    renderWithRouter(<AdminPage />);
    expect(screen.getByText(/System Administration/i)).toBeInTheDocument();
  });

  it('shows empty state for engines tab', async () => {
    renderWithRouter(<AdminPage />);
    await waitFor(() => {
      expect(screen.getByText(/No Engines Registered/i)).toBeInTheDocument();
    });
  });

  it('displays tab navigation', () => {
    renderWithRouter(<AdminPage />);
    expect(screen.getByRole('tab', { name: /engines/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /integrations/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /users/i })).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: /audit/i })).toBeInTheDocument();
  });
});
