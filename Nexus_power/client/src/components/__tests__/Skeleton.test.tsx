import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import {
  Skeleton,
  StatCardSkeleton,
  TableRowSkeleton,
  CardSkeleton,
  PageSkeleton,
} from '../Skeleton';

describe('Skeleton components', () => {
  it('renders base Skeleton', () => {
    const { container } = render(<Skeleton className="h-6 w-32" />);
    expect(container.firstChild).toHaveClass('animate-pulse');
  });

  it('renders StatCardSkeleton', () => {
    const { container } = render(<StatCardSkeleton />);
    expect(container.firstChild).toHaveClass('stat-card');
  });

  it('renders multiple TableRowSkeleton', () => {
    const { container } = render(
      <>
        <TableRowSkeleton cols={4} />
        <TableRowSkeleton cols={4} />
      </>,
    );
    // Should render animated elements
    expect(container.querySelectorAll('.animate-pulse').length).toBeGreaterThan(0);
  });

  it('renders CardSkeleton', () => {
    const { container } = render(<CardSkeleton />);
    expect(container.firstChild).toHaveClass('card');
  });

  it('renders PageSkeleton with stat cards and content area', () => {
    const { container } = render(<PageSkeleton />);
    // Should contain multiple skeleton elements
    expect(container.querySelectorAll('.animate-pulse').length).toBeGreaterThan(3);
  });
});
