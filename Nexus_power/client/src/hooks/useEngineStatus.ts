// ═══════════════════════════════════════════════════════════════
//  VKPOWER AI ENGINE FACTORY — Engine Status Real-Time Hook
// ═══════════════════════════════════════════════════════════════
import { useEffect } from 'react';
import { useEngineStore } from '../stores/engineStore';

/**
 * HTTP polling for engine status.
 * Polls engine health endpoints every 60s.
 */
export function useEngineStatus() {
  const { engines, healthyCount, totalCount, allHealthy, lastChecked, startPolling, stopPolling } =
    useEngineStore();
  const updateEngine = useEngineStore((s) => s.updateEngine);

  // Start HTTP polling on mount
  useEffect(() => {
    startPolling(60_000);
    return () => stopPolling();
  }, [startPolling, stopPolling]);

  // WebSocket disabled — no WS backend is deployed in canonical mode.
  // HTTP polling (above) is the sole health-check mechanism.

  return {
    engines,
    healthyCount,
    totalCount,
    allHealthy,
    lastChecked,
    wsConnected: false,
  };
}
