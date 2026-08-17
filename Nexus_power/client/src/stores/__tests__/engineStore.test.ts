import { describe, it, expect, beforeEach, vi } from 'vitest';
import { act } from '@testing-library/react';
import { ACTIVE_ENGINES, ACTIVE_CONTROL_PLANE } from '../../productMode';

vi.unmock('../../stores/engineStore');

// BOTH health calls are mocked, because fetchAll awaits BOTH of them inside one
// Promise.all. Mocking only getAllEngineHealth left getControlPlaneHealth
// undefined, so the Promise.all rejected with a TypeError, fetchAll's catch
// swallowed it into `error`, and the engines map was never written — which
// surfaced as the misleading "expected 'unreachable' to be 'healthy'".
vi.mock('../../services/api', () => ({
  default: {
    getAllEngineHealth: vi.fn().mockResolvedValue([]),
    getControlPlaneHealth: vi.fn().mockResolvedValue([]),
  },
}));

let useEngineStore: any;

beforeEach(async () => {
  vi.resetModules();
  const module = await import('../../stores/engineStore');
  useEngineStore = module.useEngineStore;
});

describe('engineStore', () => {
  // Asserted against ACTIVE_ENGINES rather than a hardcoded count. The store
  // derives its engine list from productMode, so the literal `11` this test used
  // to assert was only ever true in 'full' mode; the default 'canonical' mode
  // polls 5. A frozen number here does not test the store, it tests the mode the
  // author happened to be in.
  it('starts with every active engine unreachable', () => {
    const state = useEngineStore.getState();
    expect(Object.keys(state.engines)).toHaveLength(ACTIVE_ENGINES.length);
    expect(state.engines['shield'].status).toBe('unreachable');
    expect(state.engines['ears'].status).toBe('unreachable');
  });

  it('computes healthyCount and allHealthy', () => {
    const state = useEngineStore.getState();
    expect(state.healthyCount).toBe(0);
    expect(state.allHealthy).toBe(false);
  });

  it('counts the control plane in totalCount, not just engines', () => {
    expect(useEngineStore.getState().totalCount).toBe(
      ACTIVE_ENGINES.length + ACTIVE_CONTROL_PLANE.length,
    );
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

  // This replaces a test that asserted allHealthy became true once every ENGINE
  // was healthy. That stopped being the contract when the control plane was added
  // to totalCount: healthy engines alone can no longer satisfy it, and the old
  // test was failing for the correct reason. Pinning the real rule instead.
  it('healthy engines alone do NOT satisfy allHealthy — the control plane counts', () => {
    act(() => {
      const state = useEngineStore.getState();
      Object.keys(state.engines).forEach((key) => {
        state.updateEngine(key, { engine: key, status: 'healthy' });
      });
    });
    const state = useEngineStore.getState();
    expect(state.healthyCount).toBe(ACTIVE_ENGINES.length);
    expect(state.allHealthy).toBe(false);
  });

  it('allHealthy is true only when engines AND control plane are all healthy', async () => {
    const apiModule = await import('../../services/api');
    (apiModule.default.getAllEngineHealth as any).mockResolvedValueOnce(
      ACTIVE_ENGINES.map((engine) => ({ engine, status: 'healthy' })),
    );
    (apiModule.default.getControlPlaneHealth as any).mockResolvedValueOnce(
      ACTIVE_CONTROL_PLANE.map((s) => ({ service: s.name, status: 'healthy' })),
    );

    await act(async () => {
      await useEngineStore.getState().fetchAll();
    });

    const state = useEngineStore.getState();
    expect(state.healthyCount).toBe(ACTIVE_ENGINES.length + ACTIVE_CONTROL_PLANE.length);
    expect(state.allHealthy).toBe(true);
  });

  it('fetchAll calls the API and handles response', async () => {
    const apiModule = await import('../../services/api');
    (apiModule.default.getAllEngineHealth as any).mockResolvedValueOnce([
      { engine: 'shield', status: 'healthy', latencyMs: 10 },
    ]);
    (apiModule.default.getControlPlaneHealth as any).mockResolvedValueOnce([]);

    await act(async () => {
      await useEngineStore.getState().fetchAll();
    });

    expect(apiModule.default.getAllEngineHealth).toHaveBeenCalled();
    expect(apiModule.default.getControlPlaneHealth).toHaveBeenCalled();
    expect(useEngineStore.getState().engines['shield'].status).toBe('healthy');
  });
});
