import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { renderWithRouter } from '../../test/test-utils';

import DataForgePage from '../DataForgePage';

describe('DataForgePage', () => {
  it('renders data forge header', async () => {
    renderWithRouter(<DataForgePage />);
    expect(screen.getByText(/Test Data Forge/i)).toBeInTheDocument();
  });

  it('shows empty state when no configs', async () => {
    renderWithRouter(<DataForgePage />);
    await waitFor(() => {
      expect(screen.getByText(/No Configurations/i)).toBeInTheDocument();
    });
  });
});
