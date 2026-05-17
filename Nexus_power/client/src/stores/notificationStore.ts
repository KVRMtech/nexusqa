// ═══════════════════════════════════════════════════════════════
//  NEXUS AI ENGINE FACTORY — Notification Store (Zustand)
// ═══════════════════════════════════════════════════════════════
import { create } from 'zustand';

export type NotificationType = 'success' | 'error' | 'warning' | 'info';

export interface Notification {
  id: string;
  type: NotificationType;
  title: string;
  message?: string;
  timestamp: string;
  read: boolean;
  /** Auto-dismiss after ms. 0 = persist until dismissed. */
  ttl: number;
  /** Optional link for navigation on click */
  href?: string;
}

interface NotificationState {
  notifications: Notification[];
  unreadCount: number;
}

interface NotificationActions {
  addNotification: (n: Omit<Notification, 'id' | 'timestamp' | 'read'>) => string;
  markRead: (id: string) => void;
  markAllRead: () => void;
  dismiss: (id: string) => void;
  clearAll: () => void;
}

export type NotificationStore = NotificationState & NotificationActions;

let notifCounter = 0;

export const useNotificationStore = create<NotificationStore>()((set, get) => ({
  notifications: [],
  unreadCount: 0,

  addNotification: (n) => {
    const id = `notif-${Date.now()}-${++notifCounter}`;
    const notification: Notification = {
      ...n,
      id,
      timestamp: new Date().toISOString(),
      read: false,
    };

    set((state) => ({
      notifications: [notification, ...state.notifications].slice(0, 100), // cap at 100
      unreadCount: state.unreadCount + 1,
    }));

    // Auto-dismiss after TTL if > 0
    if (n.ttl > 0) {
      setTimeout(() => get().dismiss(id), n.ttl);
    }

    return id;
  },

  markRead: (id) =>
    set((state) => {
      const notifications = state.notifications.map((n) =>
        n.id === id && !n.read ? { ...n, read: true } : n,
      );
      return {
        notifications,
        unreadCount: notifications.filter((n) => !n.read).length,
      };
    }),

  markAllRead: () =>
    set((state) => ({
      notifications: state.notifications.map((n) => ({ ...n, read: true })),
      unreadCount: 0,
    })),

  dismiss: (id) =>
    set((state) => {
      const notifications = state.notifications.filter((n) => n.id !== id);
      return {
        notifications,
        unreadCount: notifications.filter((n) => !n.read).length,
      };
    }),

  clearAll: () => set({ notifications: [], unreadCount: 0 }),
}));

// --- Helper: quick notification creators ---
export function notify(
  type: NotificationType,
  title: string,
  message?: string,
  ttl = 5000,
) {
  return useNotificationStore.getState().addNotification({ type, title, message, ttl });
}

export const notifySuccess = (title: string, message?: string) =>
  notify('success', title, message);
export const notifyError = (title: string, message?: string) =>
  notify('error', title, message, 8000);
export const notifyWarning = (title: string, message?: string) =>
  notify('warning', title, message, 6000);
export const notifyInfo = (title: string, message?: string) =>
  notify('info', title, message);
