import { useState, useEffect, useCallback, useRef } from 'react';

/**
 * Generic hook that fetches data from the API with graceful fallback to demo data.
 *
 * Usage:
 *   const { data, loading, error, refetch, isLive } = useApiData(
 *     () => api.listSessions('t-1'),
 *     DEMO_SESSIONS,
 *   );
 *
 * - On success → data = API response, isLive = true
 * - On failure → data = fallback, isLive = false (silent degradation)
 */
export function useApiData<T>(
  fetcher: () => Promise<unknown>,
  fallback: T,
  /** Set false to skip automatic fetch on mount */
  autoFetch = true,
  /**
   * When 'strict', API failures throw instead of falling back to demo data.
   * Use in production to surface backend connectivity issues to operators.
   * Default: 'graceful' (silent fallback — for development / demos).
   */
  mode: 'graceful' | 'strict' = (import.meta.env.VITE_API_MODE === 'strict' ? 'strict' : 'graceful'),
) {
  const [data, setData] = useState<T>(fallback);
  const [loading, setLoading] = useState(autoFetch);
  const [error, setError] = useState<string | null>(null);
  const [isLive, setIsLive] = useState(false);
  const fetcherRef = useRef(fetcher);
  fetcherRef.current = fetcher;

  const hasFetchedOnce = useRef(false);
  const fallbackRef = useRef(fallback);
  fallbackRef.current = fallback;

  const refetch = useCallback(async () => {
    // Only show loading spinner on the very first fetch, not on re-fetches
    if (!hasFetchedOnce.current) setLoading(true);
    setError(null);
    try {
      const result = await fetcherRef.current();
      setData(result as T);
      setIsLive(true);
      hasFetchedOnce.current = true;
    } catch (err: any) {
      if (mode === 'strict') {
        // Production: surface real error, keep previous data
        setIsLive(false);
        setError(err?.message || 'API unavailable');
      } else {
        // Graceful: fall back to demo data only if we never fetched successfully
        if (!hasFetchedOnce.current) setData(fallbackRef.current);
        setIsLive(false);
        setError(err?.message || 'API unavailable — showing demo data');
      }
    } finally {
      setLoading(false);
    }
  }, [mode]);

  useEffect(() => {
    if (autoFetch) {
      refetch();
    }
  }, [autoFetch, refetch]);

  return { data, loading, error, refetch, isLive } as const;
}

export default useApiData;
