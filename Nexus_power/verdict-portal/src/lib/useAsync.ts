/**
 * useAsync — the ONE data-fetching hook the portal's data views use, so every
 * screen gets consistent loading / empty / error handling for free and every
 * request is abortable (the AbortController is torn down on unmount / re-run,
 * so a stale response can never overwrite fresh state).
 */
import { useCallback, useEffect, useMemo, useState } from 'react';

import { QecApiError } from './api';

export type AsyncStatus = 'loading' | 'success' | 'error';

export interface AsyncState<T> {
  status: AsyncStatus;
  data: T | undefined;
  error: QecApiError | undefined;
  isLoading: boolean;
  isError: boolean;
  isSuccess: boolean;
  /** Re-run the fetcher (e.g. from an error state's Retry button). */
  reload: () => void;
}

/**
 * @param fetcher receives an AbortSignal — pass it straight to `api.*({ signal })`.
 * @param deps    re-runs the fetcher when these change (like useEffect deps).
 */
export function useAsync<T>(fetcher: (signal: AbortSignal) => Promise<T>, deps: unknown[] = []): AsyncState<T> {
  const [status, setStatus] = useState<AsyncStatus>('loading');
  const [data, setData] = useState<T | undefined>(undefined);
  const [error, setError] = useState<QecApiError | undefined>(undefined);
  const [nonce, setNonce] = useState(0);

  const reload = useCallback(() => setNonce((n) => n + 1), []);

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const fetch = useCallback(fetcher, deps);

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    setStatus('loading');
    setError(undefined);

    fetch(controller.signal)
      .then((result) => {
        if (!active) return;
        setData(result);
        setStatus('success');
      })
      .catch((err: unknown) => {
        if (!active) return;
        const qe = err instanceof QecApiError ? err : new QecApiError('Unexpected error');
        if (qe.isAborted) return; // an aborted request is not an error state
        setError(qe);
        setStatus('error');
      });

    return () => {
      active = false;
      controller.abort();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fetch, nonce]);

  return useMemo(
    () => ({
      status,
      data,
      error,
      isLoading: status === 'loading',
      isError: status === 'error',
      isSuccess: status === 'success',
      reload,
    }),
    [status, data, error, reload],
  );
}
