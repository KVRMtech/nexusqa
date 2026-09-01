import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { Tabs } from '../Tabs';

const tabs = [
  { id: 'tab1', label: 'Tab 1' },
  { id: 'tab2', label: 'Tab 2' },
  { id: 'tab3', label: 'Tab 3', disabled: true },
];

describe('Tabs', () => {
  it('renders all tab labels', () => {
    render(<Tabs tabs={tabs} activeTab="tab1" onChange={vi.fn()} />);
    expect(screen.getByText('Tab 1')).toBeInTheDocument();
    expect(screen.getByText('Tab 2')).toBeInTheDocument();
    expect(screen.getByText('Tab 3')).toBeInTheDocument();
  });

  it('marks active tab with aria-selected', () => {
    render(<Tabs tabs={tabs} activeTab="tab2" onChange={vi.fn()} />);
    const activeTab = screen.getByRole('tab', { name: 'Tab 2' });
    expect(activeTab).toHaveAttribute('aria-selected', 'true');
    const inactiveTab = screen.getByRole('tab', { name: 'Tab 1' });
    expect(inactiveTab).toHaveAttribute('aria-selected', 'false');
  });

  it('calls onChange when clicking a tab', () => {
    const onChange = vi.fn();
    render(<Tabs tabs={tabs} activeTab="tab1" onChange={onChange} />);
    fireEvent.click(screen.getByRole('tab', { name: 'Tab 2' }));
    expect(onChange).toHaveBeenCalledWith('tab2');
  });

  it('does not call onChange when clicking disabled tab', () => {
    const onChange = vi.fn();
    render(<Tabs tabs={tabs} activeTab="tab1" onChange={onChange} />);
    fireEvent.click(screen.getByRole('tab', { name: 'Tab 3' }));
    expect(onChange).not.toHaveBeenCalled();
  });

  it('renders pill variant', () => {
    const { container } = render(
      <Tabs tabs={tabs} activeTab="tab1" onChange={vi.fn()} variant="pills" />,
    );
    expect(container.firstChild).toHaveAttribute('role', 'tablist');
    // Pills variant doesn't have border-b wrapper
    expect(container.firstChild).not.toHaveClass('border-b');
  });

  it('renders count badges when provided', () => {
    const tabsWithCount = [{ id: 'a', label: 'Items', count: 42 }];
    render(<Tabs tabs={tabsWithCount} activeTab="a" onChange={vi.fn()} />);
    expect(screen.getByText('42')).toBeInTheDocument();
  });
});
