import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { renderWithRouter } from '../../test/test-utils';

import TraceabilityMatrixPage from '../TraceabilityMatrixPage';

describe('TraceabilityMatrixPage', () => {
  it('renders header and coverage summary', async () => {
    renderWithRouter(<TraceabilityMatrixPage />);
    expect(screen.getByText(/Traceability Matrix/i)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByText(/Full Coverage/i)).toBeInTheDocument();
    });
  });

  it('displays empty state when no traces', async () => {
    renderWithRouter(<TraceabilityMatrixPage />);
    await waitFor(() => {
      expect(screen.getByText(/No Trace Data/i)).toBeInTheDocument();
    });
  });
});
