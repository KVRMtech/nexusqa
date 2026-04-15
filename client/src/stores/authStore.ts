// ═══════════════════════════════════════════════════════════════
//  NEXUS AI ENGINE FACTORY — Auth Store (Zustand)
// ═══════════════════════════════════════════════════════════════
import { create } from 'zustand';
import { persist, createJSONStorage } from 'zustand/middleware';
import type { User } from '../types';
import api from '../services/api';

interface AuthState {
  user: User | null;
  token: string | null;
  isAuthenticated: boolean;
  isLoading: boolean;
  error: string | null;
}

interface AuthActions {
  login: (email: string, password: string) => Promise<void>;
  register: (tenantName: string, email: string, password: string, name: string) => Promise<void>;
  logout: () => void;
  setLoading: (loading: boolean) => void;
  clearError: () => void;
  /** Restore session from persisted storage (already handled by `persist` middleware). */
  hydrate: () => void;
}

export type AuthStore = AuthState & AuthActions;

const INITIAL_STATE: AuthState = {
  user: null,
  token: null,
  isAuthenticated: false,
  isLoading: true,
  error: null,
};

export const useAuthStore = create<AuthStore>()(
  persist(
    (set, get) => ({
      ...INITIAL_STATE,

      login: async (email: string, password: string) => {
        set({ isLoading: true, error: null });
        try {
          const data = await api.login(email, password);
          const u = data.user || data;
          const user: User = {
            user_id: u.user_id,
            tenant_id: u.tenant_id,
            email: u.email,
            name: u.name || email.split('@')[0],
            role: u.role || 'user',
            permissions: u.permissions || [],
          };
          set({
            user,
            token: data.access_token,
            isAuthenticated: true,
            isLoading: false,
            error: null,
          });
        } catch (err: any) {
          const message =
            err?.response?.data?.detail ||
            err?.message ||
            'Login failed. Please check your credentials.';
          set({ isLoading: false, error: message });
          throw err;
        }
      },

      register: async (tenantName, email, password, name) => {
        set({ isLoading: true, error: null });
        try {
          await api.register(tenantName, email, password, name);
          // Auto-login after registration
          await get().login(email, password);
        } catch (err: any) {
          const message =
            err?.response?.data?.detail || err?.message || 'Registration failed.';
          set({ isLoading: false, error: message });
          throw err;
        }
      },

      logout: () => {
        set({
          user: null,
          token: null,
          isAuthenticated: false,
          isLoading: false,
          error: null,
        });
        // Also clear legacy localStorage keys for backward compat
        localStorage.removeItem('nexus_token');
        localStorage.removeItem('nexus_user');
      },

      setLoading: (loading) => set({ isLoading: loading }),
      clearError: () => set({ error: null }),

      hydrate: () => {
        const state = get();
        // If persist middleware already restored a token+user, mark authenticated
        if (state.token && state.user) {
          set({ isAuthenticated: true, isLoading: false });
        } else {
          // Try legacy localStorage keys (migration from AuthContext)
          const legacyToken = localStorage.getItem('nexus_token');
          const legacyUser = localStorage.getItem('nexus_user');
          if (legacyToken && legacyUser) {
            try {
              const user = JSON.parse(legacyUser) as User;
              set({ user, token: legacyToken, isAuthenticated: true, isLoading: false });
            } catch {
              set({ isLoading: false });
            }
          } else {
            set({ isLoading: false });
          }
        }
      },
    }),
    {
      name: 'nexus-auth',
      storage: createJSONStorage(() => localStorage),
      partialize: (state) => ({
        user: state.user,
        token: state.token,
        isAuthenticated: state.isAuthenticated,
      }),
      onRehydrateStorage: () => (state) => {
        // After rehydration, mark loading as false
        state?.hydrate();
      },
    },
  ),
);

// Selector helpers for components
export const selectUser = (s: AuthStore) => s.user;
export const selectTenantId = (s: AuthStore) => s.user?.tenant_id ?? '';
export const selectIsAuthenticated = (s: AuthStore) => s.isAuthenticated;
export const selectIsLoading = (s: AuthStore) => s.isLoading;
export const selectAuthError = (s: AuthStore) => s.error;
