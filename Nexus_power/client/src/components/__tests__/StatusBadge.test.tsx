import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { StatusBadge } from '../StatusBadge';

describe('StatusBadge', () => {
  it('renders text content', () => {
    render(<StatusBadge variant="success" label="Online" />);
    expect(screen.getByText('Online')).toBeInTheDocument();
  });

  it.each([
    ['success', 'badge-green'],
    ['warning', 'badge-yellow'],
    ['error', 'badge-red'],
    ['info', 'badge-blue'],
    ['nexus', 'badge-nexus'],
    ['gray', 'badge-gray'],
  ] as const)('maps variant %s to CSS class %s', (variant, expectedClass) => {
    const { container } = render(<StatusBadge variant={variant} label="Test" />);
    expect(container.firstChild).toHaveClass(expectedClass);
  });

  it('shows pulse dot when pulse is true', () => {
    const { container } = render(<StatusBadge variant="success" pulse label="Active" />);
    // Pulse ping animation uses 'animate-ping'
    const pingDot = container.querySelector('.animate-ping');
    expect(pingDot).toBeInTheDocument();
  });

  it('renders icon when provided', () => {
    render(
      <StatusBadge variant="info" icon={<span data-testid="icon">!</span>} label="With Icon" />,
    );
    expect(screen.getByTestId('icon')).toBeInTheDocument();
  });
});
