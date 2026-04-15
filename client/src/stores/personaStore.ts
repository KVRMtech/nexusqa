// ═══════════════════════════════════════════════════════════════
//  NEXUS AI ENGINE FACTORY — Persona Store (Zustand)
// ═══════════════════════════════════════════════════════════════
import { create } from 'zustand';
import type { Persona, CreatePersonaRequest, UpdatePersonaRequest } from '../types';
import api from '../services/api';

// ── State & Actions ─────────────────────────────────────────

interface PersonaState {
  personas: Persona[];
  selectedPersona: Persona | null;
  isLoading: boolean;
  error: string | null;
}

interface PersonaActions {
  fetchPersonas: (includeInactive?: boolean) => Promise<void>;
  selectPersona: (persona: Persona | null) => void;
  createPersona: (body: CreatePersonaRequest) => Promise<Persona>;
  updatePersona: (personaId: string, body: UpdatePersonaRequest) => Promise<Persona>;
  deletePersona: (personaId: string) => Promise<void>;
  getPersonaById: (personaId: string) => Persona | undefined;
  clearPersonas: () => void;
}

export type PersonaStore = PersonaState & PersonaActions;

// ── Selectors ───────────────────────────────────────────────

export const selectSystemPersonas = (state: PersonaStore) =>
  state.personas.filter((p) => p.is_system);

export const selectCustomPersonas = (state: PersonaStore) =>
  state.personas.filter((p) => !p.is_system);

export const selectActivePersonas = (state: PersonaStore) =>
  state.personas.filter((p) => p.is_active);

// ── Store ───────────────────────────────────────────────────

export const usePersonaStore = create<PersonaStore>()((set, get) => ({
  personas: [],
  selectedPersona: null,
  isLoading: false,
  error: null,

  fetchPersonas: async (includeInactive = false) => {
    set({ isLoading: true, error: null });
    try {
      const data = await api.listPersonas(includeInactive);
      set({ personas: Array.isArray(data) ? data : [], isLoading: false });
    } catch (err: any) {
      set({ isLoading: false, error: err?.message || 'Failed to fetch personas' });
    }
  },

  selectPersona: (persona) => set({ selectedPersona: persona }),

  createPersona: async (body) => {
    const persona = await api.createPersona(body);
    set((state) => ({ personas: [...state.personas, persona] }));
    return persona;
  },

  updatePersona: async (personaId, body) => {
    const updated = await api.updatePersona(personaId, body);
    set((state) => ({
      personas: state.personas.map((p) => (p.persona_id === personaId ? updated : p)),
      selectedPersona:
        state.selectedPersona?.persona_id === personaId ? updated : state.selectedPersona,
    }));
    return updated;
  },

  deletePersona: async (personaId) => {
    await api.deletePersona(personaId);
    set((state) => ({
      personas: state.personas.filter((p) => p.persona_id !== personaId),
      selectedPersona:
        state.selectedPersona?.persona_id === personaId ? null : state.selectedPersona,
    }));
  },

  getPersonaById: (personaId) => get().personas.find((p) => p.persona_id === personaId),

  clearPersonas: () => set({ personas: [], selectedPersona: null, error: null }),
}));
