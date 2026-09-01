import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { renderWithRouter } from '../../test/test-utils';

import TestExecutionCenterPage from '../TestExecutionCenterPage';

describe('TestExecutionCenterPage', () => {
  it('renders page with summary stats', async () => {
    renderWithRouter(<TestExecutionCenterPage />);
    expect(screen.getByText(/Test Execution Center/i)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByText(/Pass Rate/i)).toBeInTheDocument();
    });
  });

  it('shows empty state when no runs', async () => {
    renderWithRouter(<TestExecutionCenterPage />);
    await waitFor(() => {
      expect(screen.getByText(/No Execution Runs/i)).toBeInTheDocument();
    });
  });
});
