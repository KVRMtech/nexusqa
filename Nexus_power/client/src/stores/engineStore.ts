// ═══════════════════════════════════════════════════════════════
//  VKPOWER AI ENGINE FACTORY — Engine Status Store (Zustand)
// ═══════════════════════════════════════════════════════════════
import { create } from 'zustand';
import type { EngineHealth } from '../types';
import api from '../services/api';
import { ACTIVE_ENGINES, ACTIVE_CONTROL_PLANE } from '../productMode';

const ENGINE_NAMES = ACTIVE_ENGINES as readonly string[];
const CONTROL_PLANE_NAMES = ACTIVE_CONTROL_PLANE.map((s) => s.name);

export type EngineName = string;

export interface EngineStatusState {
  engines: Record<EngineName, EngineHealth>;
  controlPlane: Record<string, 'healthy' | 'unreachable'>;
  allHealthy: boolean;
  healthyCount: number;
  totalCount: number;
  lastChecked: string | null;
  isPolling: boolean;
  pollIntervalMs: number;
  error: string | null;
}

interface EngineStatusActions {
  fetchAll: () => Promise<void>;
  startPolling: (intervalMs?: number) => void;
  stopPolling: () => void;
  updateEngine: (name: EngineName, health: EngineHealth) => void;
}

export type EngineStore = EngineStatusState & EngineStatusActions;

const defaultEngineHealth = (name: string): EngineHealth => ({
  engine: name,
  status: 'unreachable',
});

const initialEngines = Object.fromEntries(
  ENGINE_NAMES.map((n) => [n, defaultEngineHealth(n)]),
) as Record<EngineName, EngineHealth>;

const initialControlPlane = Object.fromEntries(
  CONTROL_PLANE_NAMES.map((n) => [n, 'unreachable' as const]),
) as Record<string, 'healthy' | 'unreachable'>;

let pollTimer: ReturnType<typeof setInterval> | null = null;

export const useEngineStore = create<EngineStore>()((set, get) => ({
  engines: { ...initialEngines },
  controlPlane: { ...initialControlPlane },
  allHealthy: false,
  healthyCount: 0,
  totalCount: ENGINE_NAMES.length + CONTROL_PLANE_NAMES.length,
  lastChecked: null,
  isPolling: false,
  pollIntervalMs: 30_000,
  error: null,

  fetchAll: async () => {
    try {
      const [results, cpResults] = await Promise.all([
        api.getAllEngineHealth(),
        api.getControlPlaneHealth(),
      ]);
      const engines = { ...initialEngines };
      let healthyCount = 0;

      for (const r of results) {
        const name = r.engine as EngineName;
        if (ENGINE_NAMES.includes(name)) {
          engines[name] = r as EngineHealth;
          if (r.status === 'healthy') healthyCount++;
        }
      }

      const controlPlane = { ...initialControlPlane };
      for (const cp of cpResults) {
        controlPlane[cp.service] = cp.status as 'healthy' | 'unreachable';
        if (cp.status === 'healthy') healthyCount++;
      }

      const totalCount = ENGINE_NAMES.length + CONTROL_PLANE_NAMES.length;

      set({
        engines,
        controlPlane,
        healthyCount,
        allHealthy: healthyCount === totalCount,
        lastChecked: new Date().toISOString(),
        error: null,
      });
    } catch (err: any) {
      set({ error: err?.message || 'Failed to fetch engine health' });
    }
  },

  startPolling: (intervalMs = 30_000) => {
    const state = get();
    if (state.isPolling) return;

    set({ isPolling: true, pollIntervalMs: intervalMs });
    // Immediate fetch
    get().fetchAll();
    pollTimer = setInterval(() => get().fetchAll(), intervalMs);
  },

  stopPolling: () => {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = null;
    }
    set({ isPolling: false });
  },

  updateEngine: (name, health) => {
    set((state) => {
      const engines = { ...state.engines, [name]: health };
      const engineHealthy = Object.values(engines).filter((e) => e.status === 'healthy').length;
      const cpHealthy = Object.values(state.controlPlane).filter((s) => s === 'healthy').length;
      const healthyCount = engineHealthy + cpHealthy;
      const totalCount = ENGINE_NAMES.length + CONTROL_PLANE_NAMES.length;
      return {
        engines,
        healthyCount,
        allHealthy: healthyCount === totalCount,
        lastChecked: new Date().toISOString(),
      };
    });
  },
}));

// Selector helpers
export const selectEngine = (name: EngineName) => (s: EngineStore) => s.engines[name];
export const selectAllHealthy = (s: EngineStore) => s.allHealthy;
export const selectHealthSummary = (s: EngineStore) => ({
  healthy: s.healthyCount,
  total: s.totalCount,
  allHealthy: s.allHealthy,
});
