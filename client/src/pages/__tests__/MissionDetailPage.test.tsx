import { describe, it, expect, vi } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { renderWithRouter } from '../../test/test-utils';

// Mock react-router params
vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual('react-router-dom');
  return {
    ...actual,
    useParams: () => ({ missionId: 'm-test-123' }),
  };
});

import MissionDetailPage from '../MissionDetailPage';

describe('MissionDetailPage', () => {
  it('renders loading state initially', async () => {
    renderWithRouter(<MissionDetailPage />);
    await waitFor(() => {
      expect(screen.getByText(/Loading mission/i)).toBeInTheDocument();
    });
  });
});
