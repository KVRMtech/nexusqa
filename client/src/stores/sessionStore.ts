// ═══════════════════════════════════════════════════════════════
//  NEXUS AI ENGINE FACTORY — Session Store (Zustand)
// ═══════════════════════════════════════════════════════════════
import { create } from 'zustand';
import type { KTSession, Workflow } from '../types';
import api from '../services/api';
import { useAuthStore } from './authStore';

interface SessionState {
  sessions: KTSession[];
  activeSession: KTSession | null;
  workflows: Workflow[];
  isLoadingSessions: boolean;
  isLoadingWorkflows: boolean;
  error: string | null;
}

interface SessionActions {
  fetchSessions: () => Promise<void>;
  fetchWorkflows: () => Promise<void>;
  setActiveSession: (session: KTSession | null) => void;
  startWorkflow: (chainName: string, input: Record<string, any>) => Promise<Workflow>;
  updateWorkflowStatus: (workflowId: string, workflow: Partial<Workflow>) => void;
  updateSessionStatus: (sessionId: string, update: Partial<KTSession>) => void;
  clearSessions: () => void;
}

export type SessionStore = SessionState & SessionActions;

const getTenantId = () => useAuthStore.getState().user?.tenant_id ?? 't-1';

export const useSessionStore = create<SessionStore>()((set, get) => ({
  sessions: [],
  activeSession: null,
  workflows: [],
  isLoadingSessions: false,
  isLoadingWorkflows: false,
  error: null,

  fetchSessions: async () => {
    set({ isLoadingSessions: true, error: null });
    try {
      const data = await api.listSessions(getTenantId());
      set({ sessions: Array.isArray(data) ? data : [], isLoadingSessions: false });
    } catch (err: any) {
      set({ isLoadingSessions: false, error: err?.message || 'Failed to fetch sessions' });
    }
  },

  fetchWorkflows: async () => {
    set({ isLoadingWorkflows: true, error: null });
    try {
      const data = await api.listWorkflows(getTenantId());
      set({ workflows: Array.isArray(data) ? data : [], isLoadingWorkflows: false });
    } catch (err: any) {
      set({ isLoadingWorkflows: false, error: err?.message || 'Failed to fetch workflows' });
    }
  },

  setActiveSession: (session) => set({ activeSession: session }),

  startWorkflow: async (chainName, input) => {
    const tenantId = getTenantId();
    const workflow = await api.startWorkflow(chainName, tenantId, input);
    set((state) => ({
      workflows: [workflow, ...state.workflows],
    }));
    return workflow;
  },

  updateWorkflowStatus: (workflowId, update) =>
    set((state) => ({
      workflows: state.workflows.map((w) =>
        w.workflow_id === workflowId ? { ...w, ...update } : w,
      ),
    })),

  updateSessionStatus: (sessionId, update) =>
    set((state) => ({
      sessions: state.sessions.map((s) =>
        s.session_id === sessionId ? { ...s, ...update } : s,
      ),
      activeSession:
        state.activeSession?.session_id === sessionId
          ? { ...state.activeSession, ...update }
          : state.activeSession,
    })),

  clearSessions: () =>
    set({ sessions: [], activeSession: null, workflows: [], error: null }),
}));
