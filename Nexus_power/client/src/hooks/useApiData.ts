import { useState, useEffect, useCallback, useRef } from 'react';

/**
 * Generic hook that fetches data from the API without masking backend failures.
 *
 * Usage:
 *   const { data, loading, error, refetch, isLive } = useApiData(
 *     () => api.listSessions(),
 *     [],
 *   );
 *
 * - On success -> data = API response, isLive = true
 * - On failure -> keeps prior data, surfaces error, isLive = false
 */
export function useApiData<T>(
  fetcher: () => Promise<unknown>,
  fallback: T,
  /** Set false to skip automatic fetch on mount */
  autoFetch = true,
  /**
   * When 'graceful', API failures may keep initial fallback data.
   * Default is 'strict' so E2E and production runs surface real backend state.
   */
  mode: 'graceful' | 'strict' = 'strict',
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
      if (mode === 'graceful' && !hasFetchedOnce.current) {
        setData(fallbackRef.current);
      }
      setIsLive(false);
      setError(err?.message || 'API unavailable');
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
