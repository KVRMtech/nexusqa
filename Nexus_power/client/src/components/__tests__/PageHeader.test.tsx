import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { PageHeader } from '../PageHeader';

describe('PageHeader', () => {
  it('renders title and subtitle', () => {
    render(<PageHeader title="Test Title" subtitle="Test subtitle" />);
    expect(screen.getByText('Test Title')).toBeInTheDocument();
    expect(screen.getByText('Test subtitle')).toBeInTheDocument();
  });

  it('renders zone label when provided', () => {
    render(<PageHeader zone="ZONE 1" title="Title" />);
    expect(screen.getByText('ZONE 1')).toBeInTheDocument();
  });

  it('shows LiveBadge when isLive is true', () => {
    render(<PageHeader title="Live Page" isLive={true} />);
    expect(screen.getByText('Live')).toBeInTheDocument();
  });

  it('does not show LiveBadge when isLive is undefined', () => {
    render(<PageHeader title="Offline Page" />);
    expect(screen.queryByText('Live')).not.toBeInTheDocument();
    expect(screen.queryByText('Demo')).not.toBeInTheDocument();
  });

  it('renders action buttons via children', () => {
    render(
      <PageHeader title="With Actions">
        <button>Custom Action</button>
      </PageHeader>,
    );
    expect(screen.getByText('Custom Action')).toBeInTheDocument();
  });
});
