// ═══════════════════════════════════════════════════════════════
//  NEXUS AI ENGINE FACTORY — Notification Center (Sonner Toast)
// ═══════════════════════════════════════════════════════════════
import { useState } from 'react';
import { Bell, Check, X, Trash2 } from 'lucide-react';
import clsx from 'clsx';
import { useNotificationStore } from '../stores';
import type { Notification } from '../stores';

const typeIcons: Record<string, string> = {
  success: '✓',
  error: '✗',
  warning: '⚠',
  info: 'ℹ',
};

const typeColors: Record<string, string> = {
  success: 'text-green-400',
  error: 'text-red-400',
  warning: 'text-yellow-400',
  info: 'text-blue-400',
};

/**
 * Notification bell + dropdown list in the top bar.
 */
export function NotificationCenter() {
  const [open, setOpen] = useState(false);
  const { notifications, unreadCount, markRead, markAllRead, dismiss, clearAll } =
    useNotificationStore();

  return (
    <div className="relative">
      {/* Bell trigger */}
      <button
        onClick={() => setOpen((o) => !o)}
        className="relative rounded-lg p-2 text-gray-400 hover:text-white hover:bg-white/[0.06] transition-colors"
        aria-label={`Notifications (${unreadCount} unread)`}
      >
        <Bell className="h-5 w-5" />
        {unreadCount > 0 && (
          <span className="absolute -right-0.5 -top-0.5 flex h-4 w-4 items-center justify-center rounded-full bg-red-500 text-[9px] font-bold text-white">
            {unreadCount > 9 ? '9+' : unreadCount}
          </span>
        )}
      </button>

      {/* Dropdown panel */}
      {open && (
        <>
          <div className="fixed inset-0 z-40" onClick={() => setOpen(false)} />
          <div className="absolute right-0 top-full z-50 mt-2 w-80 rounded-xl bg-gray-900/95 backdrop-blur-lg shadow-2xl ring-1 ring-white/[0.08] animate-[slide-in_0.15s_ease-out]">
            {/* Header */}
            <div className="flex items-center justify-between border-b border-white/[0.06] px-4 py-3">
              <h3 className="text-sm font-semibold text-white">Notifications</h3>
              <div className="flex gap-1">
                {unreadCount > 0 && (
                  <button
                    onClick={markAllRead}
                    className="btn-ghost text-xs px-2 py-1"
                    title="Mark all read"
                  >
                    <Check className="h-3.5 w-3.5" />
                  </button>
                )}
                {notifications.length > 0 && (
                  <button
                    onClick={clearAll}
                    className="btn-ghost text-xs px-2 py-1"
                    title="Clear all"
                  >
                    <Trash2 className="h-3.5 w-3.5" />
                  </button>
                )}
              </div>
            </div>

            {/* List */}
            <div className="max-h-72 overflow-y-auto divide-y divide-white/[0.04]">
              {notifications.length === 0 ? (
                <div className="px-4 py-8 text-center text-sm text-gray-500">
                  No notifications
                </div>
              ) : (
                notifications.slice(0, 20).map((n) => (
                  <NotificationItem
                    key={n.id}
                    notification={n}
                    onRead={() => markRead(n.id)}
                    onDismiss={() => dismiss(n.id)}
                  />
                ))
              )}
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function NotificationItem({
  notification: n,
  onRead,
  onDismiss,
}: {
  notification: Notification;
  onRead: () => void;
  onDismiss: () => void;
}) {
  return (
    <div
      className={clsx(
        'group flex items-start gap-3 px-4 py-3 hover:bg-white/[0.03] transition-colors',
        !n.read && 'bg-white/[0.02]',
      )}
      onClick={onRead}
    >
      <span className={clsx('mt-0.5 text-sm font-bold', typeColors[n.type])}>
        {typeIcons[n.type]}
      </span>
      <div className="flex-1 min-w-0">
        <p className={clsx('text-sm', n.read ? 'text-gray-400' : 'text-gray-200 font-medium')}>
          {n.title}
        </p>
        {n.message && <p className="mt-0.5 text-xs text-gray-500 truncate">{n.message}</p>}
        <p className="mt-1 text-[10px] text-gray-600">
          {new Date(n.timestamp).toLocaleTimeString()}
        </p>
      </div>
      <button
        onClick={(e) => {
          e.stopPropagation();
          onDismiss();
        }}
        className="opacity-0 group-hover:opacity-100 rounded p-0.5 text-gray-500 hover:text-gray-300 transition-opacity"
        aria-label="Dismiss"
      >
        <X className="h-3.5 w-3.5" />
      </button>
    </div>
  );
}
