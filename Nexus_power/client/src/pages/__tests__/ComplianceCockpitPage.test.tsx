import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { renderWithRouter } from '../../test/test-utils';

import ComplianceCockpitPage from '../ComplianceCockpitPage';

describe('ComplianceCockpitPage', () => {
  it('renders compliance dashboard', async () => {
    renderWithRouter(<ComplianceCockpitPage />);
    expect(screen.getByText(/Compliance Cockpit/i)).toBeInTheDocument();
    await waitFor(() => {
      expect(screen.getByText(/Avg Compliance/i)).toBeInTheDocument();
    });
  });

  it('shows empty state when no jurisdictions', async () => {
    renderWithRouter(<ComplianceCockpitPage />);
    await waitFor(() => {
      expect(screen.getByText(/No Jurisdictions Configured/i)).toBeInTheDocument();
    });
  });
});
