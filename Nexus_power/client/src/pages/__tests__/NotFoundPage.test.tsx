import { describe, it, expect } from 'vitest';
import { screen } from '@testing-library/react';
import { renderWithRouter } from '../../test/test-utils';
import NotFoundPage from '../NotFoundPage';

describe('NotFoundPage', () => {
  it('renders 404 heading', () => {
    renderWithRouter(<NotFoundPage />);
    expect(screen.getByText('404')).toBeInTheDocument();
  });

  it('shows descriptive message', () => {
    renderWithRouter(<NotFoundPage />);
    expect(screen.getByText('Page not found')).toBeInTheDocument();
  });

  it('has a Go to Dashboard button', () => {
    renderWithRouter(<NotFoundPage />);
    const btn = screen.getByRole('button', { name: /dashboard/i });
    expect(btn).toBeInTheDocument();
  });
});
