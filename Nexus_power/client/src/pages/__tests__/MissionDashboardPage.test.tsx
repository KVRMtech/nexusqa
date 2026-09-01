import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { renderWithRouter } from '../../test/test-utils';

import MissionDashboardPage from '../MissionDashboardPage';

describe('MissionDashboardPage', () => {
  it('renders page header', async () => {
    renderWithRouter(<MissionDashboardPage />);
    expect(screen.getByText(/Mission Control/i)).toBeInTheDocument();
  });

  it('renders create mission button', async () => {
    renderWithRouter(<MissionDashboardPage />);
    expect(screen.getByText(/Create Mission/i)).toBeInTheDocument();
  });

  it('shows empty state when no missions', async () => {
    renderWithRouter(<MissionDashboardPage />);
    await waitFor(() => {
      expect(screen.getByText(/No missions yet/i)).toBeInTheDocument();
    });
  });

  it('renders search input', async () => {
    renderWithRouter(<MissionDashboardPage />);
    expect(screen.getByPlaceholderText(/Search missions/i)).toBeInTheDocument();
  });

  it('renders filter buttons', async () => {
    renderWithRouter(<MissionDashboardPage />);
    expect(screen.getByText(/Active/i)).toBeInTheDocument();
    expect(screen.getByText(/Draft/i)).toBeInTheDocument();
  });
});
