import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { BrowserRouter } from 'react-router-dom';
import { RouteErrorBoundary } from '../RouteErrorBoundary';

function ThrowingComponent({ shouldThrow }: { shouldThrow: boolean }) {
  if (shouldThrow) throw new Error('Route Error');
  return <div>Page content</div>;
}

beforeEach(() => {
  vi.spyOn(console, 'error').mockImplementation(() => {});
});

describe('RouteErrorBoundary', () => {
  it('renders children when no error', () => {
    render(
      <BrowserRouter>
        <RouteErrorBoundary pageName="TestPage">
          <ThrowingComponent shouldThrow={false} />
        </RouteErrorBoundary>
      </BrowserRouter>,
    );
    expect(screen.getByText('Page content')).toBeInTheDocument();
  });

  it('renders error card when child throws', () => {
    render(
      <BrowserRouter>
        <RouteErrorBoundary pageName="TestPage">
          <ThrowingComponent shouldThrow={true} />
        </RouteErrorBoundary>
      </BrowserRouter>,
    );
    expect(screen.getByText('TestPage crashed')).toBeInTheDocument();
  });

  it('shows retry button', () => {
    render(
      <BrowserRouter>
        <RouteErrorBoundary pageName="TestPage">
          <ThrowingComponent shouldThrow={true} />
        </RouteErrorBoundary>
      </BrowserRouter>,
    );
    expect(screen.getByText(/Retry/i)).toBeInTheDocument();
  });
});
