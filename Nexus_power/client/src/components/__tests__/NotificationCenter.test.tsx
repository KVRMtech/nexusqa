import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { NotificationCenter } from '../NotificationCenter';

// Override the mock to return actual notifications for this test
vi.mock('../../stores/notificationStore', () => {
  const notifications = [
    {
      id: 'n1',
      type: 'success',
      title: 'Test passed',
      message: 'All 42 tests passed.',
      read: false,
      timestamp: Date.now(),
    },
    {
      id: 'n2',
      type: 'error',
      title: 'Engine down',
      message: 'Shield engine is offline.',
      read: true,
      timestamp: Date.now() - 60000,
    },
  ];
  const store = {
    notifications,
    unreadCount: 1,
    addNotification: vi.fn(),
    markRead: vi.fn(),
    markAllRead: vi.fn(),
    dismiss: vi.fn(),
    clearAll: vi.fn(),
  };
  return {
    useNotificationStore: Object.assign(vi.fn(() => store), { getState: vi.fn(() => store) }),
    notify: vi.fn(),
    notifySuccess: vi.fn(),
    notifyError: vi.fn(),
    notifyWarning: vi.fn(),
    notifyInfo: vi.fn(),
  };
});

describe('NotificationCenter', () => {
  it('renders the bell icon', () => {
    render(<NotificationCenter />);
    // Bell icon button should exist
    const button = screen.getByRole('button');
    expect(button).toBeInTheDocument();
  });

  it('shows unread badge', () => {
    render(<NotificationCenter />);
    expect(screen.getByText('1')).toBeInTheDocument();
  });

  it('opens dropdown on click', () => {
    render(<NotificationCenter />);
    fireEvent.click(screen.getByRole('button'));
    expect(screen.getByText('Test passed')).toBeInTheDocument();
    expect(screen.getByText('Engine down')).toBeInTheDocument();
  });

  it('shows notification messages in dropdown', () => {
    render(<NotificationCenter />);
    fireEvent.click(screen.getByRole('button'));
    expect(screen.getByText('All 42 tests passed.')).toBeInTheDocument();
    expect(screen.getByText('Shield engine is offline.')).toBeInTheDocument();
  });
});
