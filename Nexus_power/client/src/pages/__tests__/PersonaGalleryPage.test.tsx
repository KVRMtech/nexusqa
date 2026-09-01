import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { renderWithRouter } from '../../test/test-utils';

import PersonaGalleryPage from '../PersonaGalleryPage';

describe('PersonaGalleryPage', () => {
  it('renders page header', async () => {
    renderWithRouter(<PersonaGalleryPage />);
    expect(screen.getByText(/Persona Gallery/i)).toBeInTheDocument();
  });

  it('shows empty state when no personas', async () => {
    renderWithRouter(<PersonaGalleryPage />);
    await waitFor(() => {
      expect(screen.getByText(/No personas available/i)).toBeInTheDocument();
    });
  });
});
