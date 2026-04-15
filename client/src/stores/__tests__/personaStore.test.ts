import { describe, it, expect, beforeEach, vi } from 'vitest';
import { act } from '@testing-library/react';

vi.unmock('../../stores/personaStore');

// Mock the api module
vi.mock('../../services/api', () => ({
  default: {
    listPersonas: vi.fn().mockResolvedValue([]),
    getPersona: vi.fn().mockResolvedValue({}),
    createPersona: vi.fn().mockResolvedValue({ persona_id: 'p-new', name: 'New' }),
    updatePersona: vi.fn().mockResolvedValue({ persona_id: 'p-1', name: 'Updated' }),
    deletePersona: vi.fn().mockResolvedValue({}),
  },
}));

let usePersonaStore: any;

beforeEach(async () => {
  vi.resetModules();
  const module = await import('../../stores/personaStore');
  usePersonaStore = module.usePersonaStore;
});

describe('personaStore', () => {
  it('starts with empty personas', () => {
    const state = usePersonaStore.getState();
    expect(state.personas).toEqual([]);
    expect(state.selectedPersona).toBeNull();
    expect(state.isLoading).toBe(false);
    expect(state.error).toBeNull();
  });

  it('fetchPersonas updates persona list', async () => {
    const api = (await import('../../services/api')).default;
    (api.listPersonas as any).mockResolvedValueOnce([
      { persona_id: 'p-1', name: 'Analyst', is_system: true },
      { persona_id: 'p-2', name: 'Custom', is_system: false },
    ]);

    await act(async () => {
      await usePersonaStore.getState().fetchPersonas();
    });

    const state = usePersonaStore.getState();
    expect(state.personas).toHaveLength(2);
    expect(state.personas[0].name).toBe('Analyst');
    expect(state.isLoading).toBe(false);
  });

  it('fetchPersonas handles errors', async () => {
    const api = (await import('../../services/api')).default;
    (api.listPersonas as any).mockRejectedValueOnce(new Error('Network error'));

    await act(async () => {
      await usePersonaStore.getState().fetchPersonas();
    });

    const state = usePersonaStore.getState();
    expect(state.error).toBeTruthy();
    expect(state.isLoading).toBe(false);
  });

  it('selectPersona updates selectedPersona', () => {
    const persona = { persona_id: 'p-1', name: 'Test' };
    act(() => {
      usePersonaStore.getState().selectPersona(persona);
    });
    expect(usePersonaStore.getState().selectedPersona).toEqual(persona);
  });

  it('createPersona adds to list', async () => {
    await act(async () => {
      await usePersonaStore.getState().createPersona({ name: 'New', slug: 'new' });
    });

    const state = usePersonaStore.getState();
    expect(state.personas.some((p: any) => p.persona_id === 'p-new')).toBe(true);
  });

  it('deletePersona removes from list', async () => {
    // First add two personas
    const api = (await import('../../services/api')).default;
    (api.listPersonas as any).mockResolvedValueOnce([
      { persona_id: 'p-1', name: 'A' },
      { persona_id: 'p-2', name: 'B' },
    ]);

    await act(async () => {
      await usePersonaStore.getState().fetchPersonas();
    });
    expect(usePersonaStore.getState().personas).toHaveLength(2);

    await act(async () => {
      await usePersonaStore.getState().deletePersona('p-1');
    });
    expect(usePersonaStore.getState().personas).toHaveLength(1);
    expect(usePersonaStore.getState().personas[0].persona_id).toBe('p-2');
  });

  it('clearPersonas resets state', () => {
    act(() => {
      usePersonaStore.getState().selectPersona({ persona_id: 'p-1' });
    });
    expect(usePersonaStore.getState().selectedPersona).not.toBeNull();

    act(() => {
      usePersonaStore.getState().clearPersonas();
    });
    expect(usePersonaStore.getState().selectedPersona).toBeNull();
    expect(usePersonaStore.getState().personas).toEqual([]);
  });
});
