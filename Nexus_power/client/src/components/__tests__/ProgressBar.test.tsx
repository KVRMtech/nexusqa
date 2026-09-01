import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ProgressBar } from '../ProgressBar';

describe('ProgressBar', () => {
  it('renders with correct width', () => {
    const { container } = render(<ProgressBar value={75} />);
    const bar = container.querySelector('[style*="width"]');
    expect(bar).toHaveStyle({ width: '75%' });
  });

  it('clamps value to 0-100', () => {
    const { container } = render(<ProgressBar value={150} />);
    const bar = container.querySelector('[style*="width"]');
    expect(bar).toHaveStyle({ width: '100%' });
  });

  it('renders label when showLabel is true', () => {
    render(<ProgressBar value={50} label="CPU Usage" showLabel />);
    expect(screen.getByText('CPU Usage')).toBeInTheDocument();
  });

  it('shows percentage when showLabel is true', () => {
    render(<ProgressBar value={42} showLabel />);
    expect(screen.getByText('42%')).toBeInTheDocument();
  });

  it('has progressbar role with aria attributes', () => {
    render(<ProgressBar value={60} />);
    const bar = screen.getByRole('progressbar');
    expect(bar).toHaveAttribute('aria-valuenow', '60');
    expect(bar).toHaveAttribute('aria-valuemin', '0');
    expect(bar).toHaveAttribute('aria-valuemax', '100');
  });
});
