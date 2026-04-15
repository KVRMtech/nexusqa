import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { renderWithRouter } from '../../test/test-utils';

import KnowledgeGraphPage from '../KnowledgeGraphPage';

describe('KnowledgeGraphPage', () => {
  it('renders page title and graph stats', async () => {
    renderWithRouter(<KnowledgeGraphPage />);
    expect(screen.getAllByText(/Knowledge Graph/i).length).toBeGreaterThanOrEqual(1);
    await waitFor(() => {
      expect(screen.getAllByText(/\d+ nodes/).length).toBeGreaterThanOrEqual(1);
    });
  });

  it('shows NL query input', () => {
    renderWithRouter(<KnowledgeGraphPage />);
    expect(screen.getByPlaceholderText(/Ask anything/i)).toBeInTheDocument();
  });
});
