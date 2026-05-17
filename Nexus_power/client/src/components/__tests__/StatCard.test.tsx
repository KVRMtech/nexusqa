import { describe, it, expect } from 'vitest';
import { render, screen } from '@testing-library/react';
import { StatCard } from '../StatCard';
import { Zap } from 'lucide-react';

describe('StatCard', () => {
  it('renders label and value', () => {
    render(<StatCard label="Total" value={42} />);
    expect(screen.getByText('Total')).toBeInTheDocument();
    expect(screen.getByText('42')).toBeInTheDocument();
  });

  it('renders string value', () => {
    render(<StatCard label="Status" value="Active" />);
    expect(screen.getByText('Active')).toBeInTheDocument();
  });

  it('renders icon when provided', () => {
    render(<StatCard label="Power" value={100} icon={<Zap data-testid="icon" />} />);
    expect(screen.getByTestId('icon')).toBeInTheDocument();
  });

  it('renders suffix', () => {
    render(<StatCard label="Rate" value={99} suffix="%" />);
    expect(screen.getByText('%')).toBeInTheDocument();
  });

  it('renders description', () => {
    render(<StatCard label="Count" value={5} description="out of 10 total" />);
    expect(screen.getByText('out of 10 total')).toBeInTheDocument();
  });

  it('shows positive change indicator', () => {
    render(<StatCard label="Growth" value={42} change="+5.2%" changeType="positive" />);
    expect(screen.getByText('+5.2%')).toBeInTheDocument();
  });

  it('shows negative change indicator', () => {
    render(<StatCard label="Decline" value={42} change="-3.1%" changeType="negative" />);
    expect(screen.getByText('-3.1%')).toBeInTheDocument();
  });
});
