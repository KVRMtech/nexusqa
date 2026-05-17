// ═══════════════════════════════════════════════════════════════
//  NEXUS AI ENGINE FACTORY — WebSocket Hook
// ═══════════════════════════════════════════════════════════════
import { useEffect, useRef, useCallback, useState } from 'react';
import { useAuthStore } from '../stores/authStore';

export type WSStatus = 'connecting' | 'connected' | 'disconnected' | 'error';

interface UseWebSocketOptions {
  /** WebSocket endpoint path (relative to WS_BASE) */
  path: string;
  /** Enable/disable the connection */
  enabled?: boolean;
  /** Reconnect automatically on disconnect */
  autoReconnect?: boolean;
  /** Base delay between reconnect attempts (ms) */
  reconnectDelay?: number;
  /** Max reconnect attempts */
  maxReconnectAttempts?: number;
  /** Message handler */
  onMessage?: (data: unknown) => void;
  /** Connection opened */
  onOpen?: () => void;
  /** Connection closed */
  onClose?: (event: CloseEvent) => void;
  /** Error handler */
  onError?: (event: Event) => void;
}

const WS_BASE =
  import.meta.env.VITE_WS_BASE ||
  `${window.location.protocol === 'https:' ? 'wss:' : 'ws:'}//${window.location.host}/ws`;

/**
 * Production-grade WebSocket hook with:
 * - Auto-reconnect with exponential backoff
 * - Auth token injection (via query param)
 * - Typed message parsing
 * - Clean teardown on unmount
 */
export function useWebSocket({
  path,
  enabled = true,
  autoReconnect = true,
  reconnectDelay = 1000,
  maxReconnectAttempts = 10,
  onMessage,
  onOpen,
  onClose,
  onError,
}: UseWebSocketOptions) {
  const wsRef = useRef<WebSocket | null>(null);
  const reconnectCount = useRef(0);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout>>();
  const [status, setStatus] = useState<WSStatus>('disconnected');
  const token = useAuthStore((s) => s.token);

  // Stable callback refs
  const onMessageRef = useRef(onMessage);
  onMessageRef.current = onMessage;
  const onOpenRef = useRef(onOpen);
  onOpenRef.current = onOpen;
  const onCloseRef = useRef(onClose);
  onCloseRef.current = onClose;
  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;

  const connect = useCallback(() => {
    if (wsRef.current?.readyState === WebSocket.OPEN) return;

    const url = new URL(`${WS_BASE}${path}`);
    if (token) url.searchParams.set('token', token);

    setStatus('connecting');
    const ws = new WebSocket(url.toString());

    ws.onopen = () => {
      setStatus('connected');
      reconnectCount.current = 0;
      onOpenRef.current?.();
    };

    ws.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        onMessageRef.current?.(data);
      } catch {
        // Non-JSON message
        onMessageRef.current?.(event.data);
      }
    };

    ws.onclose = (event) => {
      setStatus('disconnected');
      onCloseRef.current?.(event);

      // Auto-reconnect with exponential backoff
      if (autoReconnect && !event.wasClean && reconnectCount.current < maxReconnectAttempts) {
        const delay = reconnectDelay * Math.pow(2, reconnectCount.current);
        reconnectCount.current++;
        reconnectTimer.current = setTimeout(connect, Math.min(delay, 30_000));
      }
    };

    ws.onerror = (event) => {
      setStatus('error');
      onErrorRef.current?.(event);
    };

    wsRef.current = ws;
  }, [path, token, autoReconnect, reconnectDelay, maxReconnectAttempts]);

  const disconnect = useCallback(() => {
    clearTimeout(reconnectTimer.current);
    reconnectCount.current = maxReconnectAttempts; // prevent reconnect
    wsRef.current?.close(1000, 'client_disconnect');
    wsRef.current = null;
    setStatus('disconnected');
  }, [maxReconnectAttempts]);

  const send = useCallback((data: unknown) => {
    if (wsRef.current?.readyState === WebSocket.OPEN) {
      wsRef.current.send(typeof data === 'string' ? data : JSON.stringify(data));
    }
  }, []);

  useEffect(() => {
    if (enabled) connect();
    return () => {
      clearTimeout(reconnectTimer.current);
      wsRef.current?.close(1000, 'unmount');
      wsRef.current = null;
    };
  }, [enabled, connect]);

  return { status, send, disconnect, reconnect: connect };
}
