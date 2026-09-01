import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { renderWithRouter } from '../../test/test-utils';

import ContradictionRadarPage from '../ContradictionRadarPage';

describe('ContradictionRadarPage', () => {
  it('renders radar header and severity breakdown', async () => {
    renderWithRouter(<ContradictionRadarPage />);
    expect(screen.getByText(/Contradiction Radar/i)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByText('Critical')).toBeInTheDocument();
    });
  });

  it('displays empty state when no contradictions', async () => {
    renderWithRouter(<ContradictionRadarPage />);
    await waitFor(() => {
      expect(screen.getByText(/No Contradictions Detected/i)).toBeInTheDocument();
    });
  });
});
