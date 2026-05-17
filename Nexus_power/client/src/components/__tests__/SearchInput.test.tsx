import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { SearchInput } from '../SearchInput';

describe('SearchInput', () => {
  it('renders with placeholder', () => {
    render(<SearchInput value="" onChange={vi.fn()} placeholder="Search..." />);
    expect(screen.getByPlaceholderText('Search...')).toBeInTheDocument();
  });

  it('calls onChange when typing', () => {
    const onChange = vi.fn();
    render(<SearchInput value="" onChange={onChange} />);
    const input = screen.getByRole('textbox');
    fireEvent.change(input, { target: { value: 'test query' } });
    expect(onChange).toHaveBeenCalledWith('test query');
  });

  it('shows clear button when value is non-empty', () => {
    render(<SearchInput value="query" onChange={vi.fn()} />);
    const clearBtn = screen.getByRole('button');
    expect(clearBtn).toBeInTheDocument();
  });

  it('clears value when clear button is clicked', () => {
    const onChange = vi.fn();
    render(<SearchInput value="query" onChange={onChange} />);
    fireEvent.click(screen.getByRole('button'));
    expect(onChange).toHaveBeenCalledWith('');
  });

  it('renders shortcut hint when provided', () => {
    render(<SearchInput value="" onChange={vi.fn()} shortcutHint="⌘K" />);
    expect(screen.getByText('⌘K')).toBeInTheDocument();
  });
});
