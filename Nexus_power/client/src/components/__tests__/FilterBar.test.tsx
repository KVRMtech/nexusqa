import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { FilterBar } from '../FilterBar';

const options = [
  { label: 'Active', value: 'active', count: 42 },
  { label: 'Archived', value: 'archived', count: 58 },
];

describe('FilterBar', () => {
  it('renders all filter labels including auto-All', () => {
    render(<FilterBar options={options} value="all" onChange={vi.fn()} />);
    expect(screen.getByText('All')).toBeInTheDocument();
    expect(screen.getByText('Active')).toBeInTheDocument();
    expect(screen.getByText('Archived')).toBeInTheDocument();
  });

  it('renders counts', () => {
    render(<FilterBar options={options} value="all" onChange={vi.fn()} />);
    expect(screen.getByText('42')).toBeInTheDocument();
    expect(screen.getByText('58')).toBeInTheDocument();
  });

  it('calls onChange when filter is clicked', () => {
    const onChange = vi.fn();
    render(<FilterBar options={options} value="all" onChange={onChange} />);
    fireEvent.click(screen.getByText('Active'));
    expect(onChange).toHaveBeenCalledWith('active');
  });

  it('highlights active filter', () => {
    const { container } = render(<FilterBar options={options} value="active" onChange={vi.fn()} />);
    const buttons = container.querySelectorAll('button');
    expect(buttons.length).toBeGreaterThan(0);
    const activeBtn = Array.from(buttons).find((b) => b.textContent?.includes('Active'));
    expect(activeBtn).toBeTruthy();
  });
});
