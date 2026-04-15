import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { GlobalErrorBoundary } from '../GlobalErrorBoundary';

// Component that throws
function ThrowingComponent({ shouldThrow }: { shouldThrow: boolean }) {
  if (shouldThrow) throw new Error('Test explosion');
  return <div>All good</div>;
}

// Suppress React error boundary console.error
beforeEach(() => {
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

describe('GlobalErrorBoundary', () => {
  it('renders children when no error', () => {
    render(
      <GlobalErrorBoundary>
        <ThrowingComponent shouldThrow={false} />
      </GlobalErrorBoundary>,
    );
    expect(screen.getByText('All good')).toBeInTheDocument();
  });

  it('renders fallback UI when child throws', () => {
    render(
      <GlobalErrorBoundary>
        <ThrowingComponent shouldThrow={true} />
      </GlobalErrorBoundary>,
    );
    expect(screen.getByText(/Something went wrong/i)).toBeInTheDocument();
  });

  it('shows reload button', () => {
    render(
      <GlobalErrorBoundary>
        <ThrowingComponent shouldThrow={true} />
      </GlobalErrorBoundary>,
    );
    expect(screen.getByText(/Reload/i)).toBeInTheDocument();
  });
});
