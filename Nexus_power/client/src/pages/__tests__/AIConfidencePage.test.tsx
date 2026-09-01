import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { renderWithRouter } from '../../test/test-utils';

import AIConfidencePage from '../AIConfidencePage';

describe('AIConfidencePage', () => {
  it('renders guardrail pipeline stages', async () => {
    renderWithRouter(<AIConfidencePage />);
    expect(screen.getByText(/AI Confidence/i)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByText('Guardrail Pipeline')).toBeInTheDocument();
    });
  });

  it('shows trust score trend', async () => {
    renderWithRouter(<AIConfidencePage />);
    await waitFor(() => {
      expect(screen.getByText(/Trust Score/i)).toBeInTheDocument();
    });
  });
});
