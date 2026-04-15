import { describe, it, expect, beforeEach, vi } from 'vitest';
import { act } from '@testing-library/react';

vi.unmock('../../stores/engineStore');

// Mock the api module
vi.mock('../../services/api', () => ({
  default: {
    getAllEngineHealth: vi.fn().mockResolvedValue([]),
  },
}));

let useEngineStore: any;

beforeEach(async () => {
  vi.resetModules();
  const module = await import('../../stores/engineStore');
  useEngineStore = module.useEngineStore;
});

describe('engineStore', () => {
  it('starts with 11 engines all unreachable', () => {
    const state = useEngineStore.getState();
    expect(Object.keys(state.engines)).toHaveLength(11);
    expect(state.engines['shield'].status).toBe('unreachable');
    expect(state.engines['ears'].status).toBe('unreachable');
  });

  it('computes healthyCount and allHealthy', () => {
    const state = useEngineStore.getState();
    expect(state.healthyCount).toBe(0);
    expect(state.allHealthy).toBe(false);
  });

  it('updates a single engine', () => {
    act(() => {
      useEngineStore.getState().updateEngine('shield', {
        engine: 'shield',
        status: 'healthy',
        latencyMs: 42,
        version: '1.0.0',
      });
    });
    const engine = useEngineStore.getState().engines['shield'];
    expect(engine.status).toBe('healthy');
  });

  it('healthyCount increments with healthy engines', () => {
    act(() => {
      useEngineStore.getState().updateEngine('shield', { engine: 'shield', status: 'healthy' });
      useEngineStore.getState().updateEngine('ears', { engine: 'ears', status: 'healthy' });
    });
    expect(useEngineStore.getState().healthyCount).toBe(2);
  });

  it('allHealthy is true only when all engines are healthy', () => {
    act(() => {
      const state = useEngineStore.getState();
      Object.keys(state.engines).forEach((key) => {
        state.updateEngine(key, { engine: key, status: 'healthy' });
      });
    });
    expect(useEngineStore.getState().allHealthy).toBe(true);
  });

  it('fetchAll calls the API and handles response', async () => {
    const apiModule = await import('../../services/api');
    (apiModule.default.getAllEngineHealth as any).mockResolvedValueOnce([
      { engine: 'shield', status: 'healthy', latencyMs: 10 },
    ]);

    await act(async () => {
      await useEngineStore.getState().fetchAll();
    });

    expect(apiModule.default.getAllEngineHealth).toHaveBeenCalled();
    expect(useEngineStore.getState().engines['shield'].status).toBe('healthy');
  });
});
