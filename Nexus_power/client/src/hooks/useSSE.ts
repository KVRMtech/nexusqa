// ═══════════════════════════════════════════════════════════════
//  VKPOWER AI ENGINE FACTORY — Server-Sent Events (SSE) Hook
// ═══════════════════════════════════════════════════════════════
import { useEffect, useRef, useState, useCallback } from 'react';
import { useAuthStore } from '../stores/authStore';

export type SSEStatus = 'connecting' | 'connected' | 'disconnected' | 'error';

interface UseSSEOptions {
  /** SSE endpoint URL path (relative to API base) */
  path: string;
  /** Enable/disable the connection */
  enabled?: boolean;
  /** Auto-reconnect on error */
  autoReconnect?: boolean;
  /** Max reconnect attempts */
  maxReconnectAttempts?: number;
  /** Reconnect delay base (ms) */
  reconnectDelay?: number;
  /** Named event handlers: event type → handler */
  eventHandlers?: Record<string, (data: unknown) => void>;
  /** Default message handler (unnamed events) */
  onMessage?: (data: unknown) => void;
  /** Error handler */
  onError?: (event: Event) => void;
}

const API_BASE = import.meta.env.VITE_API_BASE || '/api';

/**
 * SSE hook for streaming server events (engine status, workflow progress, etc).
 * Provides auto-reconnect with exponential backoff and token-based auth.
 */
export function useSSE({
  path,
  enabled = true,
  autoReconnect = true,
  maxReconnectAttempts = 10,
  reconnectDelay = 2000,
  eventHandlers,
  onMessage,
  onError,
}: UseSSEOptions) {
  const sourceRef = useRef<EventSource | null>(null);
  const reconnectCount = useRef(0);
  const reconnectTimer = useRef<ReturnType<typeof setTimeout>>();
  const [status, setStatus] = useState<SSEStatus>('disconnected');
  const [lastEvent, setLastEvent] = useState<unknown>(null);
  // Read token from Zustand store, falling back to legacy sessionStorage key
  // (AuthContext writes to nexus_token, Zustand store may not be synced)
  const zustandToken = useAuthStore((s) => s.token);
  const token = zustandToken || sessionStorage.getItem('nexus_token');

  const onMessageRef = useRef(onMessage);
  onMessageRef.current = onMessage;
  const eventHandlersRef = useRef(eventHandlers);
  eventHandlersRef.current = eventHandlers;
  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;

  const connect = useCallback(() => {
    if (sourceRef.current) return;

    const url = new URL(`${API_BASE}${path}`, window.location.origin);
    if (token) url.searchParams.set('token', token);

    setStatus('connecting');
    const source = new EventSource(url.toString());

    source.onopen = () => {
      setStatus('connected');
      reconnectCount.current = 0;
    };

    source.onmessage = (event) => {
      try {
        const data = JSON.parse(event.data);
        setLastEvent(data);
        onMessageRef.current?.(data);
      } catch {
        setLastEvent(event.data);
        onMessageRef.current?.(event.data);
      }
    };

    // Register named event listeners
    if (eventHandlersRef.current) {
      for (const [eventName, handler] of Object.entries(eventHandlersRef.current)) {
        source.addEventListener(eventName, ((event: MessageEvent) => {
          try {
            handler(JSON.parse(event.data));
          } catch {
            handler(event.data);
          }
        }) as EventListener);
      }
    }

    source.onerror = (event) => {
      setStatus('error');
      onErrorRef.current?.(event);
      source.close();
      sourceRef.current = null;

      // Auto-reconnect
      if (autoReconnect && reconnectCount.current < maxReconnectAttempts) {
        const delay = reconnectDelay * Math.pow(2, reconnectCount.current);
        reconnectCount.current++;
        reconnectTimer.current = setTimeout(connect, Math.min(delay, 30_000));
      } else {
        setStatus('disconnected');
      }
    };

    sourceRef.current = source;
  }, [path, token, autoReconnect, maxReconnectAttempts, reconnectDelay]);

  const disconnect = useCallback(() => {
    clearTimeout(reconnectTimer.current);
    reconnectCount.current = maxReconnectAttempts;
    sourceRef.current?.close();
    sourceRef.current = null;
    setStatus('disconnected');
  }, [maxReconnectAttempts]);

  useEffect(() => {
    if (enabled) connect();
    return () => {
      clearTimeout(reconnectTimer.current);
      sourceRef.current?.close();
      sourceRef.current = null;
    };
  }, [enabled, connect]);

  return { status, lastEvent, disconnect, reconnect: connect };
}
