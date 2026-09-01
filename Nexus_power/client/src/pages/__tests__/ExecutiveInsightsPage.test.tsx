import { describe, it, expect } from 'vitest';
import { screen } from '@testing-library/react';
import { renderWithRouter } from '../../test/test-utils';

import ExecutiveInsightsPage from '../ExecutiveInsightsPage';

describe('ExecutiveInsightsPage', () => {
  it('renders executive dashboard', () => {
    renderWithRouter(<ExecutiveInsightsPage />);
    expect(screen.getAllByText(/Executive Insights/i).length).toBeGreaterThanOrEqual(1);
  });

  it('shows empty state when no data', () => {
    renderWithRouter(<ExecutiveInsightsPage />);
    expect(screen.getByText(/No Data Available/i)).toBeInTheDocument();
  });

  it('shows engine status grid', () => {
    renderWithRouter(<ExecutiveInsightsPage />);
    expect(screen.getByText('Engine Status')).toBeInTheDocument();
  });
});
