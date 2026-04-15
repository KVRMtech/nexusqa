import { describe, it, expect, beforeEach, vi } from 'vitest';
import { act } from '@testing-library/react';

// We need to unmock the store to test it directly
vi.unmock('../../stores/notificationStore');

// Dynamic import to get un-mocked version
let useNotificationStore: any;
let notify: any;
let notifySuccess: any;
let notifyError: any;

beforeEach(async () => {
  // Re-import fresh module each time
  vi.resetModules();
  const module = await import('../../stores/notificationStore');
  useNotificationStore = module.useNotificationStore;
  notify = module.notify;
  notifySuccess = module.notifySuccess;
  notifyError = module.notifyError;

  // Clear state
  act(() => {
    useNotificationStore.getState().clearAll();
  });
});

describe('notificationStore', () => {
  it('starts with empty notifications', () => {
    const state = useNotificationStore.getState();
    expect(state.notifications).toEqual([]);
    expect(state.unreadCount).toBe(0);
  });

  it('adds a notification', () => {
    act(() => {
      useNotificationStore.getState().addNotification({
        type: 'info',
        title: 'Test',
        message: 'Hello world',
      });
    });
    const state = useNotificationStore.getState();
    expect(state.notifications).toHaveLength(1);
    expect(state.notifications[0].title).toBe('Test');
    expect(state.notifications[0].read).toBe(false);
    expect(state.unreadCount).toBe(1);
  });

  it('marks notification as read', () => {
    act(() => {
      useNotificationStore.getState().addNotification({
        type: 'info',
        title: 'Read me',
      });
    });
    const id = useNotificationStore.getState().notifications[0].id;
    act(() => {
      useNotificationStore.getState().markRead(id);
    });
    const state = useNotificationStore.getState();
    expect(state.notifications[0].read).toBe(true);
    expect(state.unreadCount).toBe(0);
  });

  it('marks all as read', () => {
    act(() => {
      useNotificationStore.getState().addNotification({ type: 'info', title: 'A' });
      useNotificationStore.getState().addNotification({ type: 'info', title: 'B' });
    });
    expect(useNotificationStore.getState().unreadCount).toBe(2);
    act(() => {
      useNotificationStore.getState().markAllRead();
    });
    expect(useNotificationStore.getState().unreadCount).toBe(0);
  });

  it('dismisses a notification', () => {
    act(() => {
      useNotificationStore.getState().addNotification({ type: 'info', title: 'Dismiss me' });
    });
    const id = useNotificationStore.getState().notifications[0].id;
    act(() => {
      useNotificationStore.getState().dismiss(id);
    });
    expect(useNotificationStore.getState().notifications).toHaveLength(0);
  });

  it('clears all notifications', () => {
    act(() => {
      useNotificationStore.getState().addNotification({ type: 'info', title: 'A' });
      useNotificationStore.getState().addNotification({ type: 'warning', title: 'B' });
    });
    act(() => {
      useNotificationStore.getState().clearAll();
    });
    expect(useNotificationStore.getState().notifications).toHaveLength(0);
    expect(useNotificationStore.getState().unreadCount).toBe(0);
  });

  it('caps at 100 notifications', () => {
    act(() => {
      for (let i = 0; i < 110; i++) {
        useNotificationStore.getState().addNotification({ type: 'info', title: `N${i}` });
      }
    });
    expect(useNotificationStore.getState().notifications.length).toBeLessThanOrEqual(100);
  });

  it('helper functions add notifications', () => {
    act(() => {
      notifySuccess('Great', 'It worked');
      notifyError('Bad', 'It broke');
    });
    const state = useNotificationStore.getState();
    expect(state.notifications).toHaveLength(2);
    expect(state.notifications.find((n: any) => n.type === 'success')).toBeTruthy();
    expect(state.notifications.find((n: any) => n.type === 'error')).toBeTruthy();
  });
});
